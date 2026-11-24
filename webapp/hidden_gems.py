"""Official Hidden Gems catalog and bounded audio downloads for local transcription."""
import hashlib
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from kandu import core_config as config
from kandu import core_parsers as store

MAX_AUDIO_BYTES = 150 * 1024**2
AUDIO_HOSTS = {'traffic.megaphone.fm', 'dcs-cached.megaphone.fm', 'dcs.megaphone.fm'}


def episode_url(spec, url):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.netloc != spec['host'] or p.query or p.fragment or
            not re.fullmatch(r'/podcasts/hidden-gems/\d{4}-\d{2}-\d{2}-[a-zA-Z0-9-]+/?', p.path)):
        raise ValueError('请选择 Hidden Gems 官网的单期节目地址')
    return url.rstrip('/')


def audio_url(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.netloc not in AUDIO_HOSTS or p.fragment or
            not re.fullmatch(r'/[A-Za-z0-9_-]+\.mp3', p.path)):
        raise ValueError('音频地址不属于已支持的官方托管服务')
    return url


def catalog(spec, html, limit):
    soup = BeautifulSoup(html, 'html.parser')
    items, seen = [], set()
    for audio in soup.select('#episodes-list audio[src]'):
        row = audio.find_parent(lambda tag: tag.select_one('h4 a[href]') is not None)
        anchor = row.select_one('h4 a[href]') if row else None
        if anchor is None:
            continue
        url = episode_url(spec, urljoin(spec['url'], anchor['href']))
        if url in seen:
            continue
        # Use the exact Megaphone file embedded by the official site, omitting
        # analytics redirect wrappers. Never accept an arbitrary audio URL.
        match = re.search(r'(?:^https://|/)traffic\.megaphone\.fm/([A-Za-z0-9_-]+\.mp3)(?:\?|$)', audio['src'])
        if not match:
            continue
        seen.add(url)
        date = re.search(r'/hidden-gems/(\d{4}-\d{2}-\d{2})-', url)[1]
        description = row.select_one('.prose')
        description = description.get_text('\n', strip=True) if description else ''
        guests = re.search(r'(?:^|\n)Guests?:\s*([^\n]+)', description)
        title = anchor.get_text(' ', strip=True)
        items.append(dict(id=f'hidden-gems-{hashlib.sha256(url.encode()).hexdigest()[:16]}',
            sourceUrl=url, audioUrl=audio_url('https://traffic.megaphone.fm/'+match[1]),
            title=title, publishedAt=date, episodeNumber=0,
            guest=guests[1][:500] if guests else ''))
    if not items:
        raise ValueError('官网未找到可用的节目音频，请检查目录结构是否变化')
    return sorted(items, key=lambda item: (item['publishedAt'], item['sourceUrl']), reverse=True)[:limit]


def download(episode, control, update):
    """Persist only a complete bounded file, checking every redirect first."""
    folder = config.app_dir()/'podcasts'/'audio'/'hidden-gems'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/(episode['id']+'.mp3')
    if path.is_file() and 0 < path.stat().st_size <= MAX_AUDIO_BYTES:
        return path
    partial = path.with_suffix('.mp3.part')
    target = audio_url(episode['audioUrl'])
    started, last_update = time.monotonic(), 0
    try:
        for _ in range(5):
            control.check()
            with requests.get(target, stream=True, timeout=(12, 45), allow_redirects=False,
                    headers={'User-Agent': 'KanduWeb/0.1 (personal podcast reader)'}) as response:
                control.bind(response)
                try:
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = audio_url(urljoin(target, response.headers.get('Location', '')))
                        continue
                    response.raise_for_status()
                    if int(response.headers.get('Content-Length', 0)) > MAX_AUDIO_BYTES:
                        raise ValueError('单期音频超过 150 MB 限制')
                    media_type = response.headers.get('Content-Type', '').split(';')[0].lower()
                    if media_type not in ('audio/mpeg', 'audio/mp3', 'application/octet-stream'):
                        raise ValueError('托管服务未返回 MP3 音频')
                    size = 0
                    with partial.open('wb') as output:
                        for chunk in response.iter_content(256*1024):
                            control.check()
                            size += len(chunk)
                            if size > MAX_AUDIO_BYTES or time.monotonic()-started > 900:
                                raise ValueError('音频下载超过大小或时间限制，请稍后补缺')
                            output.write(chunk)
                            if time.monotonic()-last_update >= 1:
                                update(status=f'正在下载音频 · {size/1024**2:.1f} MB')
                                last_update = time.monotonic()
                    if not size:
                        raise ValueError('音频文件为空')
                    partial.replace(path)
                    return path
                finally:
                    control.unbind()
        raise ValueError('音频重定向次数过多')
    finally:
        partial.unlink(missing_ok=True)


def transcript(episode, control, update):
    from webapp import podcast_transcribe
    value = store._json_read(config.app_dir()/'podcasts'/'transcripts'/(episode['id']+'.json'))
    if not value or not value.get('complete') or not value.get('paragraphs'):
        if not podcast_transcribe.available():
            raise ValueError('尚未安装本地转写依赖，请先安装 requirements-transcription.txt')
        path = download(episode, control, update)
        value = podcast_transcribe.transcribe(path, episode['id'], control, update)
    # The durable machine transcript is saved before audio is removed; failed or
    # cancelled work retains its download so a retry need not fetch it again.
    (config.app_dir()/'podcasts'/'audio'/'hidden-gems'/(episode['id']+'.mp3')).unlink(missing_ok=True)
    return dict(episode, title=f'{episode["publishedAt"]} · {episode["title"]}',
        paragraphs=value['paragraphs'], chapters=[], transcriptType='machine',
        transcriptionModel=value['model'], transcriptionLanguage='en',
        transcriptionSegments=value['segments'])
