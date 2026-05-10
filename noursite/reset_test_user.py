import sqlite3, hashlib, secrets, json, urllib.request, urllib.error

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 120000).hex()
    return f"{salt}${digest}"

conn = sqlite3.connect('api/matricules.db')
test_pass_hash = hash_password('test123')
conn.execute('UPDATE users SET password_hash=? WHERE username=?', (test_pass_hash, 'testadmin'))
conn.commit()
print('Password reset to test123 for testadmin')

# Now test login
req = urllib.request.Request(
    'http://127.0.0.1:8000/api/token',
    data=json.dumps({'username': 'testadmin', 'password': 'test123'}).encode(),
    headers={'Content-Type': 'application/json'}
)
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        token = json.loads(r.read())['access_token']
        print(f'LOGIN OK, token: {token[:40]}...')
    
    # Test GET access-status
    req2 = urllib.request.Request('http://127.0.0.1:8000/api/messenger/access-status',
        headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req2, timeout=5) as r2:
        print('GET STATUS:', r2.read().decode())
    
    # Test POST access-request
    req3 = urllib.request.Request('http://127.0.0.1:8000/api/messenger/access-request',
        data=b'', method='POST', headers={'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(req3, timeout=5) as r3:
            print('POST REQUEST OK:', r3.read().decode())
    except urllib.error.HTTPError as e:
        print('POST REQUEST ERR', e.code, e.read().decode()[:500])
except urllib.error.HTTPError as e:
    print('LOGIN FAIL', e.code, e.read().decode()[:300])
except Exception as ex:
    print('ERROR:', ex)
finally:
    conn.close()
