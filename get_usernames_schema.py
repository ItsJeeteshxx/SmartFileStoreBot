import sqlite3

conn = sqlite3.connect("main_bot_session.session")
cursor = conn.cursor()
cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='usernames';")
row = cursor.fetchone()
conn.close()
print("usernames SQL:", row[0] if row else "None")
