"""Login-only throttling. Proxy headers require an explicit, local tunnel boundary."""
from contextlib import closing
import hashlib
import hmac
import ipaddress
import math
import sqlite3
import time


class ClientAddressError(ValueError):
    pass


def _ip(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def login_address(peer, hostname, cloudflare_hosts, header):
    """Ignore arbitrary forwarded headers on ordinary local/LAN deployments."""
    try:
        address = _ip(peer)
    except ValueError:
        raise ClientAddressError('无法确认登录连接来源') from None
    if hostname.lower() in cloudflare_hosts:
        # cloudflared must connect over loopback. LAN clients cannot opt into
        # trusting a forged header by choosing the public Host header.
        if not address.is_loopback:
            raise ClientAddressError('公网登录必须经过本机 Cloudflare Tunnel')
        try:
            address = _ip(header)
        except ValueError:
            raise ClientAddressError('缺少或无效的 Cloudflare 客户端地址') from None
    # Group an IPv6 /64 so rotating privacy addresses cannot reset the budget.
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f'{address}/64', strict=False))
    return str(address)


class LoginLimiter:
    """Reserve attempts atomically before hashing; keep a sliding window on disk."""
    def __init__(self, path, secret, limit=10, window=600):
        self.path = path
        self.secret = secret.encode()
        self.limit, self.window = limit, window
        with closing(self._connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address_key TEXT NOT NULL, attempted REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_address ON attempts(address_key, attempted);
                CREATE INDEX IF NOT EXISTS attempts_time ON attempts(attempted);
            ''')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=2)

    def _key(self, address):
        # Persist no raw IPs or passwords. The app secret is already private.
        return hmac.new(self.secret, address.encode(), hashlib.sha256).hexdigest()

    def reserve(self, address):
        key, now = self._key(address), time.time()
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE attempted <= ?', (now - self.window,))
            count, first = db.execute(
                'SELECT COUNT(*), MIN(attempted) FROM attempts WHERE address_key=?', (key,)).fetchone()
            if count >= self.limit:
                # Rejected requests do not extend the lockout.
                return None, max(1, math.ceil(first + self.window - now))
            ticket = db.execute('INSERT INTO attempts(address_key, attempted) VALUES (?, ?)', (key, now)).lastrowid
            return ticket, 0

    def success(self, address, ticket):
        with closing(self._connect()) as db, db:
            # Do not clear concurrent attempts that started after this login.
            db.execute('DELETE FROM attempts WHERE address_key=? AND id<=?', (self._key(address), ticket))
