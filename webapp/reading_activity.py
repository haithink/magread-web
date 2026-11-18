"""Effective reading time and progress samples. No model calls or article text stored."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import re
import sqlite3
import time

from kandu import core_config as config, core_gen as gen
from webapp import reading_stats

MAX_INTERVAL_MS = 120_000
MIN_SPEED_MS = 30_000
MIN_SPEED_WORDS = 20


def database():
    return config.app_dir() / 'reading-activity.sqlite3'


@contextmanager
def connection():
    db = sqlite3.connect(database(), timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, title_key TEXT NOT NULL,
                sequence INTEGER NOT NULL, elapsed_ms INTEGER NOT NULL,
                last_seen INTEGER NOT NULL, furthest_word INTEGER NOT NULL,
                pending_ms INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS intervals (
                id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, title_key TEXT NOT NULL,
                started_at INTEGER NOT NULL, ended_at INTEGER NOT NULL,
                active_ms INTEGER NOT NULL, words INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS intervals_date ON intervals(ended_at);
            CREATE INDEX IF NOT EXISTS intervals_title ON intervals(title_key);
            CREATE INDEX IF NOT EXISTS intervals_source ON intervals(source_id);
            CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY, credited_until INTEGER NOT NULL);
        ''')
        if 'pending_ms' not in {r['name'] for r in db.execute('PRAGMA table_info(sessions)')}:
            db.execute('ALTER TABLE sessions ADD COLUMN pending_ms INTEGER NOT NULL DEFAULT 0')
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def speed(words, active_ms):
    # Very short samples and instant jumps are not a meaningful reading speed.
    if active_ms < MIN_SPEED_MS or words < MIN_SPEED_WORDS:
        return None
    value = words * 60_000 / active_ms
    return round(value) if 1 <= value <= 1000 else None


def timing(words=0, active_ms=0):
    return dict(activeMs=active_ms, measuredWords=words, wordsPerMinute=speed(words, active_ms))


def word_position(art, data):
    index, offset = data.get('paragraphIndex'), data.get('paragraphOffset')
    if (type(index) is not int or not 0 <= index < len(art['paragraphs']) or
            type(offset) is not int or offset < 0):
        raise ValueError('计时阅读位置无效')
    return sum(len(gen._TOKEN.findall(p)) for p in art['paragraphs'][:index]) + \
        len(gen._TOKEN.findall(art['paragraphs'][index][:offset]))


def _record_result(db,source_id,active=0,words=0):
    total = db.execute('SELECT COALESCE(SUM(active_ms),0) FROM intervals WHERE source_id=?', (source_id,)).fetchone()[0]
    return dict(acceptedMs=active,acceptedWords=words,totalActiveMs=total)


def record(meta, art, data):
    """Cumulative counters make retries idempotent; server time bounds the credit.

    One account represents one reader. A global credit watermark prevents two
    overlapping tabs/devices from counting the same wall-clock time twice.
    """
    session_id, sequence, elapsed = data.get('sessionId'), data.get('sequence'), data.get('elapsedMs')
    if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9-]{16,80}', session_id):
        raise ValueError('阅读计时会话无效')
    if type(sequence) is not int or not 0 <= sequence <= 10_000_000:
        raise ValueError('阅读计时序号无效')
    if type(elapsed) is not int or not 0 <= elapsed <= 7 * 86_400_000:
        raise ValueError('阅读计时数据无效')
    position = word_position(art, data)
    source_id = reading_stats.web_id(meta['id'], art['index'])
    key = reading_stats.title_key(art['title'])
    now = time.time_ns() // 1_000_000
    sampled = data.get('sampledAt')
    if sampled is not None:
        if type(sampled) is not int or not now-7*86_400_000 <= sampled <= now+30_000:
            raise ValueError('计时采样时间无效或已超过七天')
        now = min(sampled, now)
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
        if previous and previous['source_id'] != source_id:
            raise ValueError('计时会话与文章不匹配')
        if previous is None:
            # Register the restored position as a baseline, not newly read words.
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
                (session_id, source_id, key, sequence, elapsed, now, position,0))
            # Old session counters are unnecessary after a week of inactivity.
            db.execute('DELETE FROM sessions WHERE last_seen<?', (now - 7 * 86_400_000,))
            return _record_result(db,source_id)
        if sequence <= previous['sequence']:
            return _record_result(db,source_id)
        if elapsed < previous['elapsed_ms']:
            raise ValueError('阅读计时累计值不能回退')
        if now < previous['last_seen']:
            raise ValueError('阅读计时采样时间不能回退')
        credit = db.execute('SELECT credited_until FROM clock WHERE id=1').fetchone()
        active = max(0, min(elapsed - previous['elapsed_ms'], now - previous['last_seen'],
            MAX_INTERVAL_MS, now - credit['credited_until'] if credit else MAX_INTERVAL_MS))
        words = max(0, position - previous['furthest_word']) if active else 0
        pending = previous['pending_ms'] + active
        # Page search, dragging the scrollbar, or skipping to the end must not
        # turn thousands of skipped words into measured reading volume/speed.
        if words > max(40, pending / 60):
            words = 0
        if position > previous['furthest_word']:
            pending = 0
        if active:
            db.execute('INSERT INTO intervals(source_id,title_key,started_at,ended_at,active_ms,words) VALUES (?,?,?,?,?,?)',
                (source_id, key, now-active, now, active, words))
            db.execute('INSERT OR REPLACE INTO clock VALUES (1,?)', (now,))
        db.execute('UPDATE sessions SET sequence=?,elapsed_ms=?,last_seen=?,furthest_word=?,pending_ms=? WHERE id=?',
            (sequence, elapsed, now, max(position, previous['furthest_word']), pending, session_id))
        result = _record_result(db,source_id,active,words)
    return result


def article_timing(book_id, index):
    if not database().is_file():
        return timing()
    with connection() as db:
        row = db.execute('SELECT COALESCE(SUM(words),0) words, COALESCE(SUM(active_ms),0) active_ms '
            'FROM intervals WHERE source_id=?', (reading_stats.web_id(book_id, index),)).fetchone()
    return timing(row['words'], row['active_ms'])


def _streak(days, today):
    ordered = sorted(days)
    longest, run, previous = 0, 0, None
    for day in ordered:
        current = datetime.strptime(day, '%Y-%m-%d').date()
        run = run + 1 if previous and current == previous + timedelta(days=1) else 1
        longest, previous = max(longest, run), current
    current = 0
    cursor = today if today.isoformat() in days else today - timedelta(days=1)
    while cursor.isoformat() in days:
        current += 1
        cursor -= timedelta(days=1)
    return current, longest


def report(articles, days=365, timezone_offset=0, publications=None):
    """Completed volume and measured volume are different, explicitly labeled metrics."""
    tz = timezone(timedelta(minutes=-timezone_offset))
    now = datetime.now(tz)
    today = now.date()
    start = today - timedelta(days=days-1)
    first_ms = int(datetime.combine(start, datetime.min.time(), tz).timestamp()*1000)
    daily = { (start+timedelta(days=i)).isoformat():dict(date=(start+timedelta(days=i)).isoformat(),
        activeMs=0, measuredWords=0, completedWords=0, completedArticles=0) for i in range(days)}
    by_title, active_dates = {}, set()
    total_words = total_ms = 0
    if database().is_file():
        with connection() as db:
            for row in db.execute('SELECT title_key,SUM(words) words,SUM(active_ms) active_ms FROM intervals GROUP BY title_key'):
                by_title[row['title_key']] = timing(row['words'], row['active_ms'])
                total_words += row['words']
                total_ms += row['active_ms']
            modifier = f'{-timezone_offset:+d} minutes'
            active_dates = {r[0] for r in db.execute("SELECT date(started_at/1000,'unixepoch',?) FROM intervals "
                "UNION SELECT date((ended_at-1)/1000,'unixepoch',?) FROM intervals", (modifier,modifier))}
            # The date index limits chart work to the requested time window.
            for row in db.execute('SELECT started_at,ended_at,active_ms,words FROM intervals WHERE ended_at>=?', (first_ms,)):
                cursor, end, duration = row['started_at'], row['ended_at'], row['active_ms']
                allocated_words = 0
                while cursor < end:
                    local = datetime.fromtimestamp(cursor/1000, tz)
                    tomorrow = datetime.combine(local.date()+timedelta(days=1), datetime.min.time(), tz)
                    boundary = min(end, int(tomorrow.timestamp()*1000))
                    day = local.date().isoformat()
                    active_dates.add(day)
                    # Cumulative rounding preserves the exact word sum over midnight.
                    cumulative_words = round(row['words'] * (boundary-row['started_at']) / max(1,duration))
                    words = cumulative_words - allocated_words
                    allocated_words = cumulative_words
                    if end >= first_ms and day in daily:
                        daily[day]['activeMs'] += boundary-cursor
                        daily[day]['measuredWords'] += words
                    cursor = boundary
    distribution = defaultdict(lambda:dict(articleCount=0, words=0))
    for article in articles:
        article['timing'] = by_title.get(reading_stats.title_key(article['title']), timing())
        # Attribute deduplicated completion to its first completion day/source.
        primary = min(article['sources'], key=lambda s:s.get('firstReadAt',s['readAt']))
        date = datetime.fromtimestamp(primary.get('firstReadAt',primary['readAt'])/1000, tz).date().isoformat()
        if date in daily:
            daily[date]['completedWords'] += article['wordCount']
            daily[date]['completedArticles'] += 1
        label = primary.get('publication') or (publications or {}).get(primary.get('bookId')) or \
            re.sub(r'[_ .-]*\d{4}.*$', '', primary['source']).replace('_',' ').strip()
        label = label or primary['source']
        distribution[label]['articleCount'] += 1
        distribution[label]['words'] += article['wordCount']
    for value in daily.values():
        value['wordsPerMinute'] = speed(value['measuredWords'], value['activeMs'])
    current, longest = _streak(active_dates, today)
    return dict(daily=list(daily.values()), totals=timing(total_words,total_ms),
        activeDays=len(active_dates), currentStreak=current, longestStreak=longest,
        sources=sorted((dict(name=k,**v) for k,v in distribution.items()),key=lambda v:v['words'],reverse=True),
        timezoneOffset=timezone_offset, minSpeedMs=MIN_SPEED_MS, minSpeedWords=MIN_SPEED_WORDS)
