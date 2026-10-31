"""FARSTAR addon lease client. Only the public Ed25519 verification key is shipped."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ISSUER = 'https://sub.mystics.top/license-api'
PUBLIC_KEY = '11fd735b8e7db7c4a76ea4fbc791a9c60739c7fdb7404b3493ee97548ba261ac'
BOT = 'https://t.me/FARSTAR_VPN_BOT'


class LicenseError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def machine_fingerprint():
    path = Path('/etc/machine-id')
    raw = path.read_bytes().strip() if path.is_file() else socket.gethostname().encode()
    return hashlib.sha256(b'farstar-install-v1:' + raw).hexdigest()


def resource_fingerprint(panel_instance):
    return hashlib.sha256(('farstar-panel-v1:' + panel_instance).encode()).hexdigest()


def verify(envelope, product, installation_id, machine_hash, resource_id, stamp=None):
    try:
        payload = envelope['payload']
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY)).verify(
            base64.b64decode(envelope['signature'], validate=True), canonical(payload))
        stamp = int(time.time()) if stamp is None else stamp
        if (payload['product'] != product or payload['installation_id'] != installation_id
                or payload['machine_hash'] != machine_hash or payload['resource_id'] != resource_id
                or payload['issued_at'] > stamp + 300
                or payload['status'] not in ('active', 'revoked', 'expired')):
            raise ValueError()
        if payload['status'] == 'active' and not (
                payload['issued_at'] <= payload['lease_until'] <= payload['grace_until'] <= payload['expires_at']
                and payload['grace_until'] - payload['issued_at'] <= 72 * 3600):
            raise ValueError()
        return payload
    except Exception as exc:
        raise LicenseError('امضا، محصول یا هویت نصب لایسنس معتبر نیست.') from exc


def request(path, body):
    # Fixed HTTPS issuer; ignore ambient proxy variables and never log credentials.
    req = urllib.request.Request(ISSUER + path, canonical(body), {'Content-Type': 'application/json'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=20) as response:
            data = response.read(131073)
            if len(data) > 131072:
                raise ValueError()
            return json.loads(data)
    except urllib.error.HTTPError as exc:
        # Return only known validation messages, never arbitrary HTML or supplied secrets.
        try:
            details = json.loads(exc.read(8192)).get('detail', '')
        except Exception:
            details = ''
        if isinstance(details, str) and len(details) < 400 and any('\u0600' <= c <= '\u06ff' for c in details):
            raise LicenseError(details) from None
        raise LicenseError('مرجع صدور درخواست را نپذیرفت؛ کلید و محصول انتخابی را بررسی کن.') from None
    except Exception:
        raise LicenseError('مرجع لایسنس فعلاً در دسترس نیست؛ اعتبار امضاشدهٔ قبلی حفظ می‌شود.') from None


class LicenseClient:
    def __init__(self, state_path, product, panel_instance, transport=None):
        if product not in ('zarib', 'atlas'):
            raise ValueError('Unknown product')
        self.path, self.product = Path(state_path), product
        self.machine = machine_fingerprint()
        self.resource = resource_fingerprint(panel_instance)
        self.transport = transport or request
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.last_error = ''
        self.last_check = 0

    def read(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise LicenseError('فایل وضعیت لایسنس خوانده نشد؛ بازیابی را از مدیر بخواه.') from None

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(6) + '.tmp')
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                json.dump(data, output, ensure_ascii=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def status(self):
        result = {'product': self.product, 'status': 'missing', 'can_manage': False, 'issuer_bot': BOT,
                  'last_check': self.last_check, 'last_error': self.last_error}
        with self.lock:
            try:
                data = self.read()
                if not data.get('lease'):
                    return result
                payload = verify(data['lease'], self.product, data['installation_id'], self.machine, self.resource)
                stamp = int(time.time())
                valid = payload['status'] == 'active' and stamp <= min(payload['grace_until'], payload['expires_at'])
                state = payload['status']
                if state == 'active' and stamp > min(payload['grace_until'], payload['expires_at']):
                    state = 'expired'
                result.update(status=state, can_manage=valid, permanent=bool(payload.get('permanent')), expires_at=None if payload.get('permanent') else payload['expires_at'],
                    license_id=payload['license_id'], lease_until=payload['lease_until'],
                    grace_until=payload['grace_until'], offline_grace=valid and stamp > payload['lease_until'])
            except (LicenseError, KeyError, TypeError, ValueError):
                result['status'] = 'invalid'
        return result

    def require_management(self):
        if not self.status()['can_manage']:
            raise LicenseError('لایسنس این افزونه را در @FARSTAR_VPN_BOT بگیر یا تمدید کن. اتصال‌های موجود و محاسبهٔ مصرف ادامه دارند.')

    def activate(self, key, new_installation=False):
        if not isinstance(key, str) or not 20 <= len(key.strip()) <= 80 or not key.strip().startswith('FS-'):
            raise LicenseError('کلید لایسنس معتبر وارد کن.')
        with self.lock:
            data = self.read()
            if new_installation:
                if self.status()['can_manage']:
                    raise LicenseError('نصب فعال است؛ ابتدا انتقال نصب را از مدیر درخواست کن.')
                data = {}
            if not data.get('installation_id'):
                data['installation_id'] = secrets.token_hex(16)
                self.save(data)  # Stable retry identity even if the HTTP response is lost.
            response = self.transport('/activate', {'product': self.product, 'license_key': key.strip(),
                'installation_id': data['installation_id'], 'machine_hash': self.machine, 'resource_id': self.resource})
            verify(response['lease'], self.product, data['installation_id'], self.machine, self.resource)
            token = response['installation_token']
            if not isinstance(token, str) or not 30 <= len(token) <= 100:
                raise LicenseError('پاسخ مرجع لایسنس معتبر نیست.')
            data.update(installation_token=token, lease=response['lease'])
            self.save(data)
            self.last_check, self.last_error = int(time.time()), ''
            return self.status()

    def refresh(self):
        with self.lock:
            try:
                data = self.read()
                if not data.get('installation_token'):
                    return self.status()
                response = self.transport('/heartbeat', {'product': self.product,
                    'installation_id': data['installation_id'], 'installation_token': data['installation_token'],
                    'machine_hash': self.machine, 'resource_id': self.resource})
                verify(response, self.product, data['installation_id'], self.machine, self.resource)
                data['lease'] = response
                self.save(data)
                self.last_check, self.last_error = int(time.time()), ''
            except Exception:
                self.last_error = 'بررسی آنلاین انجام نشد؛ اعتبار امضاشدهٔ قبلی تا مهلت تعیین‌شده حفظ می‌شود.'
            return self.status()

    def start(self):
        if self.thread:
            return
        def run():
            while not self.stop.is_set():
                self.refresh()
                self.stop.wait(300 if self.last_error else 6 * 3600)
        self.thread = threading.Thread(target=run, name='farstar-license', daemon=True)
        self.thread.start()
