"""Read-only localhost preview of validated candidates; never serves caches, credentials or source trees."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import argparse
import re

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT
FILES = {
         '/scenarios.html': ('scenarios.html', 'text/html'),
         '/scenarios.js': ('scenarios.js', 'text/javascript'),
         '/scenarios.css': ('scenarios.css', 'text/css'),
         '/scenario-board.json': ('test_output/scenario-public.test.json', 'application/json'),
         '/scenario-refresh-status.json': ('test_output/scenario-refresh-status.json', 'application/json'),
         '/guidance.js': ('guidance.js', 'text/javascript'),
         '/guidance.json': ('test_output/guidance.json', 'application/json'),
         '/issue-spread.js': ('issue-spread.js', 'text/javascript'),
         '/issue-spread.css': ('issue-spread.css', 'text/css'),
         '/issue-spread.json': ('test_output/issue-spread.test.json', 'application/json'),
         '/sampro.js': ('sampro.js', 'text/javascript'),
         '/sampro.css': ('sampro.css', 'text/css'),
         '/sampro-market.json': ('sampro-market.json', 'application/json'),
         '/saveticker.js': ('saveticker.js', 'text/javascript'),
         '/saveticker.css': ('saveticker.css', 'text/css'),
         '/saveticker-market.json': ('saveticker-market.json', 'application/json'),
'/': ('index.html', 'text/html'), '/index.html': ('index.html', 'text/html'),
         '/research.js': ('research.js', 'text/javascript'),
         '/refresh-status.json': ('test_output/refresh-status.test.json', 'application/json'),
         '/data.json': ('test_output/data.test.json', 'application/json'),
         '/youtube-market.json': ('test_output/youtube-market.test.json', 'application/json'),
         '/combined-recommendations.json': ('test_output/combined-recommendations.test.json', 'application/json'),
         '/recommendation-performance.json': ('test_output/recommendation-performance.test.json', 'application/json')}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path).path
        item = FILES.get(route)
        if re.fullmatch(r'/scenario-stocks/[a-f0-9]{20}/[0-9]{2}\.json', route):
            item = ('test_output' + route, 'application/json')
        source_root = DATA_ROOT if item and item[0].startswith('test_output/') else ROOT
        if not item or not (source_root/item[0]).is_file():
            self.send_error(404)
            return
        raw = (source_root/item[0]).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', item[1] + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-root', type=Path, default=ROOT,
                        help='기존 검증 test_output이 있는 작업 폴더')
    args = parser.parse_args()
    DATA_ROOT = args.data_root.resolve()
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
