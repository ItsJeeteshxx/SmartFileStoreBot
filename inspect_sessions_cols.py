import sqlite3

for name in ["my_account.session", "main_bot_session.session"]:
    print("=== File:", name)
    try:
        conn = sqlite3.connect(name)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(sessions);")
        cols = cursor.fetchall()
        print("Columns:", [c[1] for c in cols])
        conn.close()
    except Exception as e:
        print("Error:", e)
