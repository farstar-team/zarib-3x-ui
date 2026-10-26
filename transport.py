"""A loopback relay whose upstream socket is exempt from our management redirect."""
import select
import socket
import socketserver
import threading

MARK = 0x1C0EF


class MarkedRelay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, listen, target, mark=MARK):
        self.target, self.mark = target, mark
        super().__init__(listen, RelayHandler)

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if self.server.mark:
                upstream.setsockopt(socket.SOL_SOCKET, 36, self.server.mark)
            upstream.settimeout(8)
            upstream.connect(self.server.target)
            upstream.settimeout(None)
            pair = {self.request: upstream, upstream: self.request}
            while pair:
                ready, _, _ = select.select(list(pair), [], [], 60)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        pair[source].shutdown(socket.SHUT_WR)
                        del pair[source]
                    else:
                        pair[source].sendall(data)
        except OSError:
            pass
        finally:
            upstream.close()
