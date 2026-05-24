import sqlite3

for name in ["my_account.session", "main_bot_session.session"]:
    print("Processing:", name)
    try:
        conn = sqlite3.connect(name)
        cursor = conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS update_state;")
        conn.commit()
        print(f"Successfully dropped 'update_state' from {name}")
        conn.close()
    except Exception as e:
        print(f"Error on {name}: {e}")
