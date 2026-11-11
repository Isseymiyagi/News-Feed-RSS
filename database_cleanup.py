
import sqlite3

conn = sqlite3.connect("news_vault.db")
cursor = conn.cursor()


cursor.execute("""
    DELETE FROM articles
    WHERE SUMMARY = 'Failed to generate summary automatically.'
""")

print(f"Deleted {cursor.rowcount} articles.")

conn.commit()
conn.close()