"""Local administrator authentication and Windows user-bound secret storage."""
import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 310000).hex()
    return {'salt': salt, 'hash': digest}


def check_password(password, stored):
    return hmac.compare_digest(hash_password(password, stored['salt'])['hash'], stored['hash'])


def dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise ValueError('凭证持久化仅支持 Windows 加密；其他系统请使用环境变量')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]
    buf = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    dst = Blob()
    lib = ctypes.windll.crypt32
    func = lib.CryptUnprotectData if decrypt else lib.CryptProtectData
    func.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    func.restype = wintypes.BOOL
    if not func(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise ValueError('Windows 凭证加密/解密失败，请在原 Windows 用户账户下运行')
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(dst.pbData, ctypes.c_void_p))


class Config:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'config.json'
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.value = json.loads(self.path.read_text('utf-8')) if self.path.exists() else {
            'password': {}, 'retention_days': 90, 'feishu': {}}
        self.initial_password = None
        if not self.value.get('password'):
            self.reset_password()

    def save(self):
        with self.lock:
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.value, ensure_ascii=False, indent=2), 'utf-8')
            if os.name != 'nt':
                os.chmod(temp, 0o600)
            os.replace(temp, self.path)

    def reset_password(self):
        with self.lock:
            password = secrets.token_urlsafe(15)
            self.value['password'] = hash_password(password)
            self.initial_password = password
            self.save()
            note = self.path.parent / '首次登录.txt'
            note.write_text('客资雷达本机管理员\n登录密码：' + password +
                            '\n请登录后修改密码。请勿在聊天中发送本文件或密码。\n', 'utf-8-sig')
            if os.name != 'nt':
                os.chmod(note, 0o600)
            return password

    def verify(self, password):
        with self.lock:
            return check_password(str(password), self.value['password'])

    def change_password(self, old, new):
        with self.lock:
            if not self.verify(old):
                raise ValueError('原密码不正确')
            if not isinstance(new, str) or not 12 <= len(new) <= 128:
                raise ValueError('新密码需要 12–128 个字符')
            self.value['password'] = hash_password(new)
            self.initial_password = None
            self.save()
            note = self.path.parent / '首次登录.txt'
            if note.exists():
                note.unlink()

    def feishu(self):
        with self.lock:
            conf = dict(self.value.get('feishu', {}))
            sealed = conf.pop('sealed', '')
            if sealed:
                conf.update(json.loads(dpapi(base64.b64decode(sealed), True).decode()))
            for key in ('app_id', 'app_secret', 'tenant_access_token', 'app_token', 'table_id'):
                env = os.environ.get('LEAD_RADAR_FEISHU_' + key.upper())
                if env:
                    conf[key] = env
            return conf

    def public(self, lan_enabled=False):
        conf = self.feishu()
        return {'feishu': {'configured': bool(conf.get('app_token') and conf.get('table_id') and
                            (conf.get('tenant_access_token') or (conf.get('app_id') and conf.get('app_secret')))),
                          'app_id': conf.get('app_id', ''), 'app_token': conf.get('app_token', ''),
                          'table_id': conf.get('table_id', ''), 'auto_sync': bool(conf.get('auto_sync')),
                          'has_secret': bool(conf.get('app_secret')), 'has_token': bool(conf.get('tenant_access_token'))},
                'retention_days': self.value.get('retention_days', 90), 'lan_enabled': lan_enabled}

    def source_token(self, uid):
        with self.lock:
            sealed = self.value.get('source_keys', {}).get(str(uid), '')
            return dpapi(base64.b64decode(sealed), True).decode() if sealed else ''

    def save_source_token(self, uid, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or '\n' in token or '\r' in token:
            raise ValueError('API 密钥格式不正确')
        with self.lock:
            keys = self.value.setdefault('source_keys', {})
            keys[str(uid)] = base64.b64encode(dpapi(token.strip().encode())).decode()
            self.save()

    def delete_source_token(self, uid):
        with self.lock:
            self.value.get('source_keys', {}).pop(str(uid), None)
            self.save()

    def update(self, values):
        with self.lock:
            if 'retention_days' in values:
                days = int(values['retention_days'])
                if not 1 <= days <= 3650:
                    raise ValueError('保留天数需要在 1–3650 之间')
                self.value['retention_days'] = days
            if 'feishu' in values:
                given = values['feishu']
                if not isinstance(given, dict):
                    raise ValueError('飞书配置格式错误')
                conf = self.feishu()
                for key in ('app_id', 'app_token', 'table_id'):
                    if key in given:
                        conf[key] = str(given[key]).strip()[:200]
                if 'auto_sync' in given:
                    conf['auto_sync'] = bool(given['auto_sync'])
                for key in ('app_secret', 'tenant_access_token'):
                    if given.get(key):
                        conf[key] = str(given[key]).strip()[:4096]
                if given.get('clear_credentials'):
                    conf.pop('app_secret', None)
                    conf.pop('tenant_access_token', None)
                secret = {k: conf.pop(k) for k in ('app_secret', 'tenant_access_token') if conf.get(k)}
                conf['sealed'] = base64.b64encode(dpapi(json.dumps(secret).encode())).decode() if secret else ''
                self.value['feishu'] = conf
            self.save()
