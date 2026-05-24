import sqlite3

for name in ["my_account.session", "main_bot_session.session"]:
    print("Processing:", name)
    try:
        conn = sqlite3.connect(name)
        cursor = conn.cursor()
        
        # Drop tables if they exist
        cursor.execute("DROP TABLE IF EXISTS usernames;")
        cursor.execute("DROP TABLE IF EXISTS update_state;")
        
        # Also drop related indices just in case
        cursor.execute("DROP INDEX IF EXISTS idx_usernames_username;")
        cursor.execute("DROP INDEX IF EXISTS idx_usernames_id;")
        
        # Update version number to 3
        cursor.execute("UPDATE version SET number = 3;")
        conn.commit()
        
        # Verify
        cursor.execute("SELECT * FROM version;")
        print(f"Successfully reset {name}. Version table contents:", cursor.fetchall())
        conn.close()
    except Exception as e:
        print(f"Error on {name}: {e}")
