"""Authenticated companion UI and lifecycle, separate from upstream 3x-ui files."""
import collections
import hashlib
import http.client
import http.server
import json
import logging
import os
import pathlib
import signal
import socket
import ssl
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'generated'))
import grpc
from accounting import Gateway, Store, make_server, STAT, PREFIX
from app.stats.command import command_pb2 as stats
from transport import MarkedRelay
from license_client import LicenseClient
from ui_errors import response_error

VERSION = (ROOT/'VERSION').read_text().strip() if (ROOT/'VERSION').exists() else '1.1.0'


def tls_context(config):
    context=ssl.create_default_context()
    cert=config.get('cert_file')
    if cert:
        context.load_verify_locations(cafile=cert)
        context.verify_flags |= getattr(ssl,'VERIFY_X509_PARTIAL_CHAIN',0)
    return context


def origin(config):
    host=config['hostname']
    if ':' in host and not host.startswith('['):host='['+host+']'
    return config.get('web_scheme','https')+'://'+host+':'+str(config['web_port'])


class CoreGeneration:
    """Identify the configured core process without changing or restarting it."""
    def __init__(self,config_path,pid_hint=None,parent_hint=None,parent_command=None):
        self.path=pathlib.Path(config_path).resolve()
        self.boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.cached=pathlib.Path('/proc')/str(pid_hint) if pid_hint else None
        self.parent_hint=str(parent_hint) if parent_hint else None
        self.parent_command=parent_command

    def identity(self,proc):
        fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
        if fields[0]=='Z':return None
        return self.boot+':'+proc.name+':'+fields[19]

    def __call__(self):
        if self.cached:
            try:
                identity=self.identity(self.cached)
                if identity:return identity
            except OSError:pass
        for proc in pathlib.Path('/proc').iterdir():
            if not proc.name.isdigit():continue
            try:
                if not (proc/'comm').read_text().startswith('xray'):continue
                arguments=(proc/'cmdline').read_bytes().decode().split('\0')
                for index,argument in enumerate(arguments[:-1]):
                    if argument not in ['-c','-config']:continue
                    candidate=pathlib.Path(arguments[index+1])
                    if not candidate.is_absolute():
                        try:candidate=(proc/'cwd').resolve(strict=True)/candidate
                        except PermissionError:
                            # A root service with a restricted capability set
                            # cannot always dereference another root process's
                            # cwd. Match the explicit relative suffix used by
                            # native 3x-ui without granting ptrace privileges.
                            if len(candidate.parts)<2 or not self.path.as_posix().endswith('/'+candidate.as_posix()):continue
                            fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
                            parent=fields[1]
                            if self.parent_hint and parent!=self.parent_hint:
                                command=(pathlib.Path('/proc')/parent/'cmdline').read_bytes()
                                if not self.parent_command or hashlib.sha256(command).hexdigest()!=self.parent_command:continue
                            candidate=self.path
                    if candidate.resolve()==self.path:
                        identity=self.identity(proc)
                        if identity:self.cached=proc;return identity
            except (OSError,UnicodeError,IndexError):continue
        raise RuntimeError('Configured Xray core is temporarily unavailable')


