import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import json

sys.path.insert(0, str(pathlib.Path(__file__).parent / 'generated'))
import grpc
from accounting import Store, Gateway, make_server, scoped, unscoped, public_name, STAT, HANDLER
from app.stats.command import command_pb2 as stats
from app.proxyman.command import command_pb2 as handlers
from common.protocol import user_pb2
from common.serial import typed_message_pb2
from transport import MarkedRelay
from redirect import rule,ensure,remove,command


class MathTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.store.sync([dict(id=i, tag='test'+str(i), remark='test', port=10000+i, protocol='vless') for i in [1, 2]])

    def scale(self, identifier, number, reset=True):
        name = 'user>>>' + scoped(identifier, 'shared@example.com') + '>>>traffic>>>downlink'
        with self.store.lock, self.store.db:
            return self.store.scale(name, number, reset)

    def test_fraction_and_preview_do_not_double_charge(self):
        self.store.set_factor(1, '.5')
        self.assertEqual(self.scale(1, 1), 0)
        self.assertEqual(self.scale(1, 1, False), 1)
        self.assertEqual(self.scale(1, 1, False), 1)
        self.assertEqual(self.scale(1, 1), 1)
        self.assertEqual(self.scale(1, 1), 0)
        self.assertEqual(self.scale(1, 1), 1)

    def test_shared_accounts_are_independent(self):
        self.store.set_factor(1, 2)
        self.store.set_factor(2, '.5')
        self.assertEqual(self.scale(1, 1000), 2000)
        self.assertEqual(self.scale(2, 1000), 500)
        with self.store.lock, self.store.db:
            self.assertEqual(self.store.scale('inbound>>>test1>>>traffic>>>downlink', 1000, True), 1000)
            self.assertEqual(self.store.scale('user>>>shared@example.com>>>traffic>>>downlink', 1000, True), 1000)

    def test_zero_factor_and_return_to_one(self):
        self.store.set_factor(1, 0)
        self.assertEqual(self.scale(1, 99999), 0)
        self.store.set_factor(1, 1)
        self.assertEqual(self.scale(1, 100), 100)
        self.assertTrue(self.store.rows()[0]['instrumented'])

    def test_reject_invalid_factor(self):
        for value in ['-1', '10.001', 'NaN', 'Infinity', '0.0001', 'wrong']:
            with self.assertRaises((ValueError, ArithmeticError)):
                self.store.set_factor(1, value)
        self.assertEqual(self.store.rows()[0]['milli'], 1000)

    def test_identity_and_metadata_roundtrip(self):
        for email in ['shared@example.com', 'فارستار-کاربر', 'user1', 'a.b+c@example.org']:
            value = scoped(1, email)
            self.assertEqual(unscoped(value), (1, email))
            self.assertEqual(public_name('user>>>'+value+'>>>traffic>>>uplink'), 'user>>>'+email+'>>>traffic>>>uplink')
        for malformed in ['irgcoef1.bad.abc', 'irgcoef1.0.dGVzdA', 'irgcoef1.1.', 'irgcoef1.1.%%%%']:
            self.assertIsNone(unscoped(malformed))

    def test_generic_query_prefix_is_not_expanded(self):
        self.store.set_factor(1, 2)
        gateway = Gateway(None, self.store)
        self.assertEqual(gateway.aliases('user>>>'), ['user>>>'])
        self.assertEqual(len(gateway.aliases('user>>>shared@example.com>>>traffic')), 2)

    def test_deleted_tag_does_not_inherit_old_factor(self):
        self.store.set_factor(1,2)
        self.store.sync([dict(id=3,tag='test1',remark='replacement',port=10001,protocol='vless')])
        self.assertEqual(self.store.row_for_tag('test1')['id'],3)
        self.assertEqual(self.store.row_for_tag('test1')['milli'],1000)

    def test_ten_gigabyte_subscription_with_three_different_factors(self):
        self.store.sync([dict(id=i,tag='test'+str(i),remark='test',port=10000+i,protocol='vless') for i in [1,2,3]])
        self.store.set_factor(1,'1.5')
        self.store.set_factor(3,'5')
        gib=1024**3
        remaining=10*gib
        remaining-=self.scale(1,2*gib)
        self.assertEqual(remaining,7*gib)
        # Direct inbound retains its default factor of one for this shared account.
        remaining-=self.scale(2,2*gib)
        self.assertEqual(remaining,5*gib)
        remaining-=self.scale(3,gib)
        self.assertEqual(remaining,0)

    def test_cumulative_factor_changes_never_reprice_previous_traffic(self):
        name='user>>>'+scoped(1,'shared@example.com')+'>>>traffic>>>downlink'
        self.store.set_factor(1,'1.5')
        self.assertEqual(self.scale(1,100,False),150)
        self.store.set_factor(1,'2',[(name,100)])
        self.assertEqual(self.scale(1,100,False),150)
        self.assertEqual(self.scale(1,150,False),250)
        self.store.set_factor(1,'.5',[(name,150)])
        self.assertEqual(self.scale(1,150,False),250)
        self.assertEqual(self.scale(1,200,False),275)

    def test_cumulative_boundary_does_not_cross_core_generations(self):
        name='user>>>'+scoped(1,'shared@example.com')+'>>>traffic>>>downlink'
        self.store.set_factor(1,2)
        self.store.set_factor(1,.5,[(name,100)])
        self.assertEqual(self.scale(1,100,False),200)
        self.store.generation='a-new-core-process'
        self.assertEqual(self.scale(1,1000,False),500)

    def test_reset_collection_clears_coefficient_boundary(self):
        name='user>>>'+scoped(1,'shared@example.com')+'>>>traffic>>>downlink'
        self.store.set_factor(1,2)
        self.store.set_factor(1,.5,[(name,100)])
        self.assertEqual(self.scale(1,150,True),225)
        self.assertEqual(self.scale(1,100,True),50)


