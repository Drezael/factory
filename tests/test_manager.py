"""Run with: python3 -m unittest discover -s tests -v (no Docker required)."""
import importlib.util
import json
import os
import sys
import threading
import tempfile
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / 'manager' / 'server.py'
os.environ['FACTORY_AGENT_API_TOKEN'] = 'a' * 64
spec = importlib.util.spec_from_file_location('factory_server_test', MODULE)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class FakeEngine:
    def __init__(self):
        self.containers = {}
        self.id_counter = 0

    def __call__(self, method, path, payload=None, *, binary=False):
        url = urlsplit(path)
        segments = url.path.strip('/').split('/')
        if url.path == '/_ping':
            return b'OK' if binary else 'OK'
        if url.path == '/containers/json':
            return [dict(Id=it['Id'], State=it['State']['Status'], Labels=it['Config']['Labels'])
                    for it in self.containers.values()]
        if url.path == '/containers/create' and method == 'POST':
            self.id_counter += 1
            cid = f'fake-{self.id_counter}'
            name = parse_qs(url.query)['name'][0]
            item = {'Id': cid, 'Name': name, 'Config': payload,
                    'State': {'Status': 'created', 'ExitCode': 0, 'Running': False}}
            self.containers[name] = item
            return {'Id': cid}
        if segments[0] == 'containers' and len(segments) >= 2:
            name = segments[1]
            item = next((i for i in self.containers.values()
                         if i['Id'] == name or i['Name'] == name), None)
            if item is None:
                raise manager.EngineError(404, 'not found')
            if len(segments) == 3 and segments[2] == 'json':
                return item
            if len(segments) == 3 and segments[2] == 'logs':
                return (b'FACTORY_RESULT={"status":"ok","summary":"smoke"}\n'
                        if binary else {})
            if len(segments) == 3 and segments[2] == 'start':
                item['State'] = {'Status': 'exited', 'ExitCode': 0, 'Running': False}
                return {}
            if len(segments) == 3 and segments[2] == 'stop':
                item['State'] = {'Status': 'exited', 'ExitCode': 137, 'Running': False}
                return {}
            if len(segments) == 2 and method == 'DELETE':
                del self.containers[item['Name']]
                return {}
        raise AssertionError(f'Unexpected Docker request {method} {path}')


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.saved = (manager.AUTH_ROOT, manager.WORKSPACE_ROOT,
                      manager.HOST_AUTH_ROOT, manager.HOST_WORKSPACE_ROOT,
                      manager.NETWORK_MODE)
        manager.AUTH_ROOT = root / 'auth'
        manager.HOST_AUTH_ROOT = root / 'auth'
        manager.WORKSPACE_ROOT = root / 'workspaces'
        manager.HOST_WORKSPACE_ROOT = root / 'workspaces'
        manager.NETWORK_MODE = 'factory_egress'
        manager.AUTH_ROOT.mkdir()
        manager.WORKSPACE_ROOT.mkdir()

    def tearDown(self):
        (manager.AUTH_ROOT, manager.WORKSPACE_ROOT,
         manager.HOST_AUTH_ROOT, manager.HOST_WORKSPACE_ROOT,
         manager.NETWORK_MODE) = self.saved
        self.tempdir.cleanup()

    def test_daemon_bind_path_discovery_for_docker_desktop(self):
        saved_engine = manager.engine
        saved_hostname = manager.socket.gethostname
        try:
            manager.socket.gethostname = lambda: '1234567890ab'
            def fake_engine(method, path, payload=None, binary=False):
                self.assertEqual(method, 'GET')
                self.assertEqual(path, '/containers/1234567890ab/json')
                return {'Mounts': [
                    {'Type': 'bind', 'Destination': '/opt/factory-auth',
                     'Source': '/run/desktop/mnt/host/e/factory/data/auth'},
                    {'Type': 'bind', 'Destination': '/opt/factory-workspaces',
                     'Source': '/run/desktop/mnt/host/e/factory/data/workspaces'},
                ]}
            manager.engine = fake_engine
            auth, ws = manager.daemon_mount_paths()
            self.assertEqual(str(auth), '/run/desktop/mnt/host/e/factory/data/auth')
            self.assertEqual(str(ws), '/run/desktop/mnt/host/e/factory/data/workspaces')
        finally:
            manager.engine = saved_engine
            manager.socket.gethostname = saved_hostname

    def test_repository_and_branch_restrictions(self):
        self.assertEqual(manager.check_repository('my-org/my_repo'), 'my-org/my_repo')
        for bad in ['https://evil.com/r', '../x', 'x/../z', 'x/a b', '/private', 'x/x/extra']:
            with self.subTest(bad=bad), self.assertRaises(manager.ApiError):
                manager.check_repository(bad)
        self.assertEqual(manager.check_ref('feature/login'), 'feature/login')
        for bad in ['../../foo', 'x//y', '--upload-pack=evil', 'a.lock', 'x@{1}']:
            with self.subTest(bad=bad), self.assertRaises(manager.ApiError):
                manager.check_ref(bad)

    def test_auth_profiles_validate_chatgpt_not_api_keys(self):
        previous = manager.AUTH_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            manager.AUTH_ROOT = Path(tmp)
            folder = Path(tmp) / 'codex' / 'default'
            folder.mkdir(parents=True)
            try:
                self.assertFalse(manager.auth_ready('codex', 'default'))
                (folder / 'auth.json').write_text(json.dumps({
                    'auth_mode': 'api_key', 'OPENAI_API_KEY': 'should-not-be-used'
                }))
                self.assertFalse(manager.auth_ready('codex', 'default'))
                (folder / 'auth.json').write_text(json.dumps({
                    'auth_mode': 'chatgpt', 'tokens': {'access_token': 'mock-session-token'}
                }))
                self.assertTrue(manager.auth_ready('codex', 'default'))
                self.assertFalse(manager.auth_ready('codex', '../secrets'))
                self.assertFalse(manager.auth_ready('codex', 'developer'))
            finally:
                manager.AUTH_ROOT = previous

    def test_ai_job_uses_dedicated_login_bind_mount_never_openai_key(self):
        saved_engine, saved_auth = manager.engine, manager.AUTH_ROOT
        fake = FakeEngine()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'codex' / 'default'
            folder.mkdir(parents=True)
            (folder / 'auth.json').write_text(json.dumps({
                'auth_mode': 'chatgpt', 'tokens': {'access_token': 'mock-session-token'}
            }))
            manager.AUTH_ROOT = Path(tmp)
            manager.engine = fake
            try:
                job = manager.create_job({'role': 'developer', 'task': 'make a test'})
                data = fake.containers[manager.job_name(job['job_id'])]['Config']
                self.assertEqual(data['HostConfig']['Mounts'], [
                    {'Type': 'bind', 'Source': str(manager.HOST_WORKSPACE_ROOT / job['job_id']),
                     'Target': '/workspace', 'ReadOnly': False},
                    {'Type': 'bind', 'Source': str(manager.HOST_AUTH_ROOT / 'codex' / 'default'),
                     'Target': '/auth', 'ReadOnly': False},
                ])
                self.assertTrue((manager.WORKSPACE_ROOT / job['job_id']).is_dir())
                self.assertEqual(data['HostConfig']['NetworkMode'], 'factory_egress')
                self.assertIn('CODEX_HOME=/auth', data['Env'])
                self.assertFalse(any('OPENAI_API_KEY' in s for s in data['Env']))
                self.assertEqual(data['Labels']['factory.codex_profile'], 'default')
                self.assertTrue(data['HostConfig']['ReadonlyRootfs'])
                self.assertNotIn('Binds', data['HostConfig'])
                # A running task using the same login must block concurrent login writes.
                fake.containers[manager.job_name(job['job_id'])]['State'] = {
                    'Status': 'running', 'ExitCode': 0, 'Running': True}
                with self.assertRaises(manager.ApiError) as ctx:
                    manager.create_job({'role': 'requirements', 'task': 'another request'})
                self.assertEqual(ctx.exception.code, 429)
            finally:
                manager.engine = saved_engine
                manager.AUTH_ROOT = saved_auth

    def test_structured_result_is_opt_in_and_sent_to_new_worker(self):
        original=manager.engine
        fake=FakeEngine()
        manager.engine=fake
        try:
            job=manager.create_job({'role':'smoke','task':'Smoke','structured_result':True})
            env=fake.containers[manager.job_name(job['job_id'])]['Config']['Env']
            self.assertIn('FACTORY_AGENT_STRUCTURED_RESULT=true',env)
            with self.assertRaises(manager.ApiError) as err:
                manager.create_job({'role':'smoke','structured_result':'true'})
            self.assertEqual(err.exception.code,400)
            job2=manager.create_job({'role':'smoke','task':'Smoke'})
            env2=fake.containers[manager.job_name(job2['job_id'])]['Config']['Env']
            self.assertIn('FACTORY_AGENT_STRUCTURED_RESULT=false',env2)
        finally:
            manager.engine=original

    def test_missing_login_is_409(self):
        saved = manager.AUTH_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            manager.AUTH_ROOT = Path(tmp)
            try:
                with self.assertRaises(manager.ApiError) as error:
                    manager.create_job({'role': 'reviewer', 'task': 'review code'})
                self.assertEqual(error.exception.code, 409)
                self.assertIn('auth-login', str(error.exception))
            finally:
                manager.AUTH_ROOT = saved

    def test_claude_gemini_oauth_separation_and_images(self):
        saved_engine, saved_auth = manager.engine, manager.AUTH_ROOT
        fake = FakeEngine()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_dir = root / 'claude' / 'default'
            claude_dir.mkdir(parents=True)
            (claude_dir / '.factory-login-ready').touch()
            gemini_dir = root / 'gemini' / 'default' / '.gemini'
            gemini_dir.mkdir(parents=True)
            (gemini_dir / 'oauth_creds.json').write_text('{"refresh_token":"mock"}')
            manager.AUTH_ROOT = root
            manager.engine = fake
            try:
                for provider in ('claude', 'gemini'):
                    self.assertTrue(manager.auth_ready(provider, 'default'))
                    job = manager.create_job({'provider': provider, 'role': 'developer', 'task': 'test'})
                    data = fake.containers[manager.job_name(job['job_id'])]['Config']
                    self.assertEqual(data['Image'], manager.IMAGE_PREFIX + '-' + provider + ':' + manager.IMAGE_TAG)
                    self.assertEqual(data['HostConfig']['Mounts'][1]['Source'],
                                     str(manager.HOST_AUTH_ROOT / provider / 'default'))
                    self.assertEqual(data['Labels']['factory.provider'], provider)
                    self.assertEqual(data['Env'][1], 'FACTORY_AGENT_PROVIDER=' + provider)
                    self.assertFalse(any('ANTHROPIC_API_KEY' in e or 'GEMINI_API_KEY' in e for e in data['Env']))
                    self.assertTrue(data['HostConfig']['ReadonlyRootfs'])
            finally:
                manager.engine, manager.AUTH_ROOT = saved_engine, saved_auth

    def test_same_profile_lock_per_provider_not_cross_provider(self):
        saved_engine, saved_auth = manager.engine, manager.AUTH_ROOT
        fake = FakeEngine()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_dir = root / 'codex' / 'default'
            codex_dir.mkdir(parents=True)
            (codex_dir / 'auth.json').write_text(json.dumps({'auth_mode':'chatgpt','tokens':{'access_token':'mock'}}))
            claude_dir = root / 'claude' / 'default'
            claude_dir.mkdir(parents=True)
            (claude_dir / '.factory-login-ready').touch()
            manager.AUTH_ROOT, manager.engine = root, fake
            try:
                a = manager.create_job({'provider': 'codex', 'role': 'developer', 'task': 'A'})
                fake.containers[manager.job_name(a['job_id'])]['State']['Status'] = 'running'
                b = manager.create_job({'provider': 'claude', 'role': 'developer', 'task': 'B'})
                fake.containers[manager.job_name(b['job_id'])]['State']['Status'] = 'running'
                self.assertNotEqual(a['job_id'], b['job_id'])
                with self.assertRaises(manager.ApiError) as raised:
                    manager.create_job({'provider': 'claude', 'role': 'tester', 'task': 'C'})
                self.assertEqual(raised.exception.code, 429)
            finally:
                manager.engine, manager.AUTH_ROOT = saved_engine, saved_auth

    def test_workspace_chmod_precedes_chown_without_cap_fowner(self):
        """Do not chmod a workspace after its ownership changes to UID 1000.

        Manager has CAP_CHOWN but intentionally does not have CAP_FOWNER.
        A chmod after chown would return EPERM on a real Linux host.
        """
        saved_engine = manager.engine
        fake = FakeEngine()
        manager.engine = fake
        operations = []
        original_chmod = manager.Path.chmod

        def record_chmod(path, mode):
            operations.append('chmod')
            return original_chmod(path, mode)

        try:
            with mock.patch.object(manager.os, 'geteuid', return_value=0), \
                 mock.patch.object(manager.os, 'chown', side_effect=lambda *a: operations.append('chown')), \
                 mock.patch.object(manager.Path, 'chmod', autospec=True, side_effect=record_chmod):
                manager.create_job({'role': 'smoke', 'task': 'verify'})
            self.assertEqual(operations, ['chmod', 'chown'])
        finally:
            manager.engine = saved_engine

    def test_unwritable_workspace_is_500_not_docker_503(self):
        saved_engine, saved_root = manager.engine, manager.WORKSPACE_ROOT
        manager.engine = FakeEngine()
        manager.WORKSPACE_ROOT = saved_root / 'missing-parent' / 'workspaces'
        try:
            with self.assertRaises(manager.ApiError) as error:
                manager.create_job({'role': 'smoke', 'task': ''})
            self.assertEqual(error.exception.code, 500)
            self.assertIn('repair-workspaces', error.exception.message)
            self.assertIn('filesystem', error.exception.message.lower())
        finally:
            manager.engine, manager.WORKSPACE_ROOT = saved_engine, saved_root

    def test_docker_connection_errors_have_own_exception(self):
        with mock.patch.object(manager, 'UnixConnection') as mock_connection:
            mock_connection.return_value.request.side_effect = ConnectionRefusedError(
                111, 'Connection refused')
            with self.assertRaises(manager.EngineUnavailable):
                manager.engine('GET', '/_ping')
            self.assertTrue(mock_connection.return_value.close.called)

    def test_invalid_provider_is_400(self):
        with self.assertRaises(manager.ApiError) as error:
            manager.create_job({'provider': 'untrusted', 'role': 'smoke', 'task': 'x'})
        self.assertEqual(error.exception.code, 400)

    def test_no_arbitrary_docker_options(self):
        with self.assertRaises(manager.ApiError) as raised:
            manager.create_job({'role': 'smoke', 'task': '', 'image': 'nginx:latest'})
        self.assertEqual(raised.exception.code, 400)

    def test_api_auth_create_status_cleanup(self):
        saved = manager.engine
        fake = FakeEngine()
        manager.engine = fake
        server = ThreadingHTTPServer(('127.0.0.1', 0), manager.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f'http://127.0.0.1:{server.server_address[1]}'

        def api(method, path, payload=None, authorized=True):
            body = json.dumps(payload).encode() if payload is not None else None
            headers = {'Content-Type': 'application/json'}
            if authorized:
                headers['Authorization'] = 'Bearer ' + manager.TOKEN
            req = Request(address + path, method=method, headers=headers, data=body)
            with urlopen(req, timeout=5) as response:
                return json.load(response)

        try:
            with self.assertRaises(HTTPError) as raised:
                api('GET', '/jobs', authorized=False)
            self.assertEqual(raised.exception.code, 401)
            created = api('POST', '/jobs', {'role': 'smoke', 'task': 'hello'})
            self.assertEqual(created['status'], 'running')
            self.assertEqual(created['provider'], 'codex')
            self.assertEqual(len(fake.containers), 1)
            info = api('GET', '/jobs/' + created['job_id'])
            self.assertEqual(info['status'], 'succeeded')
            self.assertEqual(info['exit_code'], 0)
            self.assertEqual(info['result']['summary'], 'smoke')
            self.assertTrue(fake.containers[next(iter(fake.containers))]['Config']['HostConfig']['ReadonlyRootfs'])
            self.assertNotIn('Binds', fake.containers[next(iter(fake.containers))]['Config']['HostConfig'])
            result = api('DELETE', '/jobs/' + created['job_id'])
            self.assertTrue(result['removed'])
            self.assertEqual(len(fake.containers), 0)
        finally:
            server.shutdown()
            server.server_close()
            manager.engine = saved


if __name__ == '__main__':
    unittest.main()
