"""Root-only installation smoke test using its own core, database and service.

Uses localhost 62584/62587/62589/62590/62592/62593/62594 and a uniquely named
systemd service. It never stops, restarts or changes the real x-ui service.
"""
import http.client
import http.server
import json
import os
import pathlib
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'generated'))
from redirect import rule,remove


def exact(connection,length):
    result=b''
    while len(result)<length:
        part=connection.recv(length-len(result))
        if not part:raise RuntimeError('Data connection was interrupted')
        result+=part
    return result


def main():
    if os.geteuid()!=0:raise RuntimeError('Run this isolated smoke test as root')
    service='zarib-install-smoke'
    prefix=pathlib.Path('/opt/zarib-install-smoke')
    config=pathlib.Path('/etc/zarib-install-smoke')
    state=pathlib.Path('/var/lib/zarib-install-smoke')
    unit=pathlib.Path('/etc/systemd/system')/(service+'.service')
    for target in [prefix,config,state,unit]:
        if target.exists():raise RuntimeError('Previous smoke-test state exists: '+str(target))
    for port in [62584,62587,62589,62590,62592,62593,62594]:
        with socket.socket() as check:check.bind(('127.0.0.1',port))
    identity=str(uuid.uuid4())
    email='install-smoke@example.invalid'
    streams=[]
    process=echo=panel=None
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                chunk=self.request.recv(65536)
                if not chunk:return
                self.request.sendall(chunk)
    class EchoServer(socketserver.ThreadingTCPServer):
        allow_reuse_address=True
        daemon_threads=True
    class FakePanel(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            status=200 if self.headers.get('Cookie')=='3x-ui=smoke-test' else 401
            body=json.dumps({'success':status==200,'obj':[{'id':1}]}).encode()
            self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def web(method='GET',body=None):
        connection=http.client.HTTPConnection('127.0.0.1',62594,timeout=8)
        try:
            headers={'Cookie':'3x-ui=smoke-test','Content-Type':'application/json','Origin':'http://127.0.0.1:62594'}
            connection.request(method,'/test-panel/multipliers/api',json.dumps(body).encode() if body else None,headers)
            response=connection.getresponse();data=json.loads(response.read())
            if response.status!=200:raise RuntimeError('Smoke API '+str(response.status)+': '+str(data))
            return data
        finally:connection.close()
    def stream():
        connection=socket.create_connection(('127.0.0.1',62584),timeout=6)
        streams.append(connection)
        header=b'\0'+uuid.UUID(identity).bytes+b'\0\1'+(62587).to_bytes(2,'big')+b'\1'+socket.inet_aton('127.0.0.1')
        connection.sendall(header+b'hello')
        assert exact(connection,7)==b'\0\0hello'
        return connection
    def install(arguments):
        environment=dict(os.environ,ZARIB_SOURCE_DIR=str(ROOT))
        result=subprocess.run(['bash',str(ROOT/'install.sh')]+arguments,capture_output=True,text=True,env=environment,timeout=110)
        print(result.stdout,flush=True)
        if result.returncode:raise RuntimeError('One-command smoke installation failed: '+result.stderr[-2000:])
    with tempfile.TemporaryDirectory(prefix='zarib-smoke-fixture-',dir='/var/lib') as directory:
        fixture=pathlib.Path(directory)
        try:
            database=fixture/'panel.db'
            with sqlite3.connect(database) as connection:
                connection.executescript('CREATE TABLE settings(key TEXT,value TEXT); CREATE TABLE inbounds(id INTEGER,tag TEXT,remark TEXT,port INTEGER,protocol TEXT);')
                connection.executemany('INSERT INTO settings VALUES(?,?)',[('webPort','62593'),('webBasePath','/test-panel/'),('webCertFile',''),('webKeyFile',''),('webListen','127.0.0.1')])
                connection.execute("INSERT INTO inbounds VALUES(1,'smoke-inbound','Smoke Test',62584,'vless')")
            core={'log':{'loglevel':'error'},'api':{'tag':'api','services':['HandlerService','StatsService']},'stats':{},
                  'policy':{'levels':{'0':{'statsUserUplink':True,'statsUserDownlink':True}}},
                  'inbounds':[{'listen':'127.0.0.1','port':62589,'protocol':'dokodemo-door','tag':'api','settings':{'address':'127.0.0.1'}},
                              {'listen':'127.0.0.1','port':62584,'protocol':'vless','tag':'smoke-inbound','settings':{'decryption':'none','clients':[{'id':identity,'email':email}]}}],
                  'outbounds':[{'protocol':'freedom','settings':{'finalRules':[{'action':'allow','network':'tcp','ip':['127.0.0.1/32'],'port':'62587'}]}}],
                  'routing':{'rules':[{'type':'field','inboundTag':['api'],'outboundTag':'api'}]}}
            core_file=fixture/'core.json';core_file.write_text(json.dumps(core))
            binary=os.environ.get('XRAY_BIN','/usr/local/x-ui/bin/xray-linux-amd64')
            with open(fixture/'core.log','w') as log:
                process=subprocess.Popen([binary,'run','-c',str(core_file)],stdout=log,stderr=log)
            for _ in range(60):
                try:
                    with socket.create_connection(('127.0.0.1',62584),timeout=.2):break
                except OSError:time.sleep(.1)
            original_pid=process.pid
            echo=EchoServer(('127.0.0.1',62587),Echo);threading.Thread(target=echo.serve_forever,daemon=True).start()
            panel=http.server.ThreadingHTTPServer(('127.0.0.1',62593),FakePanel);threading.Thread(target=panel.serve_forever,daemon=True).start()
            opened_before_install=stream()
            arguments=['--backend','sqlite','--panel-db',str(database),'--core-config',str(core_file),'--host','127.0.0.1',
                       '--web-port','62594','--gateway-port','62590','--relay-port','62592','--listen','127.0.0.1',
                       '--prefix',str(prefix),'--config-dir',str(config),'--state-dir',str(state),'--service-name',service,
                       '--panel-unit','zarib-fake-panel.service']
            install(arguments)
            assert process.poll() is None and process.pid==original_pid
            assert web()['inbounds'][0]['factor']==1
            assert 'panel_url' not in web()
            opened_before_install.sendall(b'after-install');assert exact(opened_before_install,13)==b'after-install'
            web('POST',{'inbound_id':1,'factor':'1.5'})
            time.sleep(5.5)
            after_factor=stream()
            first_service_pid=subprocess.check_output(['systemctl','show',service,'-p','MainPID','--value'],text=True).strip()
            install(arguments)
            assert process.poll() is None and process.pid==original_pid
            assert web()['inbounds'][0]['factor']==1.5
            assert subprocess.check_output(['systemctl','show',service,'-p','MainPID','--value'],text=True).strip()!=first_service_pid
            after_factor.sendall(b'after-upgrade');assert exact(after_factor,13)==b'after-upgrade'
            opened_before_install.sendall(b'still-open');assert exact(opened_before_install,10)==b'still-open'
            install(arguments+['--check'])
            print('INSTALLATION_SMOKE_OK fresh=one-command update=factor-preserved data-sockets=preserved core-pid=unchanged readonly-check=passed',flush=True)
        finally:
            for connection in streams:connection.close()
            subprocess.run(['systemctl','disable','--now',service+'.service'],capture_output=True,timeout=25)
            remove(rule(62589,62590))
            unit.unlink(missing_ok=True)
            subprocess.run(['systemctl','daemon-reload'],capture_output=True,timeout=15)
            if process:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            for server in [echo,panel]:
                if server:server.shutdown();server.server_close()
            for target in [prefix,config,state]:
                # These fixed, previously absent directories belong only to this smoke test.
                if target.exists():shutil.rmtree(target)


if __name__=='__main__':main()