def wait_port(port):
    for _ in range(100):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.2):return
        except OSError:time.sleep(.05)
    raise RuntimeError('Private test listener not ready')


def exact(sock, length):
    result = b''
    while len(result) < length:
        part = sock.recv(length-len(result))
        if not part:raise RuntimeError('Persistent private stream was interrupted')
        result += part
    return result


class CoreIntegrationTests(unittest.TestCase):
    def test_real_core_shared_users_and_persistent_connection(self):
        import socketserver, threading
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                while True:
                    chunk = self.request.recv(65536)
                    if not chunk:return
                    self.request.sendall(chunk)
        class EchoServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
        echo = EchoServer(('127.0.0.1', 62687), Echo)
        threading.Thread(target=echo.serve_forever, daemon=True).start()
        identity = str(uuid.uuid4())
        email = 'private-shared-test@example.invalid'
        with tempfile.TemporaryDirectory(prefix='farstar-multiplier-test-') as directory:
            root = pathlib.Path(directory)
            cfg = {'log': {'loglevel':'warning'}, 'api':{'tag':'api','services':['HandlerService','StatsService']},
                'stats':{}, 'policy':{'levels':{'0':{'statsUserUplink':True,'statsUserDownlink':True,'statsUserOnline':True}},
                    'system':{'statsInboundUplink':True,'statsInboundDownlink':True}},
                'inbounds':[{'listen':'127.0.0.1','port':62689,'protocol':'dokodemo-door','settings':{'address':'127.0.0.1'},'tag':'api'}] +
                    [{'listen':'127.0.0.1','port':62683+i,'protocol':'vless','tag':'private'+str(i),
                      'settings':{'clients':[{'id':identity,'email':email}],'decryption':'none'}} for i in [1,2]],
                'outbounds':[{'protocol':'freedom','tag':'direct','settings':{'finalRules':[
                    {'action':'allow','network':'tcp','ip':['127.0.0.1/32'],'port':'62687'}]}}],
                'routing':{'rules':[{'type':'field','inboundTag':['api'],'outboundTag':'api'}]}}
            (root/'config.json').write_text(json.dumps(cfg))
            log = open(root/'core.log', 'w+')
            binary=os.environ.get('XRAY_BIN','/usr/local/x-ui/bin/xray-linux-amd64')
            process = subprocess.Popen([binary,'run','-c',str(root/'config.json')],stdout=log,stderr=log)
            gateway_server = relay = channel = client = redirected = None
            redirect_rule=rule(62689,62690,'farstar-multiplier-private-test')
            streams = []
            try:
                wait_port(62689)
                relay = MarkedRelay(('127.0.0.1', 62692), ('127.0.0.1', 62689))
                relay.start()
                channel = grpc.insecure_channel('127.0.0.1:62692')
                grpc.channel_ready_future(channel).result(timeout=5)
                store = Store(str(root/'state.sqlite'))
                store.sync([dict(id=i,tag='private'+str(i),remark='private',port=62683+i,protocol='vless') for i in [1,2]])
                gateway = Gateway(channel, store,generation_provider=lambda:str(process.pid))
                gateway_server = make_server(gateway,'127.0.0.1:62690');gateway_server.start()
                client = grpc.insecure_channel('127.0.0.1:62690')
                # Exercise the exact narrow Linux redirect and marked bypass.
                ensure(redirect_rule)
                redirected=grpc.insecure_channel('127.0.0.1:62689')
                sysstats=redirected.unary_unary(STAT+'GetSysStats')(
                    stats.SysStatsRequest().SerializeToString(),timeout=5)
                self.assertIsInstance(stats.SysStatsResponse.FromString(sysstats).Uptime,int)
                self.assertEqual(command('-C',redirect_rule).returncode,0)
                def rpc(channel, operation, message, response):
                    return response.FromString(channel.unary_unary(operation)(message.SerializeToString(),timeout=5))
                def snapshot(reset, via=client):
                    return rpc(via,STAT+'QueryStats',stats.QueryStatsRequest(reset=reset),stats.QueryStatsResponse)
                def stream(port):
                    s = socket.create_connection(('127.0.0.1',port),timeout=4,source_address=('127.0.0.2',0))
                    header = b'\0'+uuid.UUID(identity).bytes+b'\0\1'+(62687).to_bytes(2,'big')+b'\1'+socket.inet_aton('127.0.0.1')
                    streams.append(s)
                    s.sendall(header+b'hello'*256)
                    self.assertEqual(exact(s, 2), b'\0\0')
                    self.assertEqual(exact(s, 1280), b'hello'*256)
                    return s
                a, b = stream(62684), stream(62685)
                time.sleep(.1)
                initial = snapshot(True)
                self.assertTrue(any(s.name=='user>>>'+email+'>>>traffic>>>downlink' and s.value>0 for s in initial.stat))
                store.set_factor(1,2);gateway.reconcile()
                a.sendall(b'old-connection-still-works')
                self.assertEqual(exact(a,26),b'old-connection-still-works')
                self.assertIsNone(process.poll())
                c = stream(62684)
                c.sendall(b'x'*8192);self.assertEqual(exact(c,8192),b'x'*8192)
                b.sendall(b'y'*4096);self.assertEqual(exact(b,4096),b'y'*4096)
                time.sleep(.1)
                raw = snapshot(False,channel)
                expected = {}
                relevant=lambda name:name.startswith('user>>>') or name.startswith('inbound>>>private')
                for record in raw.stat:
                    if not relevant(record.name):continue
                    value=record.value*(2 if scoped(1,email) in record.name else 1)
                    name=public_name(record.name)
                    expected[name]=expected.get(name,0)+value
                actual={r.name:r.value for r in snapshot(True).stat if relevant(r.name)}
                self.assertEqual(actual,expected)
                self.assertFalse(any('irgcoef1.' in r.name for r in snapshot(False).stat))
                users = rpc(client,HANDLER+'GetInboundUsers',handlers.GetInboundUserRequest(tag='private1',email=email),handlers.GetInboundUserResponse)
                self.assertEqual(len(users.users),1)
                self.assertEqual(users.users[0].email,email)
                # The exact original authentication account survives metadata scoping.
                raw_users=rpc(channel,HANDLER+'GetInboundUsers',handlers.GetInboundUserRequest(tag='private1'),handlers.GetInboundUserResponse)
                self.assertEqual(users.users[0].account,raw_users.users[0].account)
                store.set_factor(2,.5);gateway.reconcile()
                d=stream(62685);d.sendall(b'z'*2048);self.assertEqual(exact(d,2048),b'z'*2048)
                time.sleep(.1)
                raw=snapshot(False,channel)
                expected={}
                for record in raw.stat:
                    if not relevant(record.name):continue
                    with store.lock,store.db:value=store.scale(record.name,record.value,False)
                    name=public_name(record.name);expected[name]=expected.get(name,0)+value
                self.assertEqual({r.name:r.value for r in snapshot(True).stat if relevant(r.name)},expected)
                # Exercise the newer aggregate/online endpoint too; it must hide scoped names.
                online=rpc(client,STAT+'GetUsersStats',stats.GetUsersStatsRequest(include_traffic=True,reset=False),stats.GetUsersStatsResponse)
                self.assertEqual([u.email for u in online.users],[email])
                self.assertEqual(len(online.users[0].ips),1)
                c.sendall(b'filtered');self.assertEqual(exact(c,8),b'filtered')
                time.sleep(.1)
                filtered=rpc(client,STAT+'QueryStats',stats.QueryStatsRequest(pattern='private-shared-test@',reset=True),stats.QueryStatsResponse)
                self.assertEqual(len(filtered.stat),2)
                self.assertTrue(all(r.name.startswith('user>>>'+email+'>>>') for r in filtered.stat))
                self.assertTrue(any(r.value>0 for r in filtered.stat))
                self.assertTrue(all(r.value==0 for r in rpc(client,STAT+'QueryStats',stats.QueryStatsRequest(pattern=email),stats.QueryStatsResponse).stat))
                # Official-panel removal of a public email removes the scoped identity.
                operation=handlers.RemoveUserOperation(email=email)
                rpc(client,HANDLER+'AlterInbound',handlers.AlterInboundRequest(tag='private1',operation=typed_message_pb2.TypedMessage(
                    type='xray.app.proxyman.command.RemoveUserOperation',value=operation.SerializeToString())),handlers.AlterInboundResponse)
                with self.assertRaises(Exception):stream(62684)
                # Removing authentication does not tear down an established data socket.
                c.sendall(b'active');self.assertEqual(exact(c,6),b'active')
                before_change={r.name:r.value for r in snapshot(False).stat if r.name.startswith('user>>>')}
                gateway.change_factor(1,3)
                after_change={r.name:r.value for r in snapshot(False).stat if r.name.startswith('user>>>')}
                self.assertEqual(before_change,after_change,'Changing coefficient repriced historical cumulative traffic')
                down='user>>>'+email+'>>>traffic>>>downlink'
                c.sendall(b'x'*128);self.assertEqual(exact(c,128),b'x'*128)
                time.sleep(.1)
                later={r.name:r.value for r in snapshot(False).stat if r.name.startswith('user>>>')}
                self.assertEqual(later[down]-after_change[down],3*128)
                # A normal upstream/core update reloads original panel settings.
                # Verify the independent gateway re-applies saved factors after
                # that private core restarts, without patching its saved config.
                saved_config=(root/'config.json').read_bytes()
                process.terminate();process.wait(timeout=5)
                for s in streams:s.close()
                streams.clear()
                process=subprocess.Popen([binary,'run','-c',str(root/'config.json')],stdout=log,stderr=log)
                for _ in range(40):
                    try:
                        gateway.reconcile()
                        restored=rpc(channel,HANDLER+'GetInboundUsers',handlers.GetInboundUserRequest(tag='private1'),handlers.GetInboundUserResponse)
                        if any(u.email==scoped(1,email) for u in restored.users):break
                    except grpc.RpcError:pass
                    time.sleep(.15)
                else:raise RuntimeError('Saved factor did not survive the private core restart')
                after_restart=stream(62684)
                after_restart.sendall(b'update-survived')
                self.assertEqual(exact(after_restart,15),b'update-survived')
                self.assertEqual((root/'config.json').read_bytes(),saved_config)
                print('PRIVATE_UPDATE_LIFECYCLE_OK saved_config=unchanged saved_factors=reapplied',flush=True)
                print('PRIVATE_REAL_CORE_OK shared_account=2inbounds factors=2,0.5 old_connection=preserved remove_user=preserved counters=exact',flush=True)
            except Exception:
                log.flush();log.seek(0);print('PRIVATE_TEST_CORE_LOG',log.read()[-2000:])
                raise
            finally:
                for s in streams:s.close()
                remove(redirect_rule)
                if redirected:redirected.close()
                if client:client.close()
                if gateway_server:gateway_server.stop(2).wait()
                if channel:channel.close()
                if relay:relay.shutdown();relay.server_close()
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                log.close();echo.shutdown();echo.server_close()


