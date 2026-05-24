import sqlite3

for name in ["my_account.session", "main_bot_session.session"]:
    print("=== File:", name)
    try:
        conn = sqlite3.connect(name)
        cursor = conn.cursor()
        cursor.execute("SELECT name, type, sql FROM sqlite_master WHERE type='table';")
        for row in cursor.fetchall():
            print(f"Table: {row[0]}\nSQL: {row[1]}\n")
        cursor.execute("SELECT * FROM version;")
        print("Version:", cursor.fetchall())
        conn.close()
    except Exception as e:
        print("Error:", e)
