"""Bounded, content-free reader diagnostics stored outside the source tree."""
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time


EVENTS = frozenset(('analysis.start', 'analysis.cache-read', 'analysis.cache-ready',
    'analysis.submit', 'analysis.accepted', 'analysis.poll', 'analysis.cache-write',
    'analysis.cache-saved', 'analysis.render', 'analysis.rendered', 'analysis.failure',
    'sheet.center', 'sheet.centered', 'sheet.dock', 'sheet.docked', 'sheet.finish',
    'sheet.close', 'sheet.closed', 'sheet.pointer', 'sheet.click', 'sheet.native-close',
    'wait.heartbeat', 'page.visibility', 'page.resume', 'page.error', 'page.rejection'))
ERRORS = frozenset(('Error', 'TypeError', 'RangeError', 'ReferenceError', 'SyntaxError',
    'InvalidStateError', 'AbortError', 'QuotaExceededError', 'SecurityError',
    'NotFoundError', 'UnknownError', 'TransactionInactiveError', 'TimeoutError'))
PHASES = frozenset(('start', 'cache-read', 'cache-ready', 'submit', 'accepted', 'poll',
    'cache-write', 'cache-saved', 'render', 'rendered', 'failure'))


def clean_event(value):
    if not isinstance(value, dict) or not isinstance(value.get('event'), str) or value['event'] not in EVENTS:
        raise ValueError('Invalid diagnostic event')
    result = {'event': value['event']}
    for key, pattern in (('page', r'[a-f0-9]{16}'), ('trace', r'[a-f0-9]{12}'),
                         ('build', r'[a-f0-9]{16}'), ('job', r'[A-Za-z0-9_-]{1,64}')):
        if isinstance(value.get(key), str) and re.fullmatch(pattern, value[key]):
            result[key] = value[key]
    for key in ('elapsedMs', 'sequence', 'sheet', 'width', 'height', 'visualHeight',
                'scrollY', 'activeJobs', 'polls', 'line', 'column', 'gapMs', 'highlightRanges'):
        number = value.get(key)
        if type(number) in (int, float) and 0 <= number <= 10**10:
            result[key] = round(number)
    for key in ('hidden', 'online', 'standalone', 'dialogOpen', 'dialogModal', 'waitPresent', 'waitHidden', 'modalHighlightsSuspended'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key, choices in (('phase', PHASES), ('errorName', ERRORS),
                         ('jobState', {'queued', 'running', 'done', 'error', 'stopped'}),
                         ('platform', {'ios', 'android', 'desktop'}),
                         ('engine', {'webkit', 'chromium', 'other'}),
                         ('control', {'wait-close', 'wait-view', 'sheet-close', 'sheet-background'}),
                         ('source', {'app.js', 'reading-cache.js', 'pwa.js'})):
        if isinstance(value.get(key), str) and value[key] in choices:
            result[key] = value[key]
    # Only static-source coordinates are retained, never messages, URLs, stacks,
    # selected text, user agent strings or request bodies.
    return result


class Diagnostics:
    def __init__(self, root, max_bytes=2 * 1024 * 1024, backups=3):
        self.path = Path(root) / 'diagnostics' / 'reader-events.jsonl'
        self.max_bytes, self.backups = max_bytes, backups
        self.lock = threading.RLock()
        self.requests = deque()

    def reserve(self):
        with self.lock:
            now = time.monotonic()
            while self.requests and self.requests[0] < now - 60:
                self.requests.popleft()
            if len(self.requests) >= 120:
                return False
            self.requests.append(now)
            return True

    def write(self, event):
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                    for i in range(self.backups, 0, -1):
                        source = self.path if i == 1 else self.path.with_suffix(f'.jsonl.{i-1}')
                        target = self.path.with_suffix(f'.jsonl.{i}')
                        if source.exists():
                            source.replace(target)
                row = dict(at=datetime.now(timezone.utc).isoformat(timespec='milliseconds'), **event)
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=True, allow_nan=False) + '\n')
        except (OSError, ValueError):
            # A full disk or unavailable diagnostics must never break reading.
            pass
