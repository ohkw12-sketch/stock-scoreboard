"""Read-only localhost preview of validated candidates; never serves caches, credentials or source trees."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import argparse

ROOT = Path(__file__).resolve().parent
FILES = {'/': ('index.html', 'text/html'), '/index.html': ('index.html', 'text/html'),
         '/research.js': ('research.js', 'text/javascript'),
         '/refresh-status.json': ('test_output/refresh-status.test.json', 'application/json'),
         '/data.json': ('test_output/data.test.json', 'application/json'),
         '/youtube-market.json': ('test_output/youtube-market.test.json', 'application/json'),
         '/combined-recommendations.json': ('test_output/combined-recommendations.test.json', 'application/json'),
         '/recommendation-performance.json': ('test_output/recommendation-performance.test.json', 'application/json')}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        item = FILES.get(urlsplit(self.path).path)
        if not item or not (ROOT/item[0]).is_file():
            self.send_error(404)
            return
        raw = (ROOT/item[0]).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', item[1] + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