class Panel:
    def __init__(self, config):
        self.config = config

    def inbounds(self, cookie=None):
        if cookie is None:
            if self.config.get('catalog_backend')=='sqlite':
                uri='file:'+urllib.parse.quote(str(pathlib.Path(self.config['panel_db']).resolve()),safe='/')+'?mode=ro'
                with sqlite3.connect(uri,uri=True,timeout=5) as connection:
                    connection.execute('PRAGMA query_only=ON')
                    connection.row_factory=sqlite3.Row
                    return [dict(row) for row in connection.execute('SELECT id,tag,remark,port,protocol FROM inbounds ORDER BY id')]
            # This version's monitor token deliberately cannot list inbound
            # configurations. A column-restricted database role reads metadata
            # without obtaining UUIDs, passwords or write permissions.
            result=subprocess.run(['/usr/bin/psql','-X','-h',self.config.get('pg_host','127.0.0.1'),
                '-p',str(self.config.get('pg_port',5432)),'-U',self.config['catalog_user'],
                '-d',self.config.get('pg_database','xui'),'-At','-v','ON_ERROR_STOP=1'],
                input="SELECT COALESCE(json_agg(row_to_json(i)),'[]') FROM (SELECT id,tag,remark,port,protocol FROM inbounds ORDER BY id) i;",
                capture_output=True,text=True,timeout=8,
                env={'PGPASSWORD':self.config['catalog_password'],'PGCONNECT_TIMEOUT':'4','LC_ALL':'C.UTF-8','PATH':'/usr/bin:/bin'})
            if result.returncode:raise RuntimeError('Read-only inbound catalog unavailable')
            rows=json.loads(result.stdout)
            if not isinstance(rows,list):raise ValueError('Invalid inbound catalog response')
            return rows
        config = self.config
        if config.get('panel_scheme','https')=='https':
            connection=http.client.HTTPSConnection(config['hostname'],config['panel_port'],context=tls_context(config),timeout=8)
        else:
            connection=http.client.HTTPConnection(config['hostname'],config['panel_port'],timeout=8)
        connection._create_connection = lambda *a, **k: socket.create_connection((config.get('panel_connect_host','127.0.0.1'), config['panel_port']), timeout=5)
        headers = {'Cookie':cookie}
        try:
            connection.request('GET', config['base_path']+'panel/api/inbounds/list', headers=headers)
            response = connection.getresponse()
            raw = response.read(4*1024*1024+1)
            if response.status != 200 or len(raw)>4*1024*1024:
                raise PermissionError('ورود به پنل لازم است')
            try:result=json.loads(raw)
            except (ValueError, UnicodeError):raise PermissionError('ورود به پنل لازم است')
            if not result.get('success') or not isinstance(result.get('obj'),list):
                raise PermissionError('ورود به پنل لازم است')
            return result['obj']
        finally:connection.close()


class Application:
    def __init__(self, config):
        self.config = config
        self.store = Store(config['state_path'])
        self.panel = Panel(config)
        self.license = LicenseClient(pathlib.Path(config['state_path']).parent / 'license.json', 'zarib',
            str(config.get('panel_port', config.get('web_port', 2053))) + ':' + config['base_path'])
        self.license.start()
        self.gateway = None
        self.error = ''
        self.metadata_ok_at = 0
        self.reconciled_at = 0
        self.stop = threading.Event()
        self._metadata_lock = threading.RLock()

    def refresh(self):
        with self._metadata_lock:
            rows = self.panel.inbounds()
            for inbound in rows:
                clients = json.loads(inbound.get('settings') or '{}').get('clients',[])
                if any(str(client.get('email','')).startswith(PREFIX) for client in clients):
                    raise RuntimeError('نام یک حساب با شناسهٔ داخلی افزونه تداخل دارد')
            self.store.sync(rows)
            self.metadata_ok_at = int(time.time())

    def safety_check(self):
        config = json.loads(pathlib.Path(self.config['core_config']).read_text())
        if any(rule.get('user') for rule in config.get('routing',{}).get('rules',[])):
            raise ValueError('قانون مسیریابی بر اساس ایمیل کاربر فعال است؛ تغییر ضریب تا بررسی سازگاری غیرفعال است')
        if any(str(user.get('email','')).startswith(PREFIX) for inbound in config.get('inbounds',[])
               for user in inbound.get('settings',{}).get('clients',[])):
            raise ValueError('نام یک حساب با شناسهٔ داخلی افزونه تداخل دارد')

    def maintain(self):
        while not self.stop.is_set():
            try:
                if time.time()-self.metadata_ok_at>30:self.refresh()
                self.gateway.reconcile()
                self.reconciled_at=int(time.time())
                self.error=''
            except Exception:
                logging.exception('Metadata refresh/reconciliation failed')
                self.error='همگام‌سازی موقتاً انجام نشد؛ پیش از تغییر ضریب، وضعیت بررسی شود'
            self.stop.wait(5)

    def status(self, allowed):
        rows = [dict(row, factor=row['milli']/1000) for row in self.store.rows() if row['id'] in allowed]
        return {'inbounds':rows, 'error':self.error or self.gateway.last_error,
            'metadata_ok_at':self.metadata_ok_at, 'reconciled_at':self.reconciled_at,
            'last_rpc_at':self.gateway.last_rpc_at, 'version':VERSION, 'license':self.license.status()}


