"""An independent, protocol-compatible Xray accounting gateway.

It leaves data sockets and upstream panel files untouched. Scoped runtime user
emails distinguish shared accounts; the gateway restores public emails before
the official panel receives statistics or handler responses.
"""
import base64
import decimal
import json
import logging
import sqlite3
import threading
import time
from concurrent import futures

import grpc
from app.stats.command import command_pb2 as stats
from app.proxyman.command import command_pb2 as handlers
from common.protocol import user_pb2
from common.serial import typed_message_pb2
from proxy.vless.inbound import config_pb2 as vless

PREFIX = "irgcoef1."
STAT = "/xray.app.stats.command.StatsService/"
HANDLER = "/xray.app.proxyman.command.HandlerService/"


def scoped(inbound_id, email):
    if not email or int(inbound_id) <= 0:
        raise ValueError("Empty or invalid traffic identity")
    encoded = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    return f"{PREFIX}{int(inbound_id)}.{encoded}"


def unscoped(email):
    if not email.startswith(PREFIX):
        return None
    try:
        inbound, encoded = email[len(PREFIX):].split(".", 1)
        original = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode()
        if int(inbound) <= 0 or not original or scoped(int(inbound), original) != email:
            return None
        return int(inbound), original
    except (ValueError, UnicodeError):
        return None


def public_name(name):
    parts = name.split(">>>")
    if len(parts) >= 2 and parts[0] == "user":
        parsed = unscoped(parts[1])
        if parsed:
            parts[1] = parsed[1]
            return ">>>".join(parts)
    parsed = unscoped(name)
    return parsed[1] if parsed else name


