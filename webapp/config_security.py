"""Short-lived, revocable access to model settings. Credentials stay outside source."""
from contextlib import closing
import hashlib
import hmac
import json
import secrets
import sqlite3
import time

ACCESS_SECONDS = 15 * 60


class ConfigAccess:
    def __init__(self, root, secret):
        self.credentials = root / 'config-auth.json'
        self.database = root / 'config-access.sqlite3'
        self.secret = secret.encode()
        with closing(self._connect()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS grants (
                token_key TEXT PRIMARY KEY, session_key TEXT NOT NULL,
                credential_version TEXT NOT NULL, expires REAL NOT NULL);''')

    def _connect(self):
        return sqlite3.connect(self.database, timeout=2)

    def _key(self, text):
        return hmac.new(self.secret, text.encode(), hashlib.sha256).hexdigest()

    def password_hash(self):
        try:
            value = json.loads(self.credentials.read_text(encoding='utf-8'))
            password_hash = value.get('passwordHash')
            return password_hash if isinstance(password_hash, str) else ''
        except (FileNotFoundError, ValueError, AttributeError):
            return ''

    def issue(self, csrf, password_hash):
        token, expires = secrets.token_urlsafe(32), time.time() + ACCESS_SECONDS
        with closing(self._connect()) as db, db:
            db.execute('DELETE FROM grants WHERE expires<=?', (time.time(),))
            db.execute('INSERT INTO grants VALUES (?, ?, ?, ?)',
                       (self._key(token), self._key(csrf), self._key(password_hash), expires))
        return token, expires

    def expires(self, token, csrf):
        if not token or not csrf:
            return 0
        password_hash = self.password_hash()
        if not password_hash:
            return 0
        with closing(self._connect()) as db:
            row = db.execute('SELECT expires FROM grants WHERE token_key=? AND session_key=? '
                             'AND credential_version=? AND expires>?',
                             (self._key(token), self._key(csrf), self._key(password_hash), time.time())).fetchone()
        return row[0] if row else 0

    def revoke(self, token):
        if token:
            with closing(self._connect()) as db, db:
                db.execute('DELETE FROM grants WHERE token_key=?', (self._key(token),))
