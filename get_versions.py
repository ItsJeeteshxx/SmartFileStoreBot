import sqlite3

for name in ["my_account.session", "main_bot_session.session"]:
    conn = sqlite3.connect(name)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM version;")
    print(name, "version:", cursor.fetchall())
    conn.close()
