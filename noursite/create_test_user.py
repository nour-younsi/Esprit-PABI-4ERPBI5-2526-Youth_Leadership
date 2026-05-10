import sqlite3
import hashlib, secrets

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 120000).hex()
    return f"{salt}${digest}"

conn = sqlite3.connect('api/matricules.db')

# Create test account with password 'test123'
test_pass_hash = hash_password('test123')
try:
    conn.execute(
        "INSERT OR REPLACE INTO users (username, email, password_hash, role, verified) VALUES (?, ?, ?, ?, ?)",
        ('testadmin', 'testadmin@test.com', test_pass_hash, 'superadmin', 1)
    )
    conn.commit()
    print("✓ Test admin account created: testadmin / test123")
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()