def missing(error):
    return error.code() == grpc.StatusCode.NOT_FOUND or any(
        word in error.details().lower() for word in ["not found", "not exist", "doesn't exist"]
    )


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS inbounds (
                id INTEGER PRIMARY KEY, tag TEXT NOT NULL, remark TEXT NOT NULL,
                port INTEGER NOT NULL, protocol TEXT NOT NULL,
                milli INTEGER NOT NULL DEFAULT 1000 CHECK(milli BETWEEN 0 AND 10000),
                instrumented INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS fractions (
                name TEXT PRIMARY KEY, remainder INTEGER NOT NULL DEFAULT 0,
                raw INTEGER NOT NULL DEFAULT 0, billed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY, inbound_id INTEGER, old_milli INTEGER,
                new_milli INTEGER, changed_at INTEGER);
            CREATE TABLE IF NOT EXISTS counter_offsets (
                name TEXT PRIMARY KEY, generation TEXT NOT NULL,
                raw_boundary INTEGER NOT NULL, offset_milli INTEGER NOT NULL);
        """)
        self.db.commit()
        self.generation='unbound'

    def sync(self, inbounds):
        with self.lock, self.db:
            self.db.execute("UPDATE inbounds SET active=0")
            for item in inbounds:
                self.db.execute("""INSERT INTO inbounds(id,tag,remark,port,protocol)
                    VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    tag=excluded.tag,remark=excluded.remark,port=excluded.port,protocol=excluded.protocol,active=1""",
                    (int(item["id"]), item["tag"], item["remark"], int(item["port"]), item["protocol"]))

    def rows(self):
        with self.lock:
            cursor = self.db.execute("SELECT id,tag,remark,port,protocol,milli,instrumented,active FROM inbounds ORDER BY id")
            return [dict(zip([c[0] for c in cursor.description], row)) for row in cursor]

    def row_for_tag(self, tag):
        return next((r for r in self.rows() if r["tag"] == tag and r['active']), None)

    def set_factor(self, inbound_id, value, samples=None):
        number = decimal.Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= 10 or number * 1000 != int(number * 1000):
            raise ValueError("ضریب باید عددی بین صفر و ۱۰ و حداکثر سه رقم اعشار باشد")
        milli = int(number * 1000)
        with self.lock, self.db:
            row = self.db.execute("SELECT protocol,milli,instrumented FROM inbounds WHERE id=? AND active=1", (int(inbound_id),)).fetchone()
            if row is None:
                raise ValueError("اینباند پیدا نشد")
            if row[0] != "vless":
                raise ValueError("این افزونه فعلاً اینباندهای VLESS را پشتیبانی می‌کند")
            if milli!=row[1] and samples is not None:
                for name,raw in samples:
                    parts=name.split('>>>')
                    identity=unscoped(parts[1]) if len(parts)==4 and parts[0]=='user' and parts[2]=='traffic' else None
                    if not identity or identity[0]!=int(inbound_id):continue
                    previous=self.db.execute('SELECT generation,raw_boundary,offset_milli FROM counter_offsets WHERE name=?',(name,)).fetchone()
                    offset=previous[2] if previous and previous[0]==self.generation and raw>=previous[1] else 0
                    offset+=(row[1]-milli)*raw
                    self.db.execute('INSERT INTO counter_offsets(name,generation,raw_boundary,offset_milli) VALUES(?,?,?,?) '
                        'ON CONFLICT(name) DO UPDATE SET generation=excluded.generation,raw_boundary=excluded.raw_boundary,offset_milli=excluded.offset_milli',
                        (name,self.generation,raw,offset))
            self.db.execute("UPDATE inbounds SET milli=?,instrumented=?,updated_at=? WHERE id=?",
                            (milli, int(row[2] or milli != 1000), int(time.time()), int(inbound_id)))
            self.db.execute("INSERT INTO changes(inbound_id,old_milli,new_milli,changed_at) VALUES(?,?,?,?)",
                            (int(inbound_id), row[1], milli, int(time.time())))

    def scale(self, name, value, reset):
        if value < 0:
            raise ValueError("Negative core traffic counter")
        parts = name.split(">>>")
        identity = unscoped(parts[1]) if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic" else None
        if identity is None:
            return value
        row = self.db.execute("SELECT milli FROM inbounds WHERE id=?", (identity[0],)).fetchone()
        milli = row[0] if row else 1000
        previous = self.db.execute("SELECT remainder FROM fractions WHERE name=?", (name,)).fetchone()
        remainder = previous[0] if previous else 0
        boundary=self.db.execute('SELECT generation,raw_boundary,offset_milli FROM counter_offsets WHERE name=?',(name,)).fetchone()
        offset=0
        if boundary:
            if boundary[0]==self.generation and value>=boundary[1]:offset=boundary[2]
            else:self.db.execute('DELETE FROM counter_offsets WHERE name=?',(name,))
        billed, remainder_next = divmod(value * milli + offset + remainder, 1000)
        if billed > (1 << 63) - 1:
            raise OverflowError("Billed traffic exceeds int64")
        if reset:
            self.db.execute('DELETE FROM counter_offsets WHERE name=?',(name,))
            self.db.execute("""INSERT INTO fractions(name,remainder,raw,billed) VALUES(?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET remainder=excluded.remainder,
                raw=fractions.raw+excluded.raw,billed=fractions.billed+excluded.billed""",
                (name, remainder_next, value, billed))
        return billed


class Gateway(grpc.GenericRpcHandler):
    def __init__(self, channel, store, metadata_refresh=None, generation_provider=None):
        self.channel = channel
        self.store = store
        self.handler_lock = threading.RLock()
        self.metadata_refresh = metadata_refresh
        self.generation_provider=generation_provider
        self.last_error = ""
        self.last_rpc_at = 0

    def call(self, method, message, timeout=10):
        payload = message.SerializeToString() if hasattr(message, "SerializeToString") else message
        return self.channel.unary_unary(method)(payload, timeout=timeout)

    def service(self, details):
        method = details.method
        if method.endswith('/FollowLog') or method.endswith('/SubscribeRoutingStats'):
            def stream(payload, context):
                upstream=None
                try:
                    upstream=self.channel.unary_stream(method)(payload)
                    context.add_callback(upstream.cancel)
                    for item in upstream:
                        if not context.is_active():break
                        yield item
                except grpc.RpcError as error:
                    if context.is_active():context.abort(error.code(),error.details())
                finally:
                    if upstream:upstream.cancel()
            return grpc.unary_stream_rpc_method_handler(stream,
                request_deserializer=lambda b:b,response_serializer=lambda b:b)
        def invoke(payload, context):
            try:
                response = self.dispatch(method, payload)
                self.last_rpc_at = int(time.time())
                self.last_error = ''
                return response
            except grpc.RpcError as error:
                context.abort(error.code(), error.details())
            except Exception:
                logging.exception("Accounting RPC failed: %s", method)
                self.last_error = "خطا در رابط ثبت آمار؛ تنظیمات کاربران تغییر نکرد"
                context.abort(grpc.StatusCode.INTERNAL, "Accounting extension could not process this management request")
        return grpc.unary_unary_rpc_method_handler(invoke, request_deserializer=lambda b: b, response_serializer=lambda b: b)

    def dispatch(self, method, payload):
        if method.startswith(STAT):
            with self.store.lock, self.store.db:
                if self.generation_provider:self.store.generation=self.generation_provider()
                return self.stats_call(method[len(STAT):], payload)
        if method.startswith(HANDLER):
            with self.handler_lock:
                return self.handler_call(method[len(HANDLER):], payload)
        return self.call(method, payload)

    def change_factor(self,inbound_id,value):
        # Keep the virtual cumulative counter continuous when a coefficient is
        # edited. Current 3x-ui computes deltas from non-resetting counters.
        with self.handler_lock,self.store.lock,self.store.db:
            if self.generation_provider:self.store.generation=self.generation_provider()
            response=stats.QueryStatsResponse.FromString(self.call(STAT+'QueryStats',stats.QueryStatsRequest(reset=False)))
            self.store.set_factor(inbound_id,value,[(item.name,item.value) for item in response.stat])

    def aliases(self, name):
        parts = name.split(">>>")
        if len(parts) < 2 or parts[0] != "user" or not parts[1] or unscoped(parts[1]):
            return [name]
        aliases = [name]
        for row in self.store.rows():
            if row["instrumented"]:
                alternate = list(parts)
                alternate[1] = scoped(row["id"], parts[1])
                aliases.append(">>>".join(alternate))
        return aliases

    def combine_stats(self, records, reset):
        combined = {}
        for record in records:
            name = public_name(record.name)
            value = self.store.scale(record.name, record.value, reset)
            if name not in combined:
                combined[name] = stats.Stat()
                combined[name].CopyFrom(record)
                combined[name].name = name
                combined[name].value = value
            else:
                combined[name].value += value
        return list(combined.values())

    def stats_call(self, operation, payload):
        method = STAT + operation
        if operation == "QueryStats":
            request = stats.QueryStatsRequest.FromString(payload)
            # The core matches arbitrary substrings. Match against public names,
            # then reset only selected counters, so a filtered query never drains others.
            invariant = request.pattern in ['', 'user>>>', 'inbound>>>', 'outbound>>>']
            query = stats.QueryStatsRequest(pattern=request.pattern if invariant else '',
                                            reset=request.reset if invariant else False)
            combined = stats.QueryStatsResponse.FromString(self.call(method, query))
            if not invariant:
                selected=[]
                for record in combined.stat:
                    if request.pattern not in public_name(record.name):continue
                    if request.reset:
                        try:
                            record=stats.GetStatsResponse.FromString(self.call(STAT+'GetStats',
                                stats.GetStatsRequest(name=record.name,reset=True))).stat
                        except grpc.RpcError as error:
                            if missing(error):continue
                            raise
                    selected.append(record)
                del combined.stat[:];combined.stat.extend(selected)
            result = stats.QueryStatsResponse()
            result.stat.extend(self.combine_stats(combined.stat, request.reset))
            return result.SerializeToString()
        if operation in ["GetStats", "GetStatsOnline"]:
            request = stats.GetStatsRequest.FromString(payload)
            if operation == "GetStatsOnline" and len(self.aliases(request.name)) > 1:
                # A source IP using two inbounds is one online source, not two.
                ips = stats.GetStatsOnlineIpListResponse.FromString(
                    self.stats_call("GetStatsOnlineIpList", payload))
                return stats.GetStatsResponse(stat=stats.Stat(
                    name=request.name, value=len(ips.ips))).SerializeToString()
            records = []
            last_error = None
            for name in self.aliases(request.name):
                query = stats.GetStatsRequest();query.CopyFrom(request);query.name = name
                try:
                    response = stats.GetStatsResponse.FromString(self.call(method, query))
                    records.append(response.stat)
                except grpc.RpcError as error:
                    if not missing(error):raise
                    last_error = error
            if not records:
                if last_error:raise last_error
                return self.call(method, payload)
            result = stats.GetStatsResponse()
            for record in self.combine_stats(records, request.reset and operation == "GetStats"):
                result.stat.name = request.name
                result.stat.value += record.value
            return result.SerializeToString()
        if operation == "GetStatsOnlineIpList":
            request = stats.GetStatsRequest.FromString(payload)
            result = stats.GetStatsOnlineIpListResponse(name=request.name)
            found = False
            for name in self.aliases(request.name):
                query = stats.GetStatsRequest();query.CopyFrom(request);query.name = name
                try:
                    response = stats.GetStatsOnlineIpListResponse.FromString(self.call(method, query))
                    found = True
                    for ip, last_seen in response.ips.items():
                        result.ips[ip] = max(last_seen, result.ips.get(ip, 0))
                except grpc.RpcError as error:
                    if not missing(error):raise
            return result.SerializeToString() if found else self.call(method, payload)
        if operation == "GetAllOnlineUsers":
            response = stats.GetAllOnlineUsersResponse.FromString(self.call(method, payload))
            users = list(dict.fromkeys(public_name(name) for name in response.users))
            del response.users[:];response.users.extend(users)
            return response.SerializeToString()
        if operation == "GetUsersStats":
            request = stats.GetUsersStatsRequest.FromString(payload)
            response = stats.GetUsersStatsResponse.FromString(self.call(method, payload))
            combined = {}
            for user in response.users:
                original = public_name(user.email)
                if original not in combined:
                    combined[original] = stats.UserStat(email=original)
                target = combined[original]
                for direction in ["uplink", "downlink"]:
                    raw = getattr(user.traffic, direction)
                    count = self.store.scale("user>>>" + user.email + ">>>traffic>>>" + direction,
                                             raw, request.reset and request.include_traffic)
                    setattr(target.traffic, direction, getattr(target.traffic, direction) + count)
                addresses = {ip.ip: ip.last_seen for ip in target.ips}
                for ip in user.ips:addresses[ip.ip] = max(ip.last_seen, addresses.get(ip.ip, 0))
                del target.ips[:]
                for ip, last_seen in addresses.items():target.ips.add(ip=ip, last_seen=last_seen)
            del response.users[:];response.users.extend(combined.values())
            return response.SerializeToString()
        return self.call(method, payload)

    def scope_users(self, inbound, reverse=False):
        settings = inbound.proxy_settings
        if settings.type != "xray.proxy.vless.inbound.Config":
            return False
        row = self.store.row_for_tag(inbound.tag)
        if not reverse and (not row or not row["instrumented"]):
            return False
        config = vless.Config.FromString(settings.value)
        for user in config.users:
            if reverse:
                user.email = public_name(user.email)
            else:
                user.email = scoped(row["id"], user.email)
        settings.value = config.SerializeToString()
        return True

    def handler_call(self, operation, payload):
        method = HANDLER + operation
        if operation == "AddInbound":
            request = handlers.AddInboundRequest.FromString(payload)
            if not self.store.row_for_tag(request.inbound.tag) and self.metadata_refresh:
                self.metadata_refresh()
            self.scope_users(request.inbound)
            return self.call(method, request)
        if operation == "ListInbounds":
            response = handlers.ListInboundsResponse.FromString(self.call(method, payload))
            for inbound in response.inbounds:self.scope_users(inbound, reverse=True)
            return response.SerializeToString()
        if operation == "GetInboundUsers":
            request = handlers.GetInboundUserRequest.FromString(payload)
            wanted = public_name(request.email)
            request.email = ""
            response = handlers.GetInboundUserResponse.FromString(self.call(method, request))
            selected = {}
            for user in response.users:
                user.email = public_name(user.email)
                if not wanted or user.email == wanted:selected[user.email] = user
            del response.users[:];response.users.extend(selected.values())
            return response.SerializeToString()
        if operation == "AlterInbound":
            request = handlers.AlterInboundRequest.FromString(payload)
            row = self.store.row_for_tag(request.tag)
            if not row or not row["instrumented"]:
                return self.call(method, payload)
            if request.operation.type == "xray.app.proxyman.command.AddUserOperation":
                add = handlers.AddUserOperation.FromString(request.operation.value)
                add.user.email = scoped(row["id"], add.user.email)
                request.operation.value = add.SerializeToString()
                return self.call(method, request)
            if request.operation.type == "xray.app.proxyman.command.RemoveUserOperation":
                remove = handlers.RemoveUserOperation.FromString(request.operation.value)
                original = public_name(remove.email)
                removed = False
                last_error = None
                for email in [scoped(row["id"], original), original]:
                    delete = handlers.RemoveUserOperation(email=email)
                    request.operation.value = delete.SerializeToString()
                    try:self.call(method, request);removed = True
                    except grpc.RpcError as error:
                        if not missing(error):raise
                        last_error = error
                if not removed and last_error:raise last_error
                return handlers.AlterInboundResponse().SerializeToString()
        return self.call(method, payload)

    def reconcile(self):
        """Change authentication metadata without removing an inbound or a socket."""
        for row in self.store.rows():
            if not row['active'] or not row["instrumented"] or row["protocol"] != "vless":continue
            with self.handler_lock:
                request = handlers.GetInboundUserRequest(tag=row["tag"])
                try:
                    response = handlers.GetInboundUserResponse.FromString(self.call(HANDLER + "GetInboundUsers", request))
                except grpc.RpcError as error:
                    if missing(error):continue
                    raise
                for user in response.users:
                    if unscoped(user.email) or not user.email:continue
                    original = user_pb2.User();original.CopyFrom(user)
                    replacement = user_pb2.User();replacement.CopyFrom(user)
                    replacement.email = scoped(row["id"], original.email)
                    remove = handlers.RemoveUserOperation(email=original.email)
                    remove_request = handlers.AlterInboundRequest(tag=row["tag"], operation=typed_message_pb2.TypedMessage(
                        type="xray.app.proxyman.command.RemoveUserOperation", value=remove.SerializeToString()))
                    self.call(HANDLER + "AlterInbound", remove_request)
                    add = handlers.AddUserOperation(user=replacement)
                    add_request = handlers.AlterInboundRequest(tag=row["tag"], operation=typed_message_pb2.TypedMessage(
                        type="xray.app.proxyman.command.AddUserOperation", value=add.SerializeToString()))
                    try:self.call(HANDLER + "AlterInbound", add_request)
                    except Exception:
                        rollback = handlers.AddUserOperation(user=original)
                        add_request.operation.value = rollback.SerializeToString()
                        self.call(HANDLER + "AlterInbound", add_request)
                        raise


def make_server(gateway, address):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16), options=[
        ("grpc.max_receive_message_length", 16 * 1024 * 1024),
        ("grpc.max_send_message_length", 16 * 1024 * 1024)])
    server.add_generic_rpc_handlers((gateway,))
    if not server.add_insecure_port(address):raise RuntimeError("Accounting management port is busy")
    return server
