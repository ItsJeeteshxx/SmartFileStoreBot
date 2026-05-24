import glob
import sqlite3

for f in glob.glob("*.session") + glob.glob("AryaPremium/*.session"):
    print(f"\n=================== Session File: {f} ===================")
    try:
        conn = sqlite3.connect(f)
        cursor = conn.cursor()
        
        # Check sessions table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t[0] for t in cursor.fetchall()]
        
        if 'sessions' in tables:
            cursor.execute("SELECT dc_id, api_id, user_id, is_bot FROM sessions LIMIT 5;")
            print("Sessions table rows:")
            for row in cursor.fetchall():
                print(f"  DC: {row[0]}, API_ID: {row[1]}, User ID: {row[2]}, Is Bot: {row[3]}")
                
        if 'peers' in tables:
            cursor.execute("SELECT id, type, username FROM peers LIMIT 5;")
            print("Peers table rows:")
            for row in cursor.fetchall():
                print(f"  ID: {row[0]}, Type: {row[1]}, Username: {row[2]}")
                
        conn.close()
    except Exception as e:
        print(f"Error reading session: {e}")
