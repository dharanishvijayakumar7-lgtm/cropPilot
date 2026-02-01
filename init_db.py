import sqlite3

conn = sqlite3.connect("farmers.db")
cursor = conn.cursor()

# Create users table
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    state TEXT,
    district TEXT
)
""")

conn.commit()
conn.close()

print("Database initialized and users table created.")
