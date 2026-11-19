"""Offline integration tests of the actual Node sidecar. No Docker daemon or n8n login required."""
import http.server
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'workflow-sync' / 'sync.js'


class HealthServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        is_ready = self.server.ready
        self.send_response(200 if is_ready else 503)
        self.end_headers()
        self.wfile.write(b'OK' if is_ready else b'NOT READY')

    def log_message(self, *args):
        return


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='factory-workflow-sync-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.src = self.root / 'workflows'
        self.state = self.root / 'state'
        self.bin = self.root / 'bin'
        for p in (self.src, self.state, self.bin):
            p.mkdir()
        self.calls = self.root / 'calls.log'
        self.ready_file = self.root / 'login-ready'
        self.ready_file.touch()
        self.mock_cli = self.bin / 'n8n'
        self.mock_cli.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$FACTORY_SYNC_TEST_CALLS"
if [ ! -f "$FACTORY_SYNC_TEST_READY" ]; then echo 'instance owner missing' >&2; exit 1; fi
exit 0
''')
        self.mock_cli.chmod(0o755)
        self.server = HealthServer(('127.0.0.1', 0), HealthHandler)
        self.server.ready = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.proc = None
        self.addCleanup(self.stop)
        self.write_workflow('a.json', 'factory-uuid-1')
        self.write_workflow('b.json', 'factory-uuid-2')

    def write_workflow(self, name, wid, updated=False):
        (self.src / name).write_text(json.dumps({
            'id': wid, 'name': 'Test ' + wid + (' updated' if updated else ''),
            'nodes': [], 'connections': {}, 'active': False,
        }))

    def env(self):
        return dict(os.environ,
                    PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                    FACTORY_WORKFLOW_SOURCE_DIR=str(self.src),
                    FACTORY_WORKFLOW_STATE_DIR=str(self.state),
                    FACTORY_N8N_SYNC_HEALTH_URL=f'http://127.0.0.1:{self.server.server_port}/healthz',
                    FACTORY_N8N_SYNC_INTERVAL_SEC='5',
                    FACTORY_N8N_SYNC_IMPORT_TIMEOUT_SEC='30',
                    FACTORY_N8N_IMPORT_PROJECT_ID='owner',
                    FACTORY_N8N_AUTO_IMPORT='true',
                    FACTORY_SYNC_TEST_CALLS=str(self.calls),
                    FACTORY_SYNC_TEST_READY=str(self.ready_file),
                    N8N_ENCRYPTION_KEY='stub-for-tests')

    def start(self, overrides=None):
        env = self.env()
        env.update(overrides or {})
        self.proc = subprocess.Popen(['node', str(SCRIPT)], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def stop(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.communicate(timeout=4)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.communicate(timeout=3)
                    self.fail('Importer did not exit promptly on SIGTERM')
            else:
                self.proc.communicate(timeout=3)
            self.proc = None

    def calls_list(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def wait_calls(self, expected, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(self.calls_list()) >= expected:
                return self.calls_list()
            time.sleep(.07)
        self.fail(f'Expected {expected} imports, got {self.calls_list()}')

    def test_first_start_then_no_reimport_after_restart(self):
        self.start()
        calls = self.wait_calls(2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all('import:workflow --input=' in call for call in calls))
        self.stop()
        state = json.loads((self.state / '.factory-workflow-sync-state.json').read_text())
        self.assertEqual(set(state['files']), {'a.json', 'b.json'})
        self.start()
        time.sleep(.7)
        self.assertEqual(len(self.calls_list()), 2, 'unchanged files must not overwrite n8n UI edits')
        self.stop()

    def test_only_modified_file_reimported(self):
        self.start()
        self.wait_calls(2)
        self.stop()
        self.write_workflow('b.json', 'factory-uuid-2', updated=True)
        self.start()
        calls = self.wait_calls(3)
        self.assertEqual(len(calls), 3)
        self.assertIn('b.json', calls[-1]); self.assertNotIn('a.json', calls[-1])
        self.stop()

    def test_waits_until_n8n_is_healthy(self):
        self.server.ready = False
        self.start()
        time.sleep(.8)
        self.assertEqual(len(self.calls_list()), 0)
        self.stop()
        self.server.ready = True
        self.start()
        self.wait_calls(2)
        self.stop()

    def test_import_failure_retried_and_not_marked_as_success(self):
        self.ready_file.unlink()
        self.start()
        self.wait_calls(2)
        self.stop()
        self.assertFalse((self.state / '.factory-workflow-sync-state.json').exists())
        self.ready_file.touch()
        self.start()
        self.wait_calls(4)
        self.stop()
        manifest = json.loads((self.state / '.factory-workflow-sync-state.json').read_text())
        self.assertEqual(len(manifest['files']), 2)

    def test_project_id_and_disabled_mode(self):
        self.start({'FACTORY_N8N_AUTO_IMPORT': 'false'})
        time.sleep(.7)
        self.assertEqual(len(self.calls_list()), 0)
        self.stop()
        self.start({'FACTORY_N8N_IMPORT_PROJECT_ID': 'factoryProject123'})
        calls = self.wait_calls(2)
        self.assertTrue(all('--projectId=factoryProject123' in call for call in calls))
        self.stop()

    def test_opt_in_publish_only_specific_github_workflow(self):
        # No default publishing, even when arbitrary imported JSON files exist.
        github_id='b4f94100-6cd8-5e36-aad5-69f618e8f6bb'
        self.write_workflow('github.json', github_id)
        self.start()
        self.wait_calls(3)
        self.stop()
        self.assertEqual(len(self.calls_list()),3)
        # Operator enables publishing after the workflow is already imported.
        self.start({'FACTORY_GITHUB_AUTO_PUBLISH':'true'})
        calls=self.wait_calls(5)
        self.assertEqual(len([c for c in calls if 'publish:workflow' in c]),1)
        self.assertIn('--id='+github_id, calls[-1])
        self.stop()
        # Repeated unchanged startup does not reimport or republish.
        self.start({'FACTORY_GITHUB_AUTO_PUBLISH':'true'})
        time.sleep(.5)
        self.assertEqual(len(self.calls_list()),5)
        self.stop()

    def test_bad_json_blocks_import(self):
        (self.src / 'bad.json').write_text('not json')
        self.start()
        time.sleep(.7)
        self.assertEqual(len(self.calls_list()), 0)
        self.stop()


if __name__ == '__main__':
    unittest.main(verbosity=2)
