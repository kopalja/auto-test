"""Tiny disposable HTTP application used by the deployment integration test."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

root = Path(os.environ['AUTO_TEST_EVIDENCE_DIR'])
receipt = root / 'service.json'
run_id = os.environ['AUTO_TEST_RUN_ID']


def request(path, data=None):
    port = json.loads(receipt.read_text())['port']
    with urlopen(Request(f'http://127.0.0.1:{port}{path}', data=data), timeout=2) as response:
        return response.read().decode()


def resource(action):
    with open(os.environ['AUTO_TEST_RESOURCE_MANIFEST'], 'a') as handle:
        handle.write(json.dumps({'action': action, 'kind': 'http-service', 'name': run_id,
                                 'target': '127.0.0.1'}) + '\n')


def serve():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == '/shutdown':
                content = 'stopping'
                threading.Thread(target=self.server.shutdown).start()
            elif self.path == '/health':
                content = run_id
            elif self.path == '/result' and (root / 'value').exists():
                content = (root / 'value').read_text()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(content.encode())

        def do_POST(self):
            (root / 'value').write_bytes(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(201)
            self.end_headers()

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        receipt.write_text(json.dumps({'port': server.server_port, 'run_id': run_id}))
        server.serve_forever(poll_interval=0.01)
    (root / 'stopped').touch()


def main():
    action = sys.argv[1]
    if action == 'serve':
        serve()
        return
    if action == 'deploy':
        with open(root / 'server.log', 'w') as log:
            subprocess.Popen([sys.executable, __file__, 'serve'], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        for _ in range(200):
            if receipt.exists():
                break
            time.sleep(0.01)
        resource('created')
        assert request('/health') == run_id
        print('deployed; GET /health returned this run identity')
    elif action == 'workflow':
        request('/value', b'synthetic-input')
        assert request('/result') == 'synthetic-input'
        print('POST synthetic-input; GET /result returned synthetic-input')
    elif action == 'boundary':
        try:
            request('/missing')
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError('Expected HTTP 404')
        assert request('/health') == run_id
        print('Unknown path returned 404; service remained healthy')
    elif action == 'teardown':
        if receipt.exists():
            assert json.loads(receipt.read_text())['run_id'] == run_id
            try:
                request('/shutdown')
            except URLError:
                pass
            for _ in range(200):
                if (root / 'stopped').exists():
                    break
                time.sleep(0.01)
            assert (root / 'stopped').exists(), 'service has not exited'
            try:
                request('/health')
            except URLError:
                pass
            else:
                raise AssertionError('Service still reachable')
            resource('removed')
        print('service exited; listener absent')


if __name__ == '__main__':
    main()
