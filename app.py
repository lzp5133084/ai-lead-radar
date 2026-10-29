"""Run on Windows: double-click launcher, or python app.py [--lan]."""
import argparse
import ipaddress
import json
import mimetypes
import os
import secrets
import socket
import threading
import time
import webbrowser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from radar.core import csv_export, filtered_leads
from radar.service import Radar

ROOT = Path(__file__).resolve().parent
VERSION = '1.1.0'


def business_profile():
    path = ROOT / 'business-profile.json'
    if not path.exists():
        return {}
    value = json.loads(path.read_text('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('业务设置格式不正确')
    return value


def local_addresses():
    found = {'127.0.0.1', 'localhost', '::1'}
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            addr = item[4][0]
            if ipaddress.ip_address(addr).is_private:
                found.add(addr)
    except OSError:
        pass
    return found


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, radar, public_hosts=None):
        super().__init__(address, Handler)
        self.radar = radar
        self.sessions = {}
        self.failures = {}
        self.lock = threading.RLock()
        # 反代公网部署时，经 --public-host 显式声明的域名同样合法。
        self.allowed_hosts = local_addresses() | set(public_hosts or [])
        self.radar.lan_urls = [f'http://{a}:{self.server_port}' for a in sorted(self.allowed_hosts)
                              if a not in ('127.0.0.1', 'localhost', '::1') and ':' not in a] if radar.lan else []


class Handler(BaseHTTPRequestHandler):
    server_version = 'LeadRadar/1'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, fmt, *args):
        # Never print request contents, URL query or credentials.
        pass

    def send(self, status, content, content_type='application/json; charset=utf-8', extra=None):
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False).encode('utf-8')
        if isinstance(content, str):
            content = content.encode('utf-8')
        self.send_response(status)
        self.close_connection = True
        self.send_header('Connection', 'close')
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(content)

    def ok(self, data):
        self.send(200, {'ok': True, 'data': data})

    def fail(self, status, message):
        self.send(status, {'ok': False, 'error': message})

    def valid_host(self):
        try:
            hostname = urlsplit('http://' + self.headers.get('Host', '')).hostname
            return hostname in self.server.allowed_hosts
        except ValueError:
            return False

    def same_origin(self):
        origin = self.headers.get('Origin')
        if not origin:
            return True
        # 服务只监听回环地址；能带上 X-Forwarded-Proto 的请求必来自本机反代，
        # 此时浏览器看到的源是 https://公网域名 而非 http://127.0.0.1。
        proto = self.headers.get('X-Forwarded-Proto', 'http')
        if proto not in ('http', 'https'):
            return False
        return origin == proto + '://' + self.headers.get('Host', '')

    def token(self):
        try:
            jar = cookies.SimpleCookie()
            jar.load(self.headers.get('Cookie', ''))
            return jar['radar_session'].value if 'radar_session' in jar else ''
        except cookies.CookieError:
            return ''

    def session(self):
        with self.server.lock:
            token = self.token()
            session = self.server.sessions.get(token)
            if session and session['expires'] > time.time():
                return session
            self.server.sessions.pop(token, None)
            return None

    def body(self):
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('不支持该请求编码')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 <= length <= 64000:
            raise ValueError('请求内容太长')
        data = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        if not isinstance(data, dict):
            raise ValueError('请求内容需要为对象')
        return data

    def login(self):
        if not self.same_origin():
            self.fail(403, '请求来源不匹配')
            return
        password = self.body().get('password', '')
        ip = self.client_address[0]
        with self.server.lock:
            attempts = [t for t in self.server.failures.get(ip, []) if t > time.time() - 60]
            self.server.failures[ip] = attempts
            if len(attempts) >= 5:
                self.fail(429, '登录失败过多，请一分钟后再试')
                return
            attempts.append(time.time())
        if not isinstance(password, str) or len(password) > 128 or not self.server.radar.config.verify(password):
            self.fail(401, '密码不正确，请查看本机首次登录文件或启动窗口')
            return
        token = secrets.token_urlsafe(32)
        session = dict(csrf=secrets.token_urlsafe(24), role='admin', username='管理员', expires=time.time() + 28800)
        with self.server.lock:
            self.server.failures[ip] = []
            # Bound memory and purge expired sessions.
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v['expires'] > time.time()}
            if len(self.server.sessions) >= 100:
                self.server.sessions.pop(next(iter(self.server.sessions)))
            self.server.sessions[token] = session
        self.send(200, {'ok': True, 'data': {k: v for k, v in session.items() if k != 'expires'}},
                  extra={'Set-Cookie': f'radar_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800'})

    def route(self, method):
        if not self.valid_host():
            self.fail(403, '访问地址不属于本机或局域网地址')
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if method == 'GET' and path in ('/', '/index.html', '/style.css', '/app.js', '/workspace.js'):
            file = ROOT / 'web' / ('index.html' if path == '/' else path[1:])
            self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] + '; charset=utf-8')
            return
        if method == 'POST' and path == '/api/login':
            self.login()
            return
        session = self.session()
        if not session:
            self.fail(401, '请先登录本机管理后台')
            return
        if method != 'GET' and (not self.same_origin() or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf'])):
            self.fail(403, '安全校验失败，请刷新后重试')
            return
        radar = self.server.radar
        if method == 'GET':
            if path == '/api/session':
                self.ok({k: v for k, v in session.items() if k != 'expires'})
            elif path == '/api/profile':
                self.ok(business_profile())
            elif path == '/api/dashboard':
                self.ok(radar.dashboard())
            elif path == '/api/guide':
                self.ok(radar.workspace.guide())
            elif path == '/api/geo':
                self.ok(radar.workspace.geo())
            elif path == '/api/geo/export.zip':
                self.send(200,radar.workspace.export(),'application/zip',extra={'Content-Disposition':'attachment; filename="GEO_Content.zip"'})
            elif path in ('/api/tasks', '/api/sources', '/api/runs'):
                self.ok(radar.sources() if path == '/api/sources' else radar.db.all(path[5:]))
            elif path == '/api/leads':
                self.ok(filtered_leads(radar.db, params))
            elif path == '/api/settings':
                self.ok(radar.config.public(radar.lan))
            elif path == '/api/export.csv':
                self.send(200, csv_export(filtered_leads(radar.db, params)), 'text/csv; charset=utf-8',
                          {'Content-Disposition': 'attachment; filename="lead-radar.csv"'})
            else:
                self.fail(404, '接口不存在')
            return
        body = self.body() if method != 'DELETE' else {}
        if method == 'POST' and path == '/api/logout':
            with self.server.lock:
                self.server.sessions.pop(self.token(), None)
            self.send(200, {'ok': True, 'data': {}}, extra={'Set-Cookie': 'radar_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'})
        elif method == 'POST' and path == '/api/tasks':
            self.ok(radar.save_task(body))
        elif method == 'POST' and path == '/api/sources':
            self.ok(radar.save_source(body))
        elif method == 'POST' and path == '/api/geo/profile':
            self.ok(radar.workspace.save_profile(body))
        elif method == 'POST' and path == '/api/geo/entries':
            self.ok(radar.workspace.save_entry(body))
        elif path.startswith('/api/geo/entries/') and method in ('PATCH','DELETE'):
            uid = int(path.rsplit('/',1)[1])
            if method == 'PATCH':
                self.ok(radar.workspace.save_entry(body,uid))
            else:
                radar.workspace.delete_entry(uid)
                self.ok({})
        elif method == 'POST' and path == '/api/settings':
            radar.config.update(body)
            self.ok(radar.config.public(radar.lan))
        elif method == 'POST' and path == '/api/password':
            radar.config.change_password(body.get('old_password', ''), body.get('new_password', ''))
            with self.server.lock:
                self.server.sessions.clear()
            self.ok({'message': '密码已修改，请重新登录'})
        elif method == 'POST' and path == '/api/feishu/check':
            self.ok(radar.check_feishu())
        elif method == 'POST' and path == '/api/feishu/sync':
            self.ok(radar.sync())
        else:
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api', 'tasks'] and parts[3] == 'run' and method == 'POST':
                self.ok(radar.run_task(int(parts[2])))
            elif len(parts) == 3 and parts[0] == 'api' and parts[1] in ('tasks', 'sources', 'leads'):
                table = parts[1]
                uid = parts[2] if table == 'leads' else int(parts[2])
                if method == 'PATCH' and table == 'tasks':
                    self.ok(radar.save_task(body, uid))
                elif method == 'PATCH' and table == 'sources':
                    self.ok(radar.save_source(body, uid))
                elif method == 'PATCH' and table == 'leads':
                    self.ok(radar.update_lead(uid, body))
                elif method == 'DELETE':
                    if table == 'sources' and any(uid in t['source_ids'] for t in radar.db.all('tasks')):
                        raise ValueError('数据源被任务引用，请先修改或删除相关任务')
                    if table == 'tasks' and uid in radar.active:
                        raise ValueError('任务正在采集，请等待完成')
                    radar.db.delete(table, uid)
                    if table == 'sources':
                        radar.config.delete_source_token(uid)
                    self.ok({})
                else:
                    self.fail(404, '接口不存在')
            else:
                self.fail(404, '接口不存在')

    def dispatch(self, method):
        try:
            self.route(method)
        except KeyError:
            self.fail(404, '记录不存在')
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.fail(400, self.server.radar.safe_error(exc))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as exc:
            self.server.radar.log('管理请求失败', errors=[self.server.radar.safe_error(exc)])
            self.fail(500, '处理失败，详情已写入运行日志')

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PATCH(self):
        self.dispatch('PATCH')

    def do_DELETE(self):
        self.dispatch('DELETE')


def main():
    parser = argparse.ArgumentParser(description='本地真实客资雷达')
    parser.add_argument('--lan', action='store_true', help='允许同一局域网手机访问')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', default=str(ROOT / 'data'))
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--reset-password', action='store_true')
    parser.add_argument('--public-host', action='append', default=[],
                        help='公网反代域名白名单，可多次指定（如 --public-host radar.ecojia.cn）')
    args = parser.parse_args()
    public_hosts = []
    for name in args.public_host:
        name = str(name).strip().lower().rstrip('.')
        if not name or len(name) > 253 or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in name) \
                or '..' in name or name.startswith(('-', '.')) or name.endswith(('-', '.')):
            parser.error('--public-host 需要合法域名（如 radar.ecojia.cn）')
        public_hosts.append(name)
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    instance_lock = open(Path(args.data_dir) / 'service.lock', 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if instance_lock.tell() == 0:
                instance_lock.write(b'1')
                instance_lock.flush()
            instance_lock.seek(0)
            msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        instance_lock.close()
        print('该数据目录已有客资雷达在运行，请使用已打开的后台窗口。')
        return 1
    radar = Radar(args.data_dir, lan=args.lan)
    if args.reset_password:
        radar.config.reset_password()
    try:
        server = Server(('0.0.0.0' if args.lan else '127.0.0.1', args.port), radar, public_hosts=public_hosts)
    except OSError:
        print('端口被占用或无法启动，请关闭其他客资雷达窗口，或用 --port 指定其他端口。')
        return 1
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'\n客资雷达真实采集版 {VERSION}\n电脑访问：{url}')
    if radar.config.initial_password:
        print('首次登录密码：' + radar.config.initial_password)
    print('登录资料在本机 data/首次登录.txt；改密码后文件自动删除。')
    if args.lan:
        for item in radar.lan_urls:
            print('同一 Wi-Fi 手机访问：' + item)
        print('局域网使用 HTTP，仅供可信家庭/办公网络；Windows 防火墙只允许专用网络。')
    print('电脑保持开机且本窗口不关闭，定时采集才会运行。按 Ctrl+C 停止。\n')
    radar.start()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print('\n已停止服务。数据保留在本机 data 文件夹。')
    finally:
        radar.stop_event.set()
        server.server_close()
        instance_lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
