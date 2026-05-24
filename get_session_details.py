import sqlite3

conn = sqlite3.connect("main_bot_session.session")
cursor = conn.cursor()
cursor.execute("SELECT dc_id, api_id, auth_key, user_id FROM sessions LIMIT 1;")
row = cursor.fetchone()
conn.close()

if row:
    dc_id, api_id, auth_key, user_id = row
    print("dc_id:", dc_id)
    print("api_id:", api_id)
    print("user_id:", user_id)
    print("auth_key length:", len(auth_key))
else:
    print("No session details found.")
