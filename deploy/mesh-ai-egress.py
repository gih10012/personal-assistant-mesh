"""HTTPS CONNECT egress on the Mesh VPS. Reach it over the existing SSH trust."""
import argparse
import http.server
import ipaddress
import re
import select
import socket
import socketserver
import threading
import time


class Gateway(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, port, destinations):
        self.destinations = frozenset(destinations)
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(('127.0.0.1', port), Connect)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def connect_public(host):
    for family, typ, protocol, _, address in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(address[0]).is_global:
            continue
        stream = socket.socket(family, typ, protocol)
        stream.settimeout(8)
        try:
            stream.connect(address)
            return stream
        except OSError:
            stream.close()
    raise OSError('authorized AI destination unavailable')


class Connect(http.server.BaseHTTPRequestHandler):
    rbufsize = 0
    timeout = 8
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        pass

    def do_CONNECT(self):
        host, _, port = self.path.rpartition(':')
        if port != '443' or host.lower() not in self.server.destinations:
            self.send_error(403)
            return
        started = time.monotonic()
        try:
            outbound = connect_public(host.lower())
        except OSError:
            self.send_error(502)
            return
        self.close_connection = True
        with outbound:
            self.send_response_only(200, 'Connection Established')
            self.end_headers()
            peers = {self.connection: outbound, outbound: self.connection}
            try:
                while True:
                    ready, _, _ = select.select(tuple(peers), (), (), 900)
                    if not ready:
                        break
                    for origin in ready:
                        block = origin.recv(65536)
                        if not block:
                            return
                        peers[origin].sendall(block)
            except OSError:
                pass
            finally:
                # Audit only the approved host and tunnel duration.
                print('ai-connect {} {:.1f}s'.format(host.lower(), time.monotonic()-started), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--allow', required=True, action='append')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 1 <= len(args.allow) <= 32:
        parser.error('invalid port or destination count')
    if any(not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', host)
           or host.split('.')[-1].isdigit() or len(host) > 253 for host in args.allow):
        parser.error('use explicit public DNS hostnames')
    with Gateway(args.port, args.allow) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
