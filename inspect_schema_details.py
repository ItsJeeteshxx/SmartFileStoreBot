import sqlite3

conn = sqlite3.connect("main_bot_session.session")
cursor = conn.cursor()
cursor.execute("SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'index');")
for row in cursor.fetchall():
    print(f"Name: {row[0]}, Type: {row[1]}\nSQL: {row[2]}\n")
conn.close()
