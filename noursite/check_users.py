import sqlite3
conn = sqlite3.connect('api/matricules.db')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id, username, email, role FROM users LIMIT 10').fetchall()
for r in rows:
    print(f"ID={r['id']} user={r['username']} email={r['email']} role={r['role']}")
conn.close()
