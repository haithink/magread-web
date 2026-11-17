"""Global daily upload budget with atomic reservations and bounded multipart streams."""
from contextlib import closing
from datetime import datetime, timedelta
import secrets
import sqlite3
from tempfile import SpooledTemporaryFile

from flask import Request
from werkzeug.exceptions import RequestEntityTooLarge, ServiceUnavailable, TooManyRequests

FILE_BYTES = 50 * 1024**2
DAILY_BYTES = 500 * 1024**2
REQUEST_BYTES = FILE_BYTES + 64 * 1024


class UploadQuotaExceeded(ValueError):
    pass


class UploadQuota:
    def __init__(self, path):
        self.path = path
        with closing(self._connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, used INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS reservations (
                    ticket TEXT PRIMARY KEY, day TEXT NOT NULL, size INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS reservations_day ON reservations(day);
            ''')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=2)

    def today(self):
        return datetime.now().astimezone().date().isoformat()

    def _used(self, db, day):
        row = db.execute('SELECT used FROM days WHERE day=?', (day,)).fetchone()
        pending = db.execute('SELECT COALESCE(SUM(size),0) FROM reservations WHERE day=?', (day,)).fetchone()[0]
        return (row[0] if row else 0) + pending

    def status(self, limit=DAILY_BYTES):
        day = self.today()
        with closing(self._connect()) as db:
            used = self._used(db, day)
        reset = datetime.combine(datetime.fromisoformat(day).date() + timedelta(days=1), datetime.min.time()).astimezone()
        return dict(day=day, dailyBytes=limit, usedBytes=used, remainingBytes=max(0, limit-used),
                    resetsAt=reset.isoformat(), timezone=str(datetime.now().astimezone().tzinfo))

    def reserve(self, size, limit=DAILY_BYTES):
        if type(size) is not int or size < 0:
            raise ValueError('无效的上传大小')
        day, ticket = self.today(), secrets.token_urlsafe(24)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if self._used(db, day) + size > limit:
                raise UploadQuotaExceeded('今日上传额度不足，每日累计最多 500 MB，请明天再试')
            db.execute('INSERT INTO reservations VALUES (?, ?, ?)', (ticket, day, size))
            # Old unfinished reservations remain charged for their own day;
            # a crashed process never gives today's reserved quota back.
            db.execute('DELETE FROM reservations WHERE day<?', (day,))
            db.execute('DELETE FROM days WHERE day<?', (day,))
        return ticket

    def finish(self, ticket, received):
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT day, size FROM reservations WHERE ticket=?', (ticket,)).fetchone()
            if row is None:
                return
            if not 0 <= received <= row[1]:
                raise ValueError('上传计量无效')
            db.execute('INSERT INTO days VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET used=used+excluded.used', (row[0], received))
            db.execute('DELETE FROM reservations WHERE ticket=?', (ticket,))

    def charge_overflow(self, ticket, target, limit):
        """Charge received bytes even when a client lied about its file size."""
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT day, size FROM reservations WHERE ticket=?', (ticket,)).fetchone()
            if row is None:
                raise sqlite3.OperationalError('Upload reservation expired')
            day, reserved = row
            remaining = max(0, limit-self._used(db, day))
            size = max(reserved, min(target, reserved+remaining))
            db.execute('UPDATE reservations SET size=? WHERE ticket=?', (size, ticket))
            return size


class UploadState:
    def __init__(self, ticket, reserved, quota, file_limit, daily_limit):
        self.ticket, self.reserved = ticket, reserved
        self.quota, self.file_limit, self.daily_limit = quota, file_limit, daily_limit
        self.received = 0
        self.streams = []
        self.finished = False


class BoundedFile:
    def __init__(self, state):
        self.state = state
        self.file = SpooledTemporaryFile(max_size=512*1024, mode='w+b')

    def write(self, data):
        # Do not trust Content-Length, a client size header or file extension.
        if self.state.received + len(data) > self.state.reserved:
            received = self.state.received + len(data)
            target = min(received, self.state.file_limit)
            try:
                self.state.reserved = self.state.quota.charge_overflow(self.state.ticket, target, self.state.daily_limit)
            except sqlite3.Error:
                raise ServiceUnavailable('上传额度服务暂时不可用') from None
            self.state.received = min(received, self.state.reserved)
            if self.state.reserved < target:
                raise TooManyRequests('今日上传额度已用尽，请明天再试')
            raise RequestEntityTooLarge('文件超过 50 MB 或实际大小超过声明值')
        self.state.received += len(data)
        return self.file.write(data)

    def __getattr__(self, name):
        return getattr(self.file, name)


class UploadRequest(Request):
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        state = self.environ.get('kandu.upload')
        if state is None or state.streams:
            raise RequestEntityTooLarge('每次只能上传一个 EPUB 或 PDF 文件')
        stream = BoundedFile(state)
        state.streams.append(stream)
        return stream
