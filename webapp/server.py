"""Authenticated LAN API; model jobs stay off HTTP worker threads."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import hashlib
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request, send_file, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from kandu import core_config as config, core_parsers as store, core_gen as gen
from kandu import core_llm as llm, core_wordbook as wordbook, core_dict, core_discussion as discussion
from kandu import core_sentence
from webapp import __version__
from webapp import reading_stats
from webapp import reading_activity
from webapp import pwa
from webapp.diagnostics import Diagnostics, clean_event
from webapp import word_meaning
from webapp import article_duplicates
from webapp import article_tags
from webapp import podcasts
from webapp import podcast_covers
from webapp import podcast_library
from webapp.login_security import ClientAddressError, LoginLimiter, login_address
from webapp.config_security import ConfigAccess
from webapp.upload_security import (UploadQuota, UploadQuotaExceeded, UploadState, UploadRequest,
                                    FILE_BYTES, DAILY_BYTES, REQUEST_BYTES)

STATIC = Path(__file__).parent / 'static'
BOOK_ID = re.compile(r'[A-Za-z0-9._-]{1,220}\Z')


def clean_error(error):
    text = str(error)[:1000]
    for model in config.load_settings().get('llms', []):
        if model.get('apiKey'):
            text = text.replace(model['apiKey'], '[已隐藏密钥]')
    return re.sub(r'sk-[A-Za-z0-9_-]+', '[已隐藏密钥]', text)


class Jobs:
    def __init__(self, diagnostics=None):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='kandu-web')
        self.lock = threading.RLock()
        self.items = OrderedDict()
        self.diagnostics = diagnostics

    def submit(self, key, action):
        with self.lock:
            for job in self.items.values():
                if job['key'] == key and job['state'] in ('queued', 'running'):
                    return job['id']
            active = sum(j['state'] in ('queued', 'running') for j in self.items.values())
            if active >= 8:
                abort(429, '已有较多任务，请稍后重试')
            while len(self.items) >= 80:
                removable = next((i for i, j in self.items.items() if j['state'] not in ('queued', 'running')), None)
                if removable is None:
                    break
                del self.items[removable]
            job_id = secrets.token_urlsafe(18)
            job = dict(id=job_id, key=key, state='queued', status='等待前面的任务', text='',
                       result=None, error='', createdAt=time.time(), control=llm.RequestControl())
            self.items[job_id] = job

        def update(**values):
            with self.lock:
                job.update(values)

        def run():
            started = time.monotonic()
            def record(event, **extra):
                if self.diagnostics:
                    self.diagnostics.write(dict(event=event, job=job_id, kind=key[0],
                        elapsedMs=round((time.monotonic()-started)*1000), **extra))
            try:
                update(state='running', status='准备中')
                record('job.started')
                result = action(update, job['control'])
                update(state='done', status='已完成', result=result)
                record('job.done')
            except llm.RequestCancelled:
                update(state='stopped', status='已停止')
                record('job.stopped')
            except Exception as error:
                update(state='error', error=clean_error(error), status='任务未完成')
                record('job.error', errorName=type(error).__name__)
        if self.diagnostics:
            self.diagnostics.write(dict(event='job.submitted', job=job_id, kind=key[0]))
        self.pool.submit(run)
        return job_id

    def public(self, job_id):
        with self.lock:
            job = self.items.get(job_id)
            if not job:
                abort(404, '任务已过期，请刷新页面查看已保存结果')
            return {k: v for k, v in job.items() if k not in ('key', 'control')}


class HostCookieSessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):
        hostname = urlsplit(request.host_url).hostname or ''
        return super().get_cookie_secure(app) or hostname.lower() in app.config['HTTPS_HOSTS']


def create_app(allowed_hosts=(), secure_cookie=False, https_hosts=(), cloudflare_hosts=()):
    root = config.app_dir()
    auth_file = root / 'web-auth.json'
    if not auth_file.exists():
        code = secrets.token_urlsafe(12)
        store._atomic_json_write(auth_file, dict(secret=secrets.token_hex(32), passwordHash=generate_password_hash(code)))
        (root / 'access-code.txt').write_text(code + '\n', encoding='utf-8')
    auth = json.loads(auth_file.read_text(encoding='utf-8'))
    app = Flask(__name__, static_folder=None)
    app.request_class = UploadRequest
    app.config.update(SECRET_KEY=auth['secret'], MAX_CONTENT_LENGTH=REQUEST_BYTES,
                      UPLOAD_FILE_BYTES=FILE_BYTES, UPLOAD_DAILY_BYTES=DAILY_BYTES,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      SESSION_COOKIE_SECURE=secure_cookie, SESSION_COOKIE_NAME='kandu_web',
                      HTTPS_HOSTS=frozenset(h.lower() for h in (*https_hosts, *cloudflare_hosts)),
                      CLOUDFLARE_HOSTS=frozenset(h.lower() for h in cloudflare_hosts),
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    app.session_interface = HostCookieSessionInterface()
    hosts = {'localhost', '127.0.0.1', '::1', socket.gethostname().lower(), *[h.lower() for h in allowed_hosts]}
    hosts.update(r[4][0] for r in socket.getaddrinfo(socket.gethostname(), None))
    hosts.update(app.config['HTTPS_HOSTS'])
    diagnostics = Diagnostics(root)
    app.extensions['diagnostics'] = diagnostics
    jobs = Jobs(diagnostics)
    app.extensions['jobs'] = jobs
    data_lock = threading.RLock()
    book_locks = {}
    analysis_locks = [threading.Lock() for _ in range(64)]
    login_limiter = LoginLimiter(root / 'login-rate.sqlite3', auth['secret'])
    config_access = ConfigAccess(root, auth['secret'])
    config_limiter = LoginLimiter(root / 'config-access.sqlite3', auth['secret'], limit=5)
    upload_quota = UploadQuota(root / 'upload-quota.sqlite3')
    password_slots = threading.BoundedSemaphore(2)
    podcast_jobs = {}
    started = time.time()
    cache_scope = hashlib.sha256(('reading-cache-v1:' + auth['secret']).encode()).hexdigest()[:24]

    def reading_identity():
        settings = config.settings_path()
        revision = str(settings.stat().st_mtime_ns) if settings.exists() else '0'
        return dict(cacheScope=cache_scope, modelRevision=hashlib.sha256(revision.encode()).hexdigest()[:16],
                    serverTime=int(time.time()*1000))

    def content_version(art):
        return hashlib.sha256(json.dumps(art, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]

    def check_content_version(art, data):
        if data.get('contentVersion') and data['contentVersion'] != content_version(art):
            abort(400, '文章正文已更新，请刷新后继续同步阅读记录')

    def book(book_id):
        if not BOOK_ID.fullmatch(book_id) or book_id in ('.', '..'):
            abort(404, '找不到刊物')
        meta = podcast_library.metadata(book_id) or store.get_book(book_id)
        if meta is None:
            abort(404, '找不到刊物')
        return meta

    def article(book_id, index):
        meta = book(book_id)
        if meta.get('sourceType') == 'podcast-series':
            abort(404, '请从播客目录选择一期节目')
        art = next((a for a in store.ensure_content(meta)['articles'] if a['index'] == index), None)
        if art is None:
            abort(404, '找不到文章')
        return meta, art

    def word_source(meta, art, paragraph):
        if paragraph is not None and (type(paragraph) is not int or not -1 <= paragraph < len(art['paragraphs'])):
            abort(400, '段落编号无效')
        context = art['title'] if paragraph == -1 else art['paragraphs'][paragraph] if paragraph is not None else ''
        return dict(bookId=meta['id'], bookTitle=meta.get('title') or meta['fileName'], articleIndex=art['index'],
            articleTitle=art['title'], paragraph=paragraph, context=context[:800], recordedAt=int(time.time() * 1000))

    def payload():
        if not request.is_json:
            abort(415, '请求需要 JSON 格式')
        value = request.get_json()
        if not isinstance(value, dict):
            abort(400, '无效请求')
        return value

    def model(data):
        settings = config.load_settings()
        name = data.get('modelName')
        cfg = config.get_llm(settings, name)
        if name and name not in config.llm_names(settings):
            abort(400, '模型配置已改变，请刷新')
        if error := llm.validate(cfg):
            abort(400, error + '，请在设置中配置模型')
        return dict(cfg)

    def lock_for(book_id):
        with data_lock:
            return book_locks.setdefault(book_id, threading.Lock())

    def accepted(job_id):
        return jsonify(jobId=job_id), 202

    def config_expiry():
        return config_access.expires(session.get('configGrant', ''), session.get('csrf', ''))

    def require_config_access():
        if not config_expiry():
            return jsonify(error='请先输入独立密码解锁模型配置', code='config_locked'), 403

    def client_address():
        try:
            return login_address(request.remote_addr or '', urlsplit(request.host_url).hostname or '',
                                 app.config['CLOUDFLARE_HOSTS'], request.headers.get('CF-Connecting-IP', ''))
        except ClientAddressError as error:
            abort(400, str(error))

    def prepare_upload():
        request.max_content_length = app.config['UPLOAD_FILE_BYTES'] + 64*1024
        request.max_form_parts = 1
        request.max_form_memory_size = 256*1024
        size = request.content_length
        if size is None or size <= 0:
            abort(411, '上传必须提供请求大小')
        if size > request.max_content_length:
            abort(413, '单个文件不得超过 50 MB')
        if request.mimetype != 'multipart/form-data':
            abort(415, '请使用文件上传表单')
        declared = request.headers.get('X-Upload-Size')
        if declared is not None:
            if not re.fullmatch(r'[0-9]{1,12}', declared):
                abort(400, '无效的上传文件大小')
            reserved = int(declared)
            if reserved > app.config['UPLOAD_FILE_BYTES']:
                abort(413, '单个文件不得超过 50 MB')
            if reserved > size:
                abort(400, '文件大小与请求不符')
            if size > reserved + 64*1024:
                abort(400, '上传请求大小与文件声明不符')
        else:
            # Compatibility with older clients: reserve a safe upper bound.
            # New clients declare file.size, which the stream then enforces.
            reserved = min(size, app.config['UPLOAD_FILE_BYTES'])
        try:
            ticket = upload_quota.reserve(reserved, app.config['UPLOAD_DAILY_BYTES'])
        except UploadQuotaExceeded as error:
            return jsonify(error=str(error), code='upload_quota_exceeded'), 429
        except sqlite3.Error:
            return jsonify(error='上传额度服务暂时不可用，请稍后重试'), 503
        request.environ['kandu.upload'] = UploadState(ticket, reserved, upload_quota,
                                                     app.config['UPLOAD_FILE_BYTES'], app.config['UPLOAD_DAILY_BYTES'])

    @app.before_request
    def guard():
        hostname = urlsplit(request.host_url).hostname
        if not hostname or hostname.lower() not in hosts:
            abort(400, '此访问地址尚未在服务端启用')
        if request.path == '/api/reading-sync/import' and request.method == 'POST':
            if request.content_length is None or request.content_length > 128 * 1024:
                abort(413, '同步数据过大')
            credential = request.headers.get('Authorization', '')
            saved = store._json_read(root / 'reading-sync-auth.json') or {}
            supplied = hashlib.sha256(credential[7:].encode()).hexdigest() if credential.startswith('Bearer ') else ''
            if not saved.get('tokenHash') or not secrets.compare_digest(supplied, saved['tokenHash']):
                abort(401, '阅读同步码无效，请在 Web 设置中重新生成')
            origin = request.headers.get('Origin', '')
            if origin and not origin.startswith('chrome-extension://') and origin.rstrip('/') != request.host_url.rstrip('/'):
                abort(403, '拒绝跨站请求')
            return
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin:
                source = urlsplit(origin)
                if source.netloc.lower() != request.host.lower() or source.scheme not in ('http', 'https'):
                    abort(403, '拒绝跨站请求')
        if request.path.startswith('/api/') and request.path not in ('/api/session', '/api/login', '/api/health'):
            if not session.get('authenticated'):
                abort(401, '请先输入访问密码')
            if request.method not in ('GET', 'HEAD'):
                if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), session.get('csrf', 'missing')):
                    abort(403, '会话已失效，请刷新页面后重试')
        if request.path == '/api/settings':
            if denied := require_config_access():
                return denied
        if request.path == '/api/books' and request.method == 'POST':
            return prepare_upload()

    @app.teardown_request
    def finish_upload(error):
        state = request.environ.get('kandu.upload')
        if state is None or state.finished:
            return
        state.finished = True
        for stream in state.streams:
            stream.close()
        try:
            upload_quota.finish(state.ticket, state.received)
        except (sqlite3.Error, ValueError):
            # Keep the larger reservation charged when accounting fails.
            app.logger.error('Upload accounting unavailable; reservation retained')

    @app.after_request
    def headers(response):
        response.headers.update({'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin',
            'X-Frame-Options': 'SAMEORIGIN', 'Cache-Control': 'no-store',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'"})
        if request.method in ('GET', 'HEAD') and response.status_code in (200, 304):
            if request.endpoint == 'static':
                version = hashlib.sha256((STATIC / request.view_args['name']).read_bytes()).hexdigest()[:16]
                response.headers['X-Kandu-Asset-Version'] = version
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable' if request.args.get('v') == version else 'no-cache'
            elif request.endpoint == 'cover':
                response.headers['Cache-Control'] = 'private, max-age=3600'
            elif response.status_code == 200 and request.endpoint in ('library', 'digest', 'read', 'vocabulary'):
                response.set_etag(hashlib.sha256(response.get_data()).hexdigest())
                response.headers['Cache-Control'] = 'private, no-cache'
                response.make_conditional(request)
        return response

    @app.errorhandler(Exception)
    def failed(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        if isinstance(error, (ValueError, FileNotFoundError)):
            return jsonify(error=clean_error(error)), 400
        app.logger.error('Request failed: %s', type(error).__name__)
        return jsonify(error='服务器处理失败，请重试或查看服务器运行日志'), 500

    @app.get('/')
    def index():
        response = app.response_class(pwa.index(STATIC), mimetype='text/html')
        response.headers['X-Kandu-Shell-Version'] = pwa.bundle(STATIC)[0]
        return response

    @app.get('/manifest.webmanifest')
    def manifest():
        response = send_file(STATIC / 'manifest.webmanifest', mimetype='application/manifest+json')
        response.headers['X-Kandu-Asset-Version'] = pwa.asset_version(STATIC, 'manifest.webmanifest')
        return response

    @app.get('/sw.js')
    def service_worker():
        response = app.response_class(pwa.worker(STATIC), mimetype='application/javascript')
        response.headers['Service-Worker-Allowed'] = '/'
        return response

    @app.get('/static/<name>')
    def static(name):
        if name not in pwa.ASSETS:
            abort(404)
        return send_file(STATIC / name)

    @app.get('/api/health')
    def health():
        return jsonify(status='ok', version=__version__, startedAt=started)

    @app.get('/api/session')
    def get_session():
        return jsonify(authenticated=bool(session.get('authenticated')), csrf=session.get('csrf'), version=__version__,
                       **(reading_identity() if session.get('authenticated') else {}))

    @app.post('/api/login')
    def login():
        request.max_content_length = 8 * 1024
        if request.content_length is not None and request.content_length > request.max_content_length:
            abort(413, '登录请求过大')
        address = client_address()
        # Bound expensive password hashing without a global failure counter
        # that could lock every visitor out. Existing sessions bypass login.
        if not password_slots.acquire(blocking=False):
            return jsonify(error='登录验证繁忙，请稍后重试', retryAfter=2), 503, {'Retry-After': '2'}
        try:
            try:
                ticket, retry = login_limiter.reserve(address)
                if retry:
                    return jsonify(error=f'此网络登录尝试过多，请 {retry} 秒后再试', retryAfter=retry), 429, {'Retry-After': str(retry)}
                data = payload()
                password = data.get('password', '')
                if not isinstance(password, str) or len(password) > 200 or not check_password_hash(auth['passwordHash'], password):
                    abort(401, '访问密码不正确')
                login_limiter.success(address, ticket)
            except sqlite3.Error:
                return jsonify(error='登录防护暂时不可用，请稍后重试', retryAfter=2), 503, {'Retry-After': '2'}
        finally:
            password_slots.release()
        config_access.revoke(session.get('configGrant', ''))
        session.clear()
        session.update(authenticated=True, csrf=secrets.token_urlsafe(24))
        session.permanent = True
        return jsonify(csrf=session['csrf'], **reading_identity())

    @app.post('/api/logout')
    def logout():
        config_access.revoke(session.get('configGrant', ''))
        session.clear()
        return jsonify(ok=True)

    @app.get('/api/settings/access')
    def settings_access():
        expires = config_expiry()
        return jsonify(configured=bool(config_access.password_hash()), unlocked=bool(expires), expiresAt=int(expires*1000))

    @app.post('/api/settings/unlock')
    def unlock_settings():
        request.max_content_length = 8*1024
        if request.content_length is not None and request.content_length > request.max_content_length:
            abort(413, '验证请求过大')
        address = client_address()
        password_hash = config_access.password_hash()
        if not password_hash:
            return jsonify(error='管理员尚未设置独立密码，请在服务器运行 tools/set_config_password.py'), 503
        if not password_slots.acquire(blocking=False):
            return jsonify(error='密码验证繁忙，请稍后重试'), 503, {'Retry-After': '2'}
        try:
            try:
                ticket, retry = config_limiter.reserve(address)
                if retry:
                    return jsonify(error=f'独立密码尝试过多，请 {retry} 秒后再试', retryAfter=retry), 429, {'Retry-After': str(retry)}
                password = payload().get('password', '')
                if not isinstance(password, str) or len(password)>200 or not check_password_hash(password_hash, password):
                    return jsonify(error='独立密码不正确'), 403
                config_limiter.success(address, ticket)
                config_access.revoke(session.get('configGrant', ''))
                grant, expires = config_access.issue(session['csrf'], password_hash)
                session['configGrant'] = grant
            except sqlite3.Error:
                return jsonify(error='配置验证暂时不可用，请稍后重试'), 503
        finally:
            password_slots.release()
        return jsonify(unlocked=True, expiresAt=int(expires*1000))

    @app.post('/api/settings/lock')
    def lock_settings():
        config_access.revoke(session.pop('configGrant', ''))
        return jsonify(unlocked=False)

    @app.get('/api/preferences')
    def preferences():
        return jsonify(vocabLevel=config.load_settings()['vocabLevel'])

    @app.post('/api/preferences')
    def save_preferences():
        data = payload()
        if set(data) != {'vocabLevel'} or data['vocabLevel'] not in gen.LEVEL_LABEL:
            abort(400, '未知词汇级别或偏好字段')
        with data_lock:
            current = config.load_settings()
            current['vocabLevel'] = data['vocabLevel']
            store._atomic_json_write(config.settings_path(), current)
        return jsonify(ok=True)

    @app.get('/api/upload-quota')
    def upload_status():
        return jsonify(fileBytes=app.config['UPLOAD_FILE_BYTES'], **upload_quota.status(app.config['UPLOAD_DAILY_BYTES']))

    @app.get('/api/library')
    def library():
        books, recent = [], []
        library = store.load_library()
        digests = {meta['id']: store.load_digest(meta['id']) or {} for meta in library}
        counts = article_duplicates.library_counts(library, digests)
        for meta in library:
            publication = meta.get('publication') or re.sub(r'[_ .-]*\d{4}.*$', '', meta['fileName']).replace('_', ' ').strip() or '其他刊物'
            card = dict(id=meta['id'], fileName=meta['fileName'], ext=meta['ext'], publication=publication,
                title=meta.get('title', ''), sourceType=meta.get('sourceType', ''),
                episodeNumber=meta.get('episodeNumber', 0), publishedAt=meta.get('publishedAt', ''),
                **counts[meta['id']],
                cover=store.has_cover(meta['id']) or meta.get('sourceType') == 'podcast',
                available=Path(meta['path']).is_file())
            if not podcast_library.source_id(meta):
                books.append(card)
            for idx, record in store.load_reading_history(meta['id']).items():
                recent.append(dict(record, bookId=meta['id'], articleIndex=int(idx), bookTitle=meta.get('title') or meta['fileName']))
        for source_id in dict.fromkeys(filter(None, (podcast_library.source_id(m) for m in library))):
            meta = podcast_library.metadata(podcast_library.PREFIX + source_id, library)
            books.append(podcast_library.card(meta, podcast_library.episodes(source_id, library), digests))
        recent.sort(key=lambda r: r['lastReadAt'], reverse=True)
        return jsonify(books=books, recent=recent[:80])

    @app.get('/api/podcast-sources')
    def podcast_sources():
        items = []
        with data_lock:
            for spec in podcasts.SOURCES.values():
                saved = podcasts.status(spec['id'])
                job_id = podcast_jobs.get(spec['id'])
                if job_id and job_id not in jobs.items:
                    podcast_jobs.pop(spec['id'], None)
                    job_id = None
                if job_id:
                    job = jobs.public(job_id)
                    saved = dict(saved, state=job['state'], status=job['status']
                        if job['state'] in ('queued', 'running') else saved.get('status') or job['status'])
                elif saved.get('state') == 'running':
                    saved = dict(saved, state='interrupted', status='上次导入已中断，再次导入可补缺')
                items.append(dict(id=spec['id'], name=spec['name'], url=spec['url'], limit=spec['limit'],
                    status=saved, jobId=job_id))
        return jsonify(sources=items)

    @app.post('/api/podcast-sources/<source_id>/import')
    def import_podcast(source_id):
        spec, data = podcasts.source(source_id), payload()
        limit = data.get('limit', spec['limit'])
        if type(limit) is not int or not 1 <= limit <= spec['limit']:
            abort(400, '导入期数应为 1 至 50 的整数')
        transcript_urls = podcasts.validate_selection(spec, data.get('transcriptUrls'))
        cfg = model(data)
        def work(update, control):
            return podcasts.import_source(source_id, limit, cfg, update, control, data_lock, lock_for,
                transcript_urls=transcript_urls)
        with data_lock:
            job_id = jobs.submit(('podcast', source_id), work)
            podcast_jobs[source_id] = job_id
        return accepted(job_id)

    @app.get('/api/reading-stats')
    def completed_reading():
        try:
            days = int(request.args.get('days', '365'))
            offset = int(request.args.get('timezoneOffset', '0'))
        except ValueError:
            abort(400, '统计日期范围无效')
        if days not in (7,30,90,365) or not -840 <= offset <= 720:
            abort(400, '统计日期范围无效')
        with data_lock:
            return jsonify(reading_stats.summary(days=days, timezone_offset=offset))

    @app.get('/api/reading-sync')
    def reading_sync_status():
        saved = store._json_read(root / 'reading-sync-auth.json') or {}
        return jsonify(enabled=bool(saved.get('tokenHash')))

    @app.post('/api/reading-sync')
    def reading_sync_pair():
        token = secrets.token_urlsafe(32)
        with data_lock:
            store._atomic_json_write(root / 'reading-sync-auth.json',
                                     dict(tokenHash=hashlib.sha256(token.encode()).hexdigest()))
        return jsonify(token=token)

    @app.delete('/api/reading-sync')
    def reading_sync_revoke():
        with data_lock:
            store._atomic_json_write(root / 'reading-sync-auth.json', {})
        return jsonify(ok=True)

    @app.post('/api/reading-sync/import')
    def reading_sync_import():
        with data_lock:
            count = reading_stats.import_records(payload().get('articles'))
        return jsonify(accepted=count)

    @app.get('/api/books/<book_id>/cover')
    def cover(book_id):
        meta = book(book_id)
        if meta.get('sourceType') == 'podcast-series':
            return send_file(podcast_covers.ensure_series_cover(podcasts.source(meta['sourceId'])), mimetype='image/jpeg')
        if meta.get('sourceType') == 'podcast':
            podcast_covers.ensure_cover(meta)
        path = store.cover_file(meta['id'])
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype='image/jpeg')

    @app.get('/api/books/<book_id>/file')
    def original_file(book_id):
        meta = book(book_id)
        if meta.get('sourceType') == 'podcast-series':
            abort(404, '请进入单期阅读页面下载该期 Transcript')
        return send_file(meta['path'], mimetype='application/pdf' if meta['ext'] == 'pdf' else 'application/epub+zip',
            as_attachment=meta['ext'] != 'pdf', download_name=meta['fileName'], conditional=True)

    @app.get('/api/books/<book_id>')
    def digest(book_id):
        meta = book(book_id)
        if meta.get('sourceType') == 'podcast-series':
            return jsonify(id=book_id, fileName=meta['fileName'], title=meta['title'], ext=meta['ext'],
                sourceType=meta['sourceType'], sourceUrl=meta['sourceUrl'], excerpted=True,
                articles=podcast_library.directory(meta), duplicateCount=0)
        content = store.ensure_content(meta)
        duplicates = article_duplicates.earlier_occurrences(meta, content['articles'])
        summaries = {a['index']: a for a in (store.load_digest(book_id) or {}).get('articles', [])}
        states, history = store.load_article_states(book_id), store.load_reading_history(book_id)
        tags = article_tags.load(book_id)
        items = [dict(index=a['index'], titleEn=a['title'], titleZh=summaries.get(a['index'], {}).get('titleZh', ''),
            summaryZh=summaries.get(a['index'], {}).get('summaryZh', ''),
            tags=tags.get(str(a['index']), []),
            state=states.get(str(a['index']), 'unread'), history=history.get(str(a['index'])),
            earlierOccurrence=duplicates.get(a['index'])) for a in content['articles']]
        digest = store.load_digest(book_id) or {}
        return jsonify(id=book_id, fileName=meta['fileName'], ext=meta['ext'], articles=items,
                       title=meta.get('title', ''), sourceUrl=meta.get('sourceUrl', ''),
                       sourceType=meta.get('sourceType', ''), excerpted=bool(digest.get('excerpted')),
                       duplicateCount=len(duplicates))

    @app.post('/api/books/<book_id>/articles/<int:index>/tags')
    def save_tags(book_id, index):
        meta, art = article(book_id, index)
        tags = payload().get('tags')
        with data_lock:
            saved = article_tags.save(meta['id'], art['index'], tags)
        return jsonify(tags=saved)

    @app.post('/api/books/<book_id>/state')
    def mark(book_id):
        data = payload()
        index = data.get('articleIndex')
        if type(index) is not int:
            abort(400, '文章编号无效')
        meta, art = article(book_id, index)
        with data_lock:
            result = store.set_article_state(book_id, index, data.get('state'))
            reading_stats.mark_web(meta, art, data.get('state') == 'read')
        return jsonify(states=result)

    @app.get('/api/books/<book_id>/articles/<int:index>')
    def read(book_id, index):
        meta, art = article(book_id, index)
        summary = next((a for a in (store.load_digest(book_id) or {}).get('articles', []) if a['index'] == index), {})
        history = store.load_reading_history(book_id).get(str(index))
        return jsonify(article=art, titleZh=summary.get('titleZh', ''), summaryZh=summary.get('summaryZh', ''),
            contentVersion=content_version(art),
            bookTitle=meta.get('title') or meta['fileName'], ext=meta['ext'], history=history,
            progressServerAt=(history or {}).get('lastReadAt', 0),
            sourceUrl=meta.get('sourceUrl', ''), excerpted=bool((store.load_digest(book_id) or {}).get('excerpted')),
            parentBookId=podcast_library.parent_id(meta),
            readingTiming=reading_activity.article_timing(book_id,index),
            wordCount=len(gen._TOKEN.findall(' '.join(art['paragraphs']))),
            state=store.load_article_states(book_id).get(str(index), 'unread'))

    @app.get('/api/books/<book_id>/articles/<int:index>/progress')
    def read_progress(book_id, index):
        _, art = article(book_id, index)
        with data_lock:
            history = store.load_reading_history(book_id).get(str(index))
            state = store.load_article_states(book_id).get(str(index), 'unread')
        # A small uncached response; polling never downloads prose or vocabulary.
        return jsonify(record=history, state=state, contentVersion=content_version(art))

    @app.post('/api/books/<book_id>/articles/<int:index>/reading-time')
    def reading_time(book_id,index):
        meta,art = article(book_id,index)
        data = payload()
        check_content_version(art, data)
        return jsonify(reading_activity.record(meta,art,data))

    @app.post('/api/books/<book_id>/articles/<int:index>/progress')
    def save_progress(book_id, index):
        meta, art = article(book_id, index)
        data = payload()
        check_content_version(art, data)
        para = data.get('paragraphIndex')
        offset = data.get('paragraphOffset', 0)
        if type(para) is not int or not 0 <= para < len(art['paragraphs']) or type(offset) is not int or offset < 0:
            abort(400, '阅读位置无效')
        offset = min(offset, len(art['paragraphs'][para]))
        progress = (sum(len(p) for p in art['paragraphs'][:para]) + offset) / max(1, sum(len(p) for p in art['paragraphs']))
        with data_lock:
            previous = store.load_reading_history(book_id).get(str(index), {})
            based_on = data.get('basedOnReadAt')
            if based_on is not None:
                if type(based_on) is not int or based_on < 0:
                    abort(400, '阅读记录基准无效')
                if based_on != previous.get('lastReadAt', 0):
                    return jsonify(record=previous, accepted=False)
            observed = data.get('observedAt')
            if observed is not None:
                now = int(time.time()*1000)
                if type(observed) is not int or not now-7*86_400_000 <= observed <= now+30_000:
                    abort(400, '阅读记录时间无效或已超过七天')
                observed = min(observed, now)
                if based_on is None and observed < previous.get('lastReadAt', 0):
                    return jsonify(record=previous, accepted=False)
                if based_on is not None:
                    # A matching baseline proves causal order even when devices'
                    # clock estimates differ due to latency. Keep the revision
                    # strictly increasing, including two updates in the same ms.
                    observed = max(observed, previous.get('lastReadAt', 0) + 1)
            record = store.save_reading_progress(book_id, index, dict(title=art['title'], titleZh='',
                paragraphIndex=para, paragraphOffset=offset, paragraphCount=len(art['paragraphs']),
                paragraphStart=art['paragraphs'][para][:100], wordCount=len(gen._TOKEN.findall(' '.join(art['paragraphs']))),
                progress=progress, atStart=para == 0 and offset == 0, atEnd=progress >= .995,
                completionMode='explicit'), read_at=observed)
            # A saved viewport is navigation, not evidence of completion. Only
            # the explicit state endpoint may credit the whole article.
        return jsonify(record=record)

    @app.get('/api/books/<book_id>/articles/<int:index>/vocab')
    def vocabulary(book_id, index):
        _, art = article(book_id, index)
        level = config.load_settings()['vocabLevel']
        cached = store.load_vocab(book_id, index, level)
        vocab = wordbook.overlay(gen.audit_phrase_coverage(cached, art) if cached else None, art, level)
        missing = [i for i in range(len(art['paragraphs'])) if i not in (cached or {}).get('processedParagraphs', [])]
        return jsonify(vocab=vocab, missing=missing, generated=bool(cached), level=level, contentVersion=content_version(art))

    @app.post('/api/books/<book_id>/articles/<int:index>/lookup')
    def lookup(book_id, index):
        meta, art = article(book_id, index)
        data = payload()
        value = wordbook.normalize(data.get('word', ''))
        para = data.get('paragraph', 0)
        if type(para) is not int or not 0 <= para < len(art['paragraphs']):
            abort(400, '段落编号无效')
        context = word_meaning.selection(art, para, value, data.get('start'))
        core_dict.ensure_db(background=True)
        result = core_dict.lookup(value) or {}
        source = dict(word_source(meta, art, para), selectedWord=value, selectedStart=data.get('start'))
        saved = wordbook.remember_lookup(value, result, source)
        cfg = config.get_llm(config.load_settings())
        meaning = word_meaning.cached(book_id, context, cfg)
        if meaning:
            saved = wordbook.attach_context(saved['word'], meaning, saved['source']) or saved
        return jsonify(result=result, saved=saved, contextMeaning=meaning, canExplain=llm.is_complete(cfg))

    @app.post('/api/books/<book_id>/articles/<int:index>/lookup-context')
    def lookup_context(book_id, index):
        _, art = article(book_id, index)
        data = payload()
        context = word_meaning.selection(art, data.get('paragraph'), data.get('word'), data.get('start'))
        cfg = model(data)
        record = wordbook.get(data.get('savedWord') or context['word'])
        source = record['source'] if record else None
        # Only attach to the lookup that actually requested this occurrence.
        if source and (source.get('bookId') != book_id or source.get('articleIndex') != index or source.get('paragraph') != data['paragraph']):
            source = None
        if record and context['word'] not in [record['word'], *record['aliases']]:
            source = None
        def work(update, control):
            meaning = word_meaning.explain(book_id, context, cfg, update, control)
            saved = None
            if source is not None:
                with data_lock:
                    saved = wordbook.attach_context(record['word'], meaning, source)
            return dict(meaning=meaning, saved=saved)
        return accepted(jobs.submit(('word-meaning', book_id, word_meaning.cache_key(context, cfg)), work))

    @app.post('/api/books/<book_id>/articles/<int:index>/collect')
    def collect_word(book_id, index):
        meta, art = article(book_id, index)
        data = payload()
        if not isinstance(data.get('entry'), dict):
            abort(400, '词条无效')
        saved = wordbook.remember_entry(data['entry'], word_source(meta, art, data.get('paragraph')))
        return jsonify(word=saved)

    @app.get('/api/wordbook')
    def words():
        return jsonify(words=wordbook.list_words(request.args.get('q', '')[:200]))

    @app.post('/api/wordbook')
    def save_word():
        data = payload()
        return jsonify(word=wordbook.save(data, original=data.get('original')))

    @app.delete('/api/wordbook/<word>')
    def delete_word(word):
        return jsonify(deleted=wordbook.delete(word))

    @app.get('/api/settings')
    def settings():
        data = config.load_settings()
        return jsonify(vocabLevel=data['vocabLevel'], models=[dict(name=p.get('name', ''), model=p.get('model', ''),
            provider=p.get('provider', 'openai'), baseUrl=p.get('baseUrl', ''),
            thinkingLevel=p.get('thinkingLevel', 'auto'),
            hasApiKey=bool(p.get('apiKey')), configured=llm.is_complete(p)) for p in data.get('llms', [])],
            defaultModel=config.default_llm_name(data), version=__version__)

    @app.post('/api/settings')
    def save_settings():
        data = payload()
        with data_lock:
            current = config.load_settings()
            if 'modelConfig' in data:
                if data.get('configJson'):
                    abort(400, '请分别保存配置编辑或 JSON 导入')
                original, changes = data.get('originalName'), data['modelConfig']
                if not isinstance(original, str) or not original or not isinstance(changes, dict):
                    abort(400, '无效的模型配置')
                index = next((i for i, p in enumerate(current['llms']) if p.get('name') == original), None)
                if index is None:
                    abort(404, '模型配置已改变，请刷新后重试')
                cfg = dict(current['llms'][index])
                for key, limit in [('name', 80), ('provider', 20), ('baseUrl', 4000), ('model', 200), ('apiKey', 8192),
                                   ('thinkingLevel', 20)]:
                    if key not in changes:
                        continue
                    value = changes[key]
                    if not isinstance(value, str) or len(value) > limit:
                        abort(400, '模型配置字段格式或长度不正确')
                    value = value.strip()
                    if key != 'apiKey' or value:
                        cfg[key] = value
                if not cfg.get('name'):
                    abort(400, '配置名称不能为空')
                if cfg.get('provider', 'openai') not in ('openai', 'anthropic'):
                    abort(400, '未知接口类型')
                if cfg.get('thinkingLevel', 'auto') not in llm.THINKING_LEVELS:
                    abort(400, '未知思考强度')
                if cfg['name'] != original and cfg['name'] in config.llm_names(current):
                    abort(409, '已有同名配置，请使用其他名称')
                if error := llm.validate(cfg):
                    abort(400, error)
                current['llms'][index] = cfg
                if current.get('defaultLlm') == original:
                    current['defaultLlm'] = cfg['name']
            if 'vocabLevel' in data:
                if data['vocabLevel'] not in gen.LEVEL_LABEL:
                    abort(400, '未知词汇级别')
                current['vocabLevel'] = data['vocabLevel']
            if data.get('configJson'):
                parsed = llm.parse_pasted_json(str(data['configJson'])[:40000])
                if not parsed:
                    abort(400, '无法识别配置 JSON')
                cfg, _ = parsed
                level = data.get('thinkingLevel', 'auto')
                if not isinstance(level, str) or level not in llm.THINKING_LEVELS:
                    abort(400, '未知思考强度')
                cfg['thinkingLevel'] = level
                if error := llm.validate(cfg):
                    abort(400, error)
                name = str(data.get('name') or cfg['model']).strip()[:80]
                current['llms'] = [p for p in current['llms'] if p.get('name') != name] + [dict(cfg, name=name)]
                current['defaultLlm'] = name
            if data.get('defaultModel'):
                if data['defaultModel'] not in config.llm_names(current):
                    abort(400, '找不到模型')
                current['defaultLlm'] = data['defaultModel']
            store._atomic_json_write(config.settings_path(), current)
        return jsonify(ok=True)

    @app.post('/api/books/<book_id>/generate-digest')
    def generate_digest(book_id):
        meta, cfg = book(book_id), model(payload())
        def work(update, control):
            with lock_for(book_id):
                if meta.get('sourceType') == 'podcast-series':
                    entries = podcast_library.episodes(meta['sourceId'])
                    failures = []
                    for i, episode in enumerate(entries):
                        control.check()
                        prefix = f'整期摘要 {i+1}/{len(entries)} · '
                        update(status=prefix+episode['title'])
                        try:
                            with lock_for(episode['id']):
                                podcasts.generate_digest(episode, cfg,
                                    lambda **v:update(status=prefix+v.get('status', '')), control)
                        except (ValueError, llm.LlmError):
                            failures.append(episode['id'])
                    if failures:
                        raise ValueError(f'{len(failures)} 期摘要待补缺，已完成的摘要已保留，可再次重试')
                    return dict(bookId=book_id)
                if meta.get('sourceType') == 'podcast':
                    podcasts.generate_digest(meta, cfg, update, control)
                    return dict(failed=0)
                value = gen.generate_digest(meta, cfg,
                    on_progress=lambda done, total: update(status=f'摘要已完成 {done}/{total} 批'),
                    on_chars=lambda chars: update(status=f'摘要正在接收 · {chars} 字'))
                return dict(failed=value.get('failedBatches', 0))
        return accepted(jobs.submit(('digest', book_id), work))

    @app.post('/api/books/<book_id>/articles/<int:index>/generate-vocab')
    def generate_vocab(book_id, index):
        meta, art = article(book_id, index)
        data = payload()
        cfg = model(data)
        settings = config.load_settings()
        phrases = data.get('phrasesOnly') is True
        para = data.get('paragraph')
        if para is not None and (type(para) is not int or not 0 <= para < len(art['paragraphs'])):
            abort(400, '段落编号无效')
        def work(update, control):
            with lock_for(book_id):
                value = gen.generate_vocab(meta, art, settings['vocabLevel'], cfg,
                    on_progress=lambda done, total, name=None: update(status=f'已完成 {done}/{total} 块 · {name or cfg.get("name", "")}'),
                    on_chars=lambda chars: update(status=f'正在接收词表 · {chars} 字'),
                    llm_rotation=settings['llms'], phrases_only=phrases, only_para=para)
                return dict(failed=value.get('failedPhraseChunks' if phrases else 'failedChunks', 0))
        return accepted(jobs.submit(('vocab', book_id, index, settings['vocabLevel'], phrases, para), work))

    @app.post('/api/books/<book_id>/articles/<int:index>/analyze')
    def analyze(book_id, index):
        meta, art = article(book_id, index)
        data = payload()
        sentence = str(data.get('sentence', '')).strip()
        para = data.get('paragraph')
        if type(para) is not int or not 0 <= para < len(art['paragraphs']) or not sentence or len(sentence) > 5000 or sentence not in art['paragraphs'][para]:
            abort(400, '请选择同一段落中的原文句子')
        if any(type(data.get(k, False)) is not bool for k in ('force', 'deep')):
            abort(400, '讲解模式无效')
        try:
            context = core_sentence.context(art, para, sentence, data.get('start'))
        except ValueError as error:
            abort(400, str(error))
        cfg = model(data)
        identity = core_sentence.identity(context, cfg)
        force, deep = data.get('force', False), data.get('deep', False)
        def work(update, control):
            # A sentence must not wait on the book's long vocabulary/digest lock.
            with analysis_locks[int(identity[:8], 16) % len(analysis_locks)]:
                control.check()
                return gen.analyze(meta, art, sentence, '', '', cfg,
                    context=context, force=force, deep=deep, cache_identity=identity, control=control,
                    on_partial=lambda value: update(partial=value), on_progress=lambda p: update(
                        status=(f'正在接收讲解 · {p["chars"]} 字' if p['chars'] else
                                ('等待深度复核' if deep else '等待模型响应')) + f' · {p["elapsedMs"] / 1000:.0f} 秒'))
        return accepted(jobs.submit(('analysis', book_id, index, identity, force, deep), work))

    @app.get('/api/books/<book_id>/articles/<int:index>/discussion')
    def messages(book_id, index):
        meta, art = article(book_id, index)
        return jsonify(messages=discussion.load(meta, art))

    @app.post('/api/books/<book_id>/articles/<int:index>/discussion')
    def ask(book_id, index):
        meta, art = article(book_id, index)
        data = payload()
        question = str(data.get('question', '')).strip()
        quote = str(data.get('quote', '')).strip()
        para = data.get('paragraph')
        if not question or len(question) > 6000 or len(quote) > 5000:
            abort(400, '请输入问题，问题最多 6000 字，引用最多 5000 字')
        if quote and (type(para) is not int or not 0 <= para < len(art['paragraphs']) or quote not in art['paragraphs'][para]):
            abort(400, '引用内容与段落不匹配，请重新选择')
        cfg = model(data)
        def work(update, control):
            with lock_for(book_id):
                control.check()
                history = discussion.load(meta, art)
                system, request_messages, excerpted = discussion.build_request(meta, art, history, question, quote, para)
                history.extend([dict(role='user', content=question, status='complete', quote=quote,
                    paragraph=para, createdAt=time.time()), dict(role='assistant', content='', status='pending',
                    model=cfg.get('name', ''), createdAt=time.time())])
                discussion.save(meta, art, history)
                response, last_saved = history[-1], time.monotonic()
                def delta(text):
                    nonlocal last_saved
                    response['content'] = text
                    update(text=response['content'], status=f'正在回答 · {len(response["content"])} 字')
                    if time.monotonic() - last_saved > 2:
                        discussion.save(meta, art, history)
                        last_saved = time.monotonic()
                try:
                    update(status='等待模型回答', excerpted=excerpted)
                    llm.conversation(cfg, system, request_messages, on_text=delta, control=control)
                    response['status'] = 'complete'
                except llm.RequestCancelled:
                    response['status'] = 'stopped'
                    raise
                except Exception as error:
                    response.update(status='error', error=clean_error(error))
                    raise
                finally:
                    discussion.save(meta, art, history)
                return dict(excerpted=excerpted)
        return accepted(jobs.submit(('discussion', book_id, index, question, quote, para), work))

    @app.post('/api/diagnostics')
    def reader_diagnostics():
        if request.content_length is None or request.content_length > 12 * 1024:
            abort(413, '诊断数据过大')
        if not diagnostics.reserve():
            return jsonify(error='诊断请求过多'), 429
        body = request.get_json()
        events = body.get('events') if isinstance(body, dict) else None
        if not isinstance(events, list) or not 1 <= len(events) <= 12:
            abort(400, '诊断格式无效')
        try:
            rows = [clean_event(event) for event in events]
        except ValueError:
            abort(400, '诊断格式无效')
        for row in rows:
            diagnostics.write(dict(origin='browser', **row))
        return jsonify(accepted=len(rows))

    @app.get('/api/jobs/<job_id>')
    def job(job_id):
        return jsonify(jobs.public(job_id))

    @app.post('/api/jobs/<job_id>/stop')
    def stop(job_id):
        jobs.public(job_id)
        with jobs.lock:
            current = jobs.items[job_id]
            if current['key'][0] not in ('discussion', 'podcast'):
                abort(400, '此任务暂不支持中途停止')
            current['control'].cancel()
        return jsonify(ok=True)

    @app.post('/api/books')
    def upload():
        file = request.files.get('file')
        state = request.environ['kandu.upload']
        declared = request.headers.get('X-Upload-Size')
        if declared is not None and state.received != int(declared):
            abort(400, '文件实际大小与声明值不一致')
        if not file or not file.filename:
            abort(400, '请选择 EPUB 或 PDF 文件')
        name = Path(file.filename.replace('\\', '/')).name[:180]
        ext = Path(name).suffix.lower()
        if ext not in ('.epub', '.pdf'):
            abort(400, '仅支持 EPUB 和 PDF')
        bid = store.sanitize_id(Path(name).stem)
        if not BOOK_ID.fullmatch(bid) or bid in ('.', '..'):
            abort(400, '刊物文件名无效')
        if store.get_book(bid):
            abort(409, '同名刊物已在书库中')
        staging = root / 'uploads' / secrets.token_hex(12)
        staging.mkdir(parents=True)
        path = staging / name
        try:
            file.save(path)
        except Exception:
            shutil.rmtree(staging)
            raise
        def work(update, control):
            try:
                update(status='正在解析刊物和提取封面')
                if ext == '.epub':
                    _, articles = store.epub_parse(str(path))
                else:
                    articles = store.pdf_parse(str(path))
                if not articles:
                    raise ValueError('没有找到可阅读的正文')
                with data_lock:
                    if store.get_book(bid):
                        raise ValueError('同名刊物已被其他设备添加')
                    meta, _ = store.add_book(str(path), str(root / 'books'))
                store.ensure_content(meta)
                store.ensure_cover(meta)
                return dict(bookId=meta['id'])
            finally:
                shutil.rmtree(staging)
        try:
            return accepted(jobs.submit(('upload', bid, secrets.token_hex(8)), work))
        except Exception:
            shutil.rmtree(staging)
            raise

    config.ensure_wordlists()
    return app
