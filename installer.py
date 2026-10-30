"""ZARIB installer: discover first, back up, install independently, verify, roll back.

The original panel database is read-only except for provisioning a narrowly
scoped PostgreSQL reader role. The panel/core services are never restarted.
"""
import argparse
import hashlib
import http.client
import json
import os
import pathlib
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse

SOURCE=pathlib.Path(__file__).resolve().parent
RUNTIME=['license_client.py','accounting.py','server.py','transport.py','redirect.py','index.html','ui.css','ui.js','VERSION']
SETTINGS=('webPort','webBasePath','webCertFile','webKeyFile','webDomain','webListen')


def run(arguments, timeout=25):
    result=subprocess.run(list(map(str,arguments)),capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError('Command failed: '+str(arguments[0])+' (exit '+str(result.returncode)+')')
    return result.stdout.strip()


def write_private(path, content):
    path=pathlib.Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_name(path.name+'.new-'+secrets.token_hex(4))
    temporary.write_text(content,encoding='utf-8');temporary.chmod(0o600)
    os.replace(temporary,path)


def panel_environment(unit):
    try:
        pid=run(['systemctl','show',unit,'-p','MainPID','--value'])
        if not pid.isdigit() or int(pid)<2:return {}
        data=(pathlib.Path('/proc')/pid/'environ').read_bytes()
        return dict(item.decode().split('=',1) for item in data.split(b'\0') if b'=' in item)
    except (OSError,RuntimeError):return {}


def pg_options(dsn):
    if dsn.startswith(('postgres://','postgresql://')):
        url=urllib.parse.urlsplit(dsn)
        return {'host':url.hostname or '127.0.0.1','port':url.port or 5432,
                'dbname':urllib.parse.unquote(url.path.lstrip('/')) or 'xui'}
    values=dict(item.split('=',1) for item in shlex.split(dsn) if '=' in item)
    return {'host':values.get('host','127.0.0.1'),'port':int(values.get('port',5432)),
            'dbname':values.get('dbname','xui')}


class Catalog:
    def __init__(self, backend, database=None, options=None):
        self.backend,self.database,self.options=backend,database,options or {}
        self.created_role=None

    def pg(self, statement):
        args=['runuser','-u','postgres','--','psql','-X','-p',str(self.options['port']),
              '-d',self.options['dbname'],'-At','-v','ON_ERROR_STOP=1']
        result=subprocess.run(args,input=statement,capture_output=True,text=True,timeout=15)
        if result.returncode:raise RuntimeError('Local PostgreSQL is unavailable or the required database privileges are missing')
        return result.stdout.strip()

    def settings(self):
        names=','.join("'"+name+"'" for name in SETTINGS)
        if self.backend=='sqlite':
            uri='file:'+urllib.parse.quote(str(self.database.resolve()),safe='/')+'?mode=ro'
            with sqlite3.connect(uri,uri=True,timeout=5) as connection:
                connection.execute('PRAGMA query_only=ON')
                return dict(connection.execute('SELECT key,value FROM settings WHERE key IN ('+names+')'))
        raw=self.pg("SELECT COALESCE(json_object_agg(key,value),'{}') FROM settings WHERE key IN ("+names+");")
        return json.loads(raw)

    def configure(self, config):
        config['catalog_backend']=self.backend
        if self.backend=='sqlite':
            config['panel_db']=str(self.database.resolve());return
        role='zarib_reader_'+secrets.token_hex(5)
        password=secrets.token_urlsafe(36)
        database=self.options['dbname'].replace('"','""')
        self.pg('BEGIN; CREATE ROLE '+role+" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD '"+password+"';\n"
            'GRANT CONNECT ON DATABASE "'+database+'" TO '+role+';\nGRANT USAGE ON SCHEMA public TO '+role+';\n'
            'GRANT SELECT (id,tag,remark,port,protocol) ON inbounds TO '+role+'; COMMIT;')
        self.created_role=role
        config.update(catalog_user=role,catalog_password=password,pg_host='127.0.0.1',
                      pg_port=int(self.options['port']),pg_database=self.options['dbname'])

    def rollback(self):
        if self.created_role:
            self.pg('DROP OWNED BY '+self.created_role+'; DROP ROLE '+self.created_role+';')
            self.created_role=None


def discover_catalog(args):
    environment=panel_environment(args.panel_unit)
    backend=args.backend
    if backend=='auto':
        backend='sqlite' if args.panel_db else ('postgres' if environment.get('XUI_DB_TYPE','').lower() in ['postgres','postgresql','pg'] else 'sqlite')
    if backend=='sqlite':
        database=pathlib.Path(args.panel_db or str(pathlib.Path(environment.get('XUI_DB_FOLDER','/etc/x-ui'))/'x-ui.db'))
        if not database.is_file():
            raise ValueError('Panel SQLite database not found. Use --panel-db PATH, or --backend postgres for a local PostgreSQL panel.')
        return Catalog('sqlite',database=database)
    options=pg_options(environment.get('XUI_DB_DSN',''))
    if options['host'] not in ['127.0.0.1','localhost','::1'] and not options['host'].startswith('/'):
        raise ValueError('Automatic PostgreSQL installation supports a local database. Remote/container PostgreSQL requires a separately provisioned reader configuration.')
    return Catalog('postgres',options=options)


def detect_hostname(settings, supplied=None):
    if supplied:host=supplied.strip()
    elif settings.get('webDomain'):host=settings['webDomain'].strip()
    elif settings.get('webCertFile'):
        certificate=ssl._ssl._test_decode_cert(settings['webCertFile'])
        names=[value for kind,value in certificate.get('subjectAltName',[]) if kind=='DNS' and '*' not in value]
        if not names:
            names=[value for group in certificate.get('subject',[]) for kind,value in group if kind=='commonName' and '*' not in value]
        if not names:raise ValueError('Certificate has no concrete hostname. Specify --host panel.example.com.')
        host=names[0]
    else:
        addresses=json.loads(run(['ip','-j','-4','address','show','scope','global']))
        import ipaddress
        candidates=[item['local'] for link in addresses for item in link.get('addr_info',[])
                    if not ipaddress.ip_address(item['local']).is_private]
        if not candidates:raise ValueError('No public panel hostname found. Specify --host YOUR_PANEL_DOMAIN_OR_IP.')
        host=candidates[0]
    if any(character in host for character in '/\\\r\n\t @?#') or not host or len(host)>253:
        raise ValueError('Provide a hostname or IP without a scheme, path or port.')
    if ':' in host:
        import ipaddress
        ipaddress.ip_address(host.strip('[]'));host=host.strip('[]')
    else:host=host.encode('idna').decode('ascii')
    return host.lower()


def free_port(preferred, supplied=None, bind='127.0.0.1', excluded=()):
    choices=[supplied] if supplied else preferred
    for port in choices:
        if not 1024<=int(port)<=65535 or int(port) in excluded:continue
        try:
            with socket.socket() as listener:listener.bind((bind,int(port)))
            return int(port)
        except OSError:continue
    raise ValueError('No available port found; choose an unused port explicitly.')


def normalize_base(value):
    if not value:return '/'
    if not value.startswith('/') or any(c in value for c in ['?','#','\r','\n']):
        raise ValueError('Invalid panel base path')
    return value.rstrip('/')+'/'


def discover_config(args, catalog):
    settings=catalog.settings()
    if not settings.get('webPort'):raise ValueError('Panel webPort setting is missing')
    core=json.loads(args.core_config.read_text())
    api_tag=core.get('api',{}).get('tag')
    api=next((item for item in core.get('inbounds',[]) if item.get('tag')==api_tag),None)
    if not api or api.get('listen')!='127.0.0.1':raise ValueError('A running loopback IPv4 Xray API is required')
    if not {'HandlerService','StatsService'}.issubset(core['api'].get('services',[])):
        raise ValueError('Enable HandlerService and StatsService in the official panel first')
    if not any(i.get('protocol')=='vless' for i in core.get('inbounds',[])):
        raise ValueError('At least one VLESS inbound is required')
    if bool(settings.get('webCertFile'))!=bool(settings.get('webKeyFile')):
        raise ValueError('Panel certificate and private key must both be configured')
    scheme='https' if settings.get('webCertFile') else 'http'
    for key in ['webCertFile','webKeyFile']:
        if settings.get(key) and not pathlib.Path(settings[key]).is_file():raise ValueError('Configured panel TLS file is missing')
    gateway=free_port([62390,62490,62590],args.gateway_port,excluded=[int(api['port'])])
    relay=free_port([62392,62492,62592],args.relay_port,excluded=[int(api['port']),gateway])
    web=free_port([2087,2083,2054],args.web_port,args.listen,excluded=[int(api['port']),gateway,relay,int(settings['webPort'])])
    config=dict(hostname=detect_hostname(settings,args.host),panel_port=int(settings['webPort']),
        panel_scheme=scheme,web_scheme=scheme,base_path=normalize_base(settings.get('webBasePath','/')),
        panel_connect_host=settings.get('webListen') if settings.get('webListen') not in [None,'','0.0.0.0','::'] else '127.0.0.1',
        cert_file=settings.get('webCertFile',''),key_file=settings.get('webKeyFile',''),core_api_port=int(api['port']),
        gateway_port=gateway,relay_port=relay,web_port=web,web_listen=args.listen,
        state_path=str(args.state_dir/'state.sqlite'),core_config=str(args.core_config.resolve()))
    return config


def compile_sources(directory):
    generated=directory/'generated';generated.mkdir(exist_ok=True)
    protos=[str(file.relative_to(directory/'proto')) for file in sorted((directory/'proto').rglob('*.proto'))]
    run(['protoc','-I',directory/'proto','--python_out='+str(generated)]+protos)
    for file in directory.rglob('*.py'):
        compile(file.read_text(encoding='utf-8'),str(file),'exec')
    return generated


def core_fingerprint(config):
    target=pathlib.Path(config['core_config']).resolve()
    result={'config_hash':hashlib.sha256(target.read_bytes()).hexdigest(),'pids':[]}
    for proc in pathlib.Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:
            arguments=(proc/'cmdline').read_bytes().decode().split('\0')
            for position,argument in enumerate(arguments[:-1]):
                if argument in ['-c','-config']:
                    candidate=pathlib.Path(arguments[position+1])
                    if not candidate.is_absolute():candidate=(proc/'cwd').resolve()/candidate
                    if candidate.resolve()==target:result['pids'].append(int(proc.name))
        except (OSError,UnicodeError):continue
    result['pids'].sort()
    return result


def unit_text(args):
    return f'''[Unit]
Description=ZARIB per-inbound traffic multiplier
After=network-online.target {args.panel_unit}
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=notify
NotifyAccess=main
User=root
WorkingDirectory={args.prefix}
ExecStart=/usr/bin/python3 {args.prefix}/server.py {args.config_dir}/config.json
ExecStartPost=/usr/bin/python3 {args.prefix}/redirect.py ensure {args.config_dir}/config.json
Restart=always
RestartSec=3
TimeoutStartSec=45
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={args.state_dir} /run
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK

[Install]
WantedBy=multi-user.target
'''


def probe_core(config):
    import grpc
    from app.stats.command import command_pb2 as stats
    from app.proxyman.command import command_pb2 as handlers
    from transport import MarkedRelay
    core=json.loads(pathlib.Path(config['core_config']).read_text())
    tag=next(item['tag'] for item in core['inbounds'] if item.get('protocol')=='vless')
    # A marked direct socket also works when an existing ZARIB redirect is installed.
    relay=MarkedRelay(('127.0.0.1',0),('127.0.0.1',config['core_api_port']));relay.start()
    try:
        with grpc.insecure_channel('127.0.0.1:'+str(relay.server_address[1])) as channel:
            call=channel.unary_unary('/xray.app.stats.command.StatsService/GetSysStats')
            stats.SysStatsResponse.FromString(call(stats.SysStatsRequest().SerializeToString(),timeout=6))
            call=channel.unary_unary('/xray.app.proxyman.command.HandlerService/GetInboundUsers')
            handlers.GetInboundUserResponse.FromString(call(handlers.GetInboundUserRequest(tag=tag).SerializeToString(),timeout=6))
    finally:relay.shutdown();relay.server_close()


def probe_web(config):
    from server import tls_context
    if config.get('web_scheme','https')=='https':
        connection=http.client.HTTPSConnection(config['hostname'],config['web_port'],context=tls_context(config),timeout=6)
    else:connection=http.client.HTTPConnection(config['hostname'],config['web_port'],timeout=6)
    connection._create_connection=lambda *a,**k:socket.create_connection(('127.0.0.1',config['web_port']),timeout=5)
    try:
        connection.request('GET',config['base_path']+'multipliers/')
        response=connection.getresponse();body=response.read()
        if response.status!=200 or b'ZARIB' not in body:raise RuntimeError('Companion page verification failed')
    finally:connection.close()


def install(args):
    config_path=args.config_dir/'config.json'
    unit=pathlib.Path('/etc/systemd/system')/(args.service_name+'.service')
    existing=config_path.is_file()
    catalog=None
    config=json.loads(config_path.read_text()) if existing else None
    if not existing and unit.exists():raise ValueError('A service with this name already exists without its configuration')
    if not existing:
        catalog=discover_catalog(args)
        config=discover_config(args,catalog)
    # Compile to an isolated staging directory before changing installed files.
    with tempfile.TemporaryDirectory(prefix='zarib-stage-') as temporary:
        stage=pathlib.Path(temporary)
        for name in RUNTIME:shutil.copy2(SOURCE/name,stage/name)
        shutil.copytree(SOURCE/'proto',stage/'proto')
        compile_sources(stage)
        sys.path.insert(0,str(stage));sys.path.insert(0,str(stage/'generated'))
        from accounting import Store
        from server import Panel,origin
        from redirect import rule,remove
        probe_core(config)
        if args.check:
            if existing:Panel(config).inbounds()
            print('Preflight OK. No files, factors or services were changed.');return
        before=core_fingerprint(config)
        if not before['pids']:raise RuntimeError('The configured Xray core must be running before installation')
        core_pid=before['pids'][-1]
        core_fields=(pathlib.Path('/proc')/str(core_pid)/'stat').read_text().rsplit(')',1)[1].split()
        config['core_pid_hint']=core_pid
        config['core_parent_pid_hint']=int(core_fields[1])
        parent_command=(pathlib.Path('/proc')/core_fields[1]/'cmdline').read_bytes()
        config['core_parent_command_hash']=hashlib.sha256(parent_command).hexdigest()
        stamp=time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)
        backup=args.state_dir/'backups'/stamp
        backup.mkdir(parents=True,mode=0o700)
        if existing:
            shutil.copy2(config_path,backup/'config.json')
            if unit.exists():shutil.copy2(unit,backup/'service.unit')
            state=pathlib.Path(config['state_path'])
            if state.exists():
                with sqlite3.connect(state) as source:
                    with sqlite3.connect(backup/'state.sqlite') as target:source.backup(target)
        previous={}
        for name in RUNTIME+['generated','proto']:
            target=args.prefix/name
            if target.exists():
                previous[name]=True
                if target.is_dir():shutil.copytree(target,backup/name)
                else:shutil.copy2(target,backup/name)
        service_touched=False
        try:
            if catalog:catalog.configure(config)
            metadata=Panel(config).inbounds()
            args.state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
            state=Store(config['state_path']);state.sync(metadata)
            if not existing and any(row['milli']!=1000 or row['instrumented'] for row in state.rows()):
                raise RuntimeError('Fresh installation found prior accounting state; use its original configuration')
            args.prefix.mkdir(parents=True,exist_ok=True,mode=0o700)
            for name in RUNTIME:
                write_private(args.prefix/name,(stage/name).read_text(encoding='utf-8'))
            for name in ['proto','generated']:
                shutil.copytree(stage/name,args.prefix/name,dirs_exist_ok=True)
            for name in ['LICENSE','THIRD_PARTY_LICENSE_Xray-core.txt']:
                if (SOURCE/name).exists():shutil.copy2(SOURCE/name,args.prefix/name)
            write_private(config_path,json.dumps(config,indent=2)+'\n')
            write_private(unit,unit_text(args))
            run(['systemctl','daemon-reload'])
            service_touched=True
            run(['systemctl','restart' if existing else 'start',args.service_name+'.service'],timeout=55)
            probe_web(config)
            after=core_fingerprint(config)
            if before!=after:raise RuntimeError('The original core changed during installation; inspect panel activity before retrying')
            run(['systemctl','enable',args.service_name+'.service'])
            print('\nZARIB '+(SOURCE/'VERSION').read_text().strip()+' is ready.')
            print('URL: '+origin(config)+config['base_path']+'multipliers/')
            print('Sign into 3x-ui in the same browser and hostname, then open ZARIB.')
            print('Existing factors preserved.' if existing else 'All inbound factors start at 1.')
            print('Original panel/core were not restarted. Backup: '+str(backup))
        except Exception:
            if existing:
                # Restore code/config, never an old live accounting database.
                for name in previous:
                    saved=backup/name;target=args.prefix/name
                    if saved.is_dir():shutil.copytree(saved,target,dirs_exist_ok=True)
                    else:shutil.copy2(saved,target)
                shutil.copy2(backup/'config.json',config_path)
                if (backup/'service.unit').exists():shutil.copy2(backup/'service.unit',unit)
                run(['systemctl','daemon-reload'])
                if service_touched:run(['systemctl','restart',args.service_name+'.service'],timeout=55)
            else:
                state_path=pathlib.Path(config['state_path'])
                if state_path.exists() and any(row['instrumented'] for row in Store(state_path).rows()):
                    raise RuntimeError('A factor was edited during installation; accounting gateway retained for manual inspection')
                if service_touched:
                    # A fresh gateway cannot contain scoped real users before an edit.
                    remove(rule(config['core_api_port'],config['gateway_port']))
                    subprocess.run(['systemctl','disable','--now',args.service_name+'.service'],capture_output=True,timeout=25)
                unit.unlink(missing_ok=True);config_path.unlink(missing_ok=True)
                run(['systemctl','daemon-reload'])
                if catalog:catalog.rollback()
            raise


