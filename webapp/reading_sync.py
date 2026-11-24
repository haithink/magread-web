"""Independent upload-only credentials, with transparent legacy-code migration."""
import hashlib
import secrets
import time

from kandu import core_parsers as store


class DeviceLimit(ValueError):
    pass


class SyncDevices:
    def __init__(self, root, lock):
        self.path = root / 'reading-sync-auth.json'
        self.lock = lock

    def _load(self):
        saved = store._json_read(self.path) or {}
        if saved.get('tokenHash'):
            saved = dict(version=2, devices=[dict(id='legacy', name='原有插件（旧同步码）',
                tokenHash=saved['tokenHash'], createdAt=None, lastSyncAt=None,
                lastUploadAt=None, lastBatchCount=0, legacy=True)])
            store._atomic_json_write(self.path, saved)
        return saved.get('devices', [])

    def _save(self, devices):
        store._atomic_json_write(self.path, dict(version=2, devices=devices))

    @staticmethod
    def public(device):
        return {key: device.get(key) for key in ('id', 'name', 'createdAt', 'lastSyncAt',
            'lastUploadAt', 'lastBatchCount', 'legacy')}

    def status(self):
        with self.lock:
            devices = [self.public(d) for d in self._load()]
            return dict(enabled=bool(devices), devices=devices)

    def pair(self, name):
        if not isinstance(name, str) or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError('设备名称应为不超过 80 个字符的单行文字')
        with self.lock:
            devices = self._load()
            if len(devices) >= 50:
                raise DeviceLimit('最多保留 50 个设备同步码，请先停用不再使用的设备')
            token = secrets.token_urlsafe(32)
            device = dict(id=secrets.token_hex(12), name=name.strip() or f'Chrome 设备 {len(devices)+1}',
                tokenHash=hashlib.sha256(token.encode()).hexdigest(), createdAt=int(time.time()*1000),
                lastSyncAt=None, lastUploadAt=None, lastBatchCount=0, legacy=False)
            self._save([*devices, device])
            return dict(token=token, device=self.public(device))

    def authenticate(self, credential):
        if not credential.startswith('Bearer ') or len(credential) > 256:
            return None
        supplied = hashlib.sha256(credential[7:].encode()).hexdigest()
        with self.lock:
            return next((d['id'] for d in self._load()
                if secrets.compare_digest(supplied, d['tokenHash'])), None)

    def revoke(self, device_id=None):
        with self.lock:
            devices = self._load()
            if device_id is not None and not any(d['id'] == device_id for d in devices):
                return False
            self._save([d for d in devices if d['id'] != device_id] if device_id is not None else [])
            return True

    def import_records(self, device_id, action):
        # Recheck under the same lock as import and revocation: an already
        # authorized request must not import after its code has been revoked.
        with self.lock:
            devices = self._load()
            device = next((d for d in devices if d['id'] == device_id), None)
            if device is None:
                return None
            count = action()
            device['lastSyncAt'] = int(time.time()*1000)
            if count:
                device.update(lastUploadAt=device['lastSyncAt'], lastBatchCount=count)
            self._save(devices)
            return count
