"""Public application shell metadata. Personal API responses never enter this bundle."""
import hashlib
import json


ASSETS = ('app.js', 'app.css', 'icon.svg', 'reading-charts.js', 'reading-cache.js', 'pwa.js',
          'icon-180.png', 'icon-192.png', 'icon-512.png', 'icon-maskable-512.png')


def asset_version(root, name):
    return hashlib.sha256((root / name).read_bytes()).hexdigest()[:16]


def bundle(root):
    names = (*ASSETS, 'index.html', 'manifest.webmanifest', 'service-worker.js')
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    urls = ['/static/' + name + '?v=' + asset_version(root, name) for name in ASSETS]
    urls.append('/manifest.webmanifest?v=' + asset_version(root, 'manifest.webmanifest'))
    return digest.hexdigest()[:24], urls


def index(root):
    html = (root / 'index.html').read_text(encoding='utf-8')
    for name in ASSETS:
        html = html.replace('/static/' + name, '/static/' + name + '?v=' + asset_version(root, name))
    return html.replace('/manifest.webmanifest', '/manifest.webmanifest?v=' + asset_version(root, 'manifest.webmanifest'))


def worker(root):
    version, urls = bundle(root)
    return (root / 'service-worker.js').read_text(encoding='utf-8').replace(
        '__KANDU_BUILD__', json.dumps(version)).replace('__KANDU_ASSETS__', json.dumps(urls))
