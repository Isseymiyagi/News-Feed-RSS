
import sqlite3

conn = sqlite3.connect("news_vault.db")
cursor = conn.cursor()


cursor.execute("""
    DELETE FROM articles
    WHERE image_url IS NULL
       OR TRIM(image_url) = ''
""")

print(f"Deleted {cursor.rowcount} articles.")

conn.commit()
conn.close()