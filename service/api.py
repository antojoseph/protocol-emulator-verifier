"""Operator API, deliberately bound only to localhost behind an IAP SSH tunnel."""
import base64
import json
import os
import re
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from .core import MAX_ARCHIVE, Store


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def respond(self, code, value):
        body = json.dumps(value, default=str, allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        try:
            # The SSH tunnel keeps the client's local port in Host. Accept only
            # literal loopback names, so DNS rebinding cannot bypass this boundary.
            host = self.headers.get('Host', '').lower()
            if (not re.fullmatch(r'(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?', host)
                    or self.headers.get('Origin') is not None):
                return self.respond(403, {'error': 'Loopback operator clients only'})
            if self.headers.get('X-ASIC-Client') != '1':
                return self.respond(403, {'error': 'Operator client header required'})
            path = urlsplit(self.path).path.strip('/').split('/')
            if self.command == 'GET' and path == ['health']:
                with self.server.store.connection() as db:
                    db.execute('SELECT 1')
                return self.respond(200, {'status': 'ok'})
            if self.command == 'POST' and path == ['jobs']:
                if self.headers.get('Transfer-Encoding'):
                    raise ValueError('Chunked uploads are not supported')
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 4 * MAX_ARCHIVE // 3 + 4096:
                    raise ValueError('Invalid request size')
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError('Incomplete request')
                data = json.loads(body)
                if not isinstance(data, dict) or set(data) != {'archive', 'mode', 'request_key'}:
                    raise ValueError('Expected archive, mode, request_key')
                if not all(isinstance(value, str) for value in data.values()):
                    raise ValueError('Request fields must be strings')
                blob = base64.b64decode(data['archive'], validate=True)
                job = self.server.store.submit(blob, data['mode'], data['request_key'])
                return self.respond(202, {'id': job})
            if len(path) == 2 and path[0] == 'jobs' and self.command == 'GET':
                row = self.server.store.get(path[1])
                return self.respond(200 if row else 404, row or {'error': 'Not found'})
            if len(path) == 3 and path[0] == 'jobs' and path[2] == 'cancel' and self.command == 'POST':
                return self.respond(200, {'cancel_requested': self.server.store.cancel(path[1])})
            self.respond(404, {'error': 'Not found'})
        except (ValueError, TypeError, KeyError, zipfile.BadZipFile) as error:
            self.respond(400, {'error': str(error)})
        except OverflowError as error:
            self.respond(429, {'error': str(error)})
        except Exception:
            # Do not expose database credentials or tracebacks over the API.
            import traceback
            traceback.print_exc()
            self.respond(503, {'error': 'Service unavailable'})

    do_GET = dispatch
    do_POST = dispatch


def main():
    server = HTTPServer(('127.0.0.1', int(os.environ.get('ASIC_PORT', '8123'))), Handler)
    server.store = Store(os.environ['ASIC_DATABASE_URL'])
    server.serve_forever()


if __name__ == '__main__':
    main()