class WebHandler(http.server.BaseHTTPRequestHandler):
    server_version='FarstarMultiplier/1'
    def log_message(self, *args):pass

    @property
    def app(self):return self.server.application

    def respond(self, status, body, content_type='application/json; charset=utf-8'):
        if status>=400 and isinstance(body,dict) and 'error' in body and not body.get('error_code'):
            body={**body,**response_error(body['error'],status,self.path)}
        if isinstance(body,dict):body=json.dumps(body,ensure_ascii=False).encode()
        elif isinstance(body,str):body=body.encode()
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers();self.wfile.write(body)

    def authorized(self):
        cookie=self.headers.get('Cookie','')
        if not cookie or len(cookie)>8192:raise PermissionError('ابتدا با همین دامنه وارد پنل ثنایی شوید')
        return {int(row['id']) for row in self.app.panel.inbounds(cookie=cookie)}

    def valid_host(self):
        return self.headers.get('Host','').lower()==urllib.parse.urlsplit(origin(self.app.config)).netloc.lower()

    def do_GET(self):
        base=self.app.config['base_path']+'multipliers/'
        if not self.valid_host():return self.respond(421,{'error':'از دامنهٔ پنل استفاده کنید'})
        path=urllib.parse.urlsplit(self.path).path
        if path in [base,base+'index.html',base+'ui.js',base+'ui.css',base+'ui-feedback.js']:
            filename={base:'index.html',base+'index.html':'index.html',base+'ui.js':'ui.js',base+'ui.css':'ui.css',base+'ui-feedback.js':'ui-feedback.js'}[path]
            kind={'index.html':'text/html; charset=utf-8','ui.js':'text/javascript; charset=utf-8','ui-feedback.js':'text/javascript; charset=utf-8','ui.css':'text/css; charset=utf-8'}[filename]
            return self.respond(200,(ROOT/filename).read_bytes(),kind)
        if path != base+'api':return self.respond(404,{'error':'پیدا نشد'})
        try:
            allowed=self.authorized()
            self.respond(200,self.app.status(allowed))
        except PermissionError as error:self.respond(401,{'error':str(error)})
        except Exception:
            logging.exception('UI read failed');self.respond(503,{'error':'ارتباط با پنل برقرار نشد؛ دوباره امتحان کنید'})

    def do_POST(self):
        base=self.app.config['base_path']+'multipliers/'
        if not self.valid_host():return self.respond(421,{'error':'دامنه نامعتبر است'})
        if self.path not in [base+'api',base+'api/license/activate',base+'api/license/refresh']:return self.respond(404,{'error':'پیدا نشد'})
        if self.headers.get('Origin')!=origin(self.app.config) or self.headers.get_content_type()!='application/json':
            return self.respond(403,{'error':'درخواست باید از صفحهٔ مدیریت ضریب ارسال شود'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=2048:raise ValueError('درخواست نامعتبر است')
            allowed=self.authorized()
            body=json.loads(self.rfile.read(length))
            if self.path==base+'api/license/activate':
                return self.respond(200,self.app.license.activate(body.get('key'),body.get('new_installation') is True))
            if self.path==base+'api/license/refresh':
                return self.respond(200,self.app.license.refresh())
            self.app.license.require_management()
            inbound_id=int(body['inbound_id'])
            if inbound_id not in allowed:raise PermissionError('به این اینباند دسترسی ندارید')
            if self.app.error or time.time()-self.app.metadata_ok_at>90:
                return self.respond(503,{'error':'همگام‌سازی پنل نیاز به بررسی دارد؛ ضریب تغییر نکرد'})
            self.app.safety_check()
            self.app.gateway.change_factor(inbound_id,body['factor'])
            # Reconciliation is asynchronous so a UI timeout never repeats a user edit.
            self.respond(200,{'success':True,'message':'ضریب ذخیره شد؛ تا ۵ ثانیه دیگر برای اتصال‌های تازه اعمال می‌شود'})
        except PermissionError as error:self.respond(401,{'error':str(error)})
        except (ValueError,TypeError,KeyError,ArithmeticError) as error:self.respond(400,{'error':str(error)})
        except Exception:
            logging.exception('UI write failed');self.respond(503,{'error':'ذخیرهٔ ضریب تأیید نشد؛ صفحه را تازه کنید'})


class TLSHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self, address, application):
        self.application=application
        self.tls_lock=threading.RLock()
        self.tls_stamp=None
        self.tls_context=None
        self.context()
        super().__init__(address,WebHandler)

    def context(self):
        config=self.application.config
        stamp=(os.stat(config['cert_file']).st_mtime_ns,os.stat(config['key_file']).st_mtime_ns)
        with self.tls_lock:
            if stamp!=self.tls_stamp:
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version=ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(config['cert_file'],config['key_file'])
                self.tls_context,self.tls_stamp=context,stamp
            return self.tls_context

    def process_request_thread(self, request, client_address):
        wrapped=None
        try:
            request.settimeout(10)
            wrapped=self.context().wrap_socket(request,server_side=True)
            super().process_request_thread(wrapped,client_address)
        except (OSError,ssl.SSLError):pass
        finally:
            if wrapped:wrapped.close()
            else:request.close()


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    config=json.loads(pathlib.Path(sys.argv[1]).read_text())
    app=Application(config)
    app.refresh()
    relay=MarkedRelay(('127.0.0.1',config['relay_port']),('127.0.0.1',config['core_api_port']))
    relay.start()
    channel=grpc.insecure_channel('127.0.0.1:'+str(config['relay_port']))
    app.gateway=Gateway(channel,app.store,app.refresh,CoreGeneration(config['core_config'],
        config.get('core_pid_hint'),config.get('core_parent_pid_hint'),config.get('core_parent_command_hash')))
    # Only a read without resetting counters is used for readiness.
    app.gateway.call(STAT+'GetSysStats',stats.SysStatsRequest(),timeout=8)
    grpc_server=make_server(app.gateway,'127.0.0.1:'+str(config['gateway_port']))
    grpc_server.start()
    listen=(config.get('web_listen','0.0.0.0'),config['web_port'])
    if config.get('web_scheme','https')=='https':
        web=TLSHTTPServer(listen,app)
    else:
        web=http.server.ThreadingHTTPServer(listen,WebHandler);web.application=app
    threading.Thread(target=web.serve_forever,daemon=True).start()
    threading.Thread(target=app.maintain,daemon=True).start()
    def stop(*_):app.stop.set()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    # systemd readiness, without extra Python dependencies.
    notify=os.environ.get('NOTIFY_SOCKET')
    if notify:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as notifier:
            notifier.connect('\0'+notify[1:] if notify.startswith('@') else notify)
            notifier.sendall(b'READY=1')
    logging.info('Accounting gateway and companion UI ready; customer sockets unchanged')
    app.stop.wait()
    grpc_server.stop(10).wait()
    web.shutdown();web.server_close();channel.close();relay.shutdown();relay.server_close()


if __name__=='__main__':main()
