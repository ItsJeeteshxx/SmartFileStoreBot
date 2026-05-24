import sqlite3

# 1. Update main_bot_session.session
try:
    conn = sqlite3.connect("main_bot_session.session")
    cursor = conn.cursor()
    cursor.execute("UPDATE version SET number = 7;")
    conn.commit()
    print("Updated main_bot_session.session version to 7.")
    conn.close()
except Exception as e:
    print("Error on main_bot_session.session:", e)

# 2. Update my_account.session
try:
    conn = sqlite3.connect("my_account.session")
    cursor = conn.cursor()
    
    # Create usernames table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS usernames (
        id       INTEGER,
        username TEXT,
        FOREIGN KEY (id) REFERENCES peers(id)
    );
    """)
    
    # Create indices
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usernames_username ON usernames (username);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usernames_id ON usernames (id);")
    
    # Create update_state table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS update_state (
        id   INTEGER PRIMARY KEY,
        pts  INTEGER,
        qts  INTEGER,
        date INTEGER,
        seq  INTEGER
    );
    """)
    
    # Set version to 7
    cursor.execute("UPDATE version SET number = 7;")
    conn.commit()
    print("Manually migrated and set my_account.session version to 7.")
    conn.close()
except Exception as e:
    print("Error on my_account.session:", e)
