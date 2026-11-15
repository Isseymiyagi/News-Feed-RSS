"""Safely migrate the existing news_vault.db from SQLite to Supabase PostgreSQL.

Prerequisites:
  1. Run 001_news_vault_schema.sql in Supabase SQL Editor.
  2. Place news_vault_supabase_db.py on your Python import path.
  3. Configure SUPABASE_DB_HOST, SUPABASE_DB_USER, SUPABASE_DB_PASSWORD.
  4. Pause ingestion while taking the final SQLite snapshot and importing.

Usage (from your project directory):
  python migrate_news_vault.py                         # Preflight only
  python migrate_news_vault.py --apply                 # Perform import
  python migrate_news_vault.py --sqlite path/to/db     # Alternate source

The script never modifies the SQLite file. PostgreSQL writes are committed only
if both tables and their IDs, counts, and status breakdowns verify successfully.
It refuses to import into nonempty destination tables.
"""

import argparse
import sqlite3
from collections import Counter
from pathlib import Path

from news_vault_supabase_db import get_connection

TABLES = {
    "articles": (
        "id", "title", "source", "raw_content", "summary", "country_code",
        "timestamp", "link_url", "topic_tag", "image_url", "processing_status",
        "attempt_count", "next_attempt_at", "last_error", "processed_at",
        "model_used",
    ),
    "paid_inference_ledger": (
        "id", "charged_utc_date", "article_id", "model", "reserved_usd",
        "actual_usd", "created_at",
    ),
}


def read_source(sqlite_path):
    """Read a consistent, read-only snapshot of the existing SQLite tables."""
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"SQLite file not found: {sqlite_path}")

    uri = sqlite_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as source:
        integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")
        print("[OK] SQLite integrity check passed")

        data = {}
        for table, columns in TABLES.items():
            existing_columns = {
                row[1] for row in source.execute(f'PRAGMA table_info("{table}")')
            }
            missing = set(columns) - existing_columns
            if missing:
                raise RuntimeError(f"SQLite table {table} is missing columns: {sorted(missing)}")

            quoted = ", ".join(f'"{column}"' for column in columns)
            data[table] = source.execute(
                f'SELECT {quoted} FROM "{table}" ORDER BY "id"'
            ).fetchall()
            ids = [row[0] for row in data[table]]
            if len(ids) != len(set(ids)):
                raise RuntimeError(f"Duplicate IDs in SQLite table {table}")
            print(f"[SOURCE] {table}: {len(ids):,} rows")

        article_ids = {row[0] for row in data["articles"]}
        ledger_article_id_index = TABLES["paid_inference_ledger"].index("article_id")
        orphans = [
            row[ledger_article_id_index]
            for row in data["paid_inference_ledger"]
            if row[ledger_article_id_index] not in article_ids
        ]
        if orphans:
            raise RuntimeError(f"Ledger contains {len(orphans)} orphan article references")
        return data


def verify_destination(cursor, source_data):
    """Verify row IDs and processing statuses, not just row counts."""
    for table, rows in source_data.items():
        cursor.execute(f'SELECT id FROM public."{table}" ORDER BY id')
        actual_ids = [record[0] for record in cursor.fetchall()]
        expected_ids = [record[0] for record in rows]
        if actual_ids != expected_ids:
            raise RuntimeError(f"ID verification failed for {table}")
        print(f"[VERIFIED] {table}: {len(actual_ids):,} IDs match")

    status_index = TABLES["articles"].index("processing_status")
    expected_statuses = Counter(row[status_index] for row in source_data["articles"])
    cursor.execute(
        "SELECT processing_status, COUNT(*) FROM public.articles "
        "GROUP BY processing_status"
    )
    actual_statuses = Counter(dict(cursor.fetchall()))
    if actual_statuses != expected_statuses:
        raise RuntimeError(
            f"Processing status mismatch: SQLite={dict(expected_statuses)}, "
            f"PostgreSQL={dict(actual_statuses)}"
        )
    print(f"[VERIFIED] Processing statuses: {dict(expected_statuses)}")

    cursor.execute(
        "SELECT COUNT(*) FROM public.paid_inference_ledger l "
        "LEFT JOIN public.articles a ON a.id = l.article_id "
        "WHERE a.id IS NULL"
    )
    if cursor.fetchone()[0]:
        raise RuntimeError("PostgreSQL ledger has orphan article references")
    print("[VERIFIED] No orphaned paid inference records")


def reset_identity_sequence(cursor, table, rows):
    """Make future inserts use IDs above the imported maximum."""
    cursor.execute(
        "SELECT pg_get_serial_sequence(%s, 'id')",
        (f"public.{table}",),
    )
    sequence_name = cursor.fetchone()[0]
    if sequence_name is None:
        raise RuntimeError(f"No identity/serial sequence found for {table}.id")
    max_id = max((row[0] for row in rows), default=0)
    cursor.execute("SELECT setval(%s, %s, %s)", (sequence_name, max(max_id, 1), bool(rows)))


def migrate(source_data, apply):
    with get_connection() as connection:
        # Explicit transaction: a failed import rolls back both tables.
        with connection.transaction():
            with connection.cursor() as cursor:
                for table in TABLES:
                    cursor.execute(f'SELECT COUNT(*) FROM public."{table}"')
                    count = cursor.fetchone()[0]
                    print(f"[DESTINATION] {table}: {count:,} existing rows")
                    if count != 0:
                        raise RuntimeError(
                            f"Refusing import: public.{table} is not empty. "
                            "No rows were imported."
                        )

                if not apply:
                    print("\n[OK] Preflight passed. No PostgreSQL data was changed.")
                    print("Run with --apply when you are ready to import.")
                    return

                for table, columns in TABLES.items():
                    rows = source_data[table]
                    if rows:
                        quoted = ", ".join(f'"{column}"' for column in columns)
                        placeholders = ", ".join("%s" for _ in columns)
                        statement = (
                            f'INSERT INTO public."{table}" ({quoted}) '
                            f'VALUES ({placeholders})'
                        )
                        cursor.executemany(statement, rows)
                    print(f"[IMPORTED] {table}: {len(rows):,} rows")

                verify_destination(cursor, source_data)
                for table, rows in source_data.items():
                    reset_identity_sequence(cursor, table, rows)
                print("[VERIFIED] PostgreSQL identity sequences updated")

        # Leaving the transaction commits the changes only if all checks pass.
        if apply:
            print("\n[SUCCESS] Migration committed to Supabase PostgreSQL.")
            print("Keep your original SQLite backup until the applications are validated.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite", type=Path, default=Path("news_vault.db"),
        help="Path to source SQLite database (default: news_vault.db)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Import data after preflight checks (otherwise read-only preflight)",
    )
    args = parser.parse_args()
    print("News Vault SQLite -> Supabase PostgreSQL migration")
    print("Mode:", "IMPORT" if args.apply else "PREFLIGHT ONLY")
    source_data = read_source(args.sqlite)
    migrate(source_data, apply=args.apply)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n[ERROR] {exc}")
        raise SystemExit(1) from exc