class WebAuthTests(unittest.TestCase):
    def test_auth_origin_and_inbound_ownership(self):
        import threading,http.client,types
        import server as web
        with tempfile.TemporaryDirectory() as directory:
            config=dict(hostname='panel.example.invalid',panel_port=2053,base_path='/farstar/',
                web_port=2054,state_path=directory+'/state.sqlite',core_config=directory+'/core.json')
            pathlib.Path(config['core_config']).write_text('{"routing":{"rules":[]}}')
            app=web.Application(config)
            rows=[dict(id=i,tag='test'+str(i),remark='test',port=10000+i,protocol='vless') for i in [1,2]]
            app.store.sync(rows)
            app.gateway=types.SimpleNamespace(last_rpc_at=0,last_error='',handler_lock=threading.RLock(),change_factor=app.store.set_factor)
            app.metadata_ok_at=int(time.time())
            def inbounds(cookie=None):
                if cookie!='3x-ui=private-session':raise PermissionError('login required')
                return rows[:1]
            app.panel.inbounds=inbounds
            httpd=web.http.server.ThreadingHTTPServer(('127.0.0.1',0),web.WebHandler)
            httpd.application=app
            threading.Thread(target=httpd.serve_forever,daemon=True).start()
            def request(method,data=None,cookie=None,origin=None):
                connection=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=4)
                headers={'Host':'panel.example.invalid:2054','Content-Type':'application/json'}
                if cookie:headers['Cookie']=cookie
                if origin:headers['Origin']=origin
                body=json.dumps(data) if data is not None else None
                try:
                    connection.request(method,'/farstar/multipliers/api',body=body,headers=headers)
                    response=connection.getresponse();return response.status,json.loads(response.read())
                finally:connection.close()
            try:
                self.assertEqual(request('GET')[0],401)
                self.assertEqual(request('GET',cookie='invalid')[0],401)
                status,body=request('GET',cookie='3x-ui=private-session')
                self.assertEqual(status,200);self.assertEqual([r['id'] for r in body['inbounds']],[1])
                data={'inbound_id':1,'factor':2}
                self.assertEqual(request('POST',data,cookie='3x-ui=private-session',origin='https://evil.invalid')[0],403)
                origin='https://panel.example.invalid:2054'
                self.assertEqual(request('POST',{'inbound_id':2,'factor':2},cookie='3x-ui=private-session',origin=origin)[0],401)
                self.assertEqual(request('POST',data,cookie='3x-ui=private-session',origin=origin)[0],200)
                self.assertEqual(app.store.rows()[0]['milli'],2000)
                self.assertEqual(app.store.rows()[1]['milli'],1000)
                self.assertEqual(request('POST',{'inbound_id':1,'factor':'NaN'},cookie='3x-ui=private-session',origin=origin)[0],400)
            finally:httpd.shutdown();httpd.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