def arguments():
    parser=argparse.ArgumentParser(description='Install or update ZARIB without restarting 3x-ui/Xray')
    parser.add_argument('--check',action='store_true',help='Read-only compatibility check')
    parser.add_argument('--host',default=os.environ.get('ZARIB_HOST'))
    parser.add_argument('--backend',choices=['auto','sqlite','postgres'],default='auto')
    parser.add_argument('--panel-db',type=pathlib.Path)
    parser.add_argument('--core-config',type=pathlib.Path,default=pathlib.Path('/usr/local/x-ui/bin/config.json'))
    parser.add_argument('--panel-unit',default='x-ui.service')
    parser.add_argument('--web-port',type=int)
    parser.add_argument('--gateway-port',type=int)
    parser.add_argument('--relay-port',type=int)
    parser.add_argument('--listen',default='0.0.0.0',choices=['0.0.0.0','127.0.0.1'])
    parser.add_argument('--prefix',type=pathlib.Path,default=pathlib.Path('/opt/farstar-inbound-multiplier'))
    parser.add_argument('--config-dir',type=pathlib.Path,default=pathlib.Path('/etc/farstar-inbound-multiplier'))
    parser.add_argument('--state-dir',type=pathlib.Path,default=pathlib.Path('/var/lib/farstar-inbound-multiplier'))
    parser.add_argument('--service-name',default='farstar-inbound-multiplier')
    args=parser.parse_args()
    for value in [args.service_name,args.panel_unit]:
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}',value):parser.error('Invalid service name')
    for name in ['prefix','config_dir','state_dir']:
        path=getattr(args,name).resolve()
        if len(path.parts)<3 or any(character.isspace() for character in str(path)):
            parser.error('Installation directories must be absolute application directories without whitespace')
        setattr(args,name,path)
    return args


if __name__=='__main__':
    os.umask(0o077)
    if os.geteuid()!=0:sys.exit('Run the installer as root.')
    try:install(arguments())
    except Exception as error:
        # Never echo a database DSN, panel cookie or generated credential.
        sys.exit('ZARIB installation failed: '+str(error))
