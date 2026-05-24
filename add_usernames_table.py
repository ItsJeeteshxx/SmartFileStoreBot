import sqlite3

conn = sqlite3.connect("my_account.session")
cursor = conn.cursor()
try:
    cursor.execute("""
    CREATE TABLE usernames
    (
        id             TEXT PRIMARY KEY,
        peer_id        INTEGER NOT NULL,
        last_update_on INTEGER NOT NULL DEFAULT (CAST(STRFTIME('%s', 'now') AS INTEGER))
    );
    """)
    conn.commit()
    print("Table 'usernames' created successfully in my_account.session!")
except sqlite3.OperationalError as e:
    print("Error or table already exists:", e)
conn.close()
