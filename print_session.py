import sqlite3

conn = sqlite3.connect("my_account.session")
cursor = conn.cursor()

# Get table names
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()
print("Tables:", tables)

for table_name in [t[0] for t in tables]:
    print(f"\n--- Columns in {table_name} ---")
    cursor.execute(f"PRAGMA table_info({table_name});")
    print(cursor.fetchall())
    
    # Print first row
    cursor.execute(f"SELECT * FROM {table_name} LIMIT 1;")
    print("Row 1:", cursor.fetchone())
    
conn.close()
