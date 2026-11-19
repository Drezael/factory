import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
manager_spec = importlib.util.spec_from_file_location('quota_manager', ROOT/'manager'/'server.py')
manager = importlib.util.module_from_spec(manager_spec)
manager_spec.loader.exec_module(manager)
probe_spec = importlib.util.spec_from_file_location('quota_probe', ROOT/'worker'/'limits.py')
probe = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(probe)


def fake_container(provider='codex', profile='default', role='limit-check', state='exited', timestamp=9999999999):
    return {'State': state, 'Id': 'abc123', 'Labels': {
        'factory.managed':'true', 'factory.owner': manager.OWNER, 'factory.job_id':'a'*20,
        'factory.role': role, 'factory.provider': provider, 'factory.profile': profile,
        'factory.created_at':str(timestamp)}}


class QuotaTests(unittest.TestCase):
    def test_codex_multi_window_has_percentages_and_reset(self):
        with mock.patch.dict(os.environ, {'FACTORY_LIMIT_CODEX_BUCKETS':'codex'}):
            with mock.patch.object(probe, 'MIN_REMAIN', 5):
                obj=probe.parse_codex_limits({'rateLimits': {'limitId':'codex',
                    'primary': {'usedPercent':40, 'windowDurationMins':300, 'resetsAt':199},
                    'secondary':{'usedPercent':77, 'windowDurationMins':10080, 'resetsAt':299}}})
        self.assertEqual(obj['status'], 'available')
        self.assertEqual(obj['buckets']['codex']['windows'][0]['remaining_percent'], 60)
        self.assertEqual(obj['buckets']['codex']['windows'][1]['resets_at'], 299)

    def test_codex_blocks_if_either_window_too_close_to_full(self):
        with mock.patch.object(probe, 'MIN_REMAIN', 5):
            obj=probe.parse_codex_limits({'rateLimits': {'limitId':'codex',
                'primary': {'usedPercent':2}, 'secondary':{'usedPercent':99}}})
        self.assertEqual(obj['status'], 'exhausted')

    def test_codex_unknown_if_bucket_or_data_missing(self):
        self.assertEqual(probe.parse_codex_limits({})['status'], 'unknown')
        self.assertEqual(probe.parse_codex_limits({'rateLimits': {'limitId':'codex'}})['status'], 'unknown')

    def test_codex_rate_limit_flag_blocks_even_if_percent_low(self):
        with mock.patch.object(probe, 'MIN_REMAIN', 5):
            obj=probe.parse_codex_limits({'rateLimits': {'limitId':'codex',
                'rateLimitReachedType':'primary', 'primary': {'usedPercent':22}}})
        self.assertEqual(obj['status'], 'exhausted')

    def test_old_smoke_still_allowed_even_with_monitoring(self):
        with mock.patch.object(manager, 'LIMIT_ENABLED', True), \
             mock.patch.object(manager, 'engine') as engine, \
             mock.patch.object(manager, 'WORKSPACE_ROOT') as path, \
             mock.patch.object(manager, 'docker_list', return_value=[]):
            engine.side_effect=[{'Id':'cid'},{}]
            path.__truediv__.return_value.mkdir.return_value=None
            path.__truediv__.return_value.chmod.return_value=None
            data=manager.create_job({'role':'smoke','provider':'codex'})
            self.assertEqual(data['role'], 'smoke')

    def test_quota_gate_fails_closed_unknown(self):
        with mock.patch.object(manager, 'LIMIT_ENABLED', True), \
             mock.patch.object(manager, 'LIMIT_UNKNOWN_POLICY', 'block'), \
             mock.patch.object(manager, 'auth_ready', return_value=True), \
             mock.patch.object(manager, 'docker_list', return_value=[]):
            d=manager.limit_availability('codex','developer')
            self.assertFalse(d['available'])
            self.assertEqual(d['status'], 'unknown')

    def test_quota_gate_allows_fresh_available(self):
        with mock.patch.object(manager, 'LIMIT_ENABLED', True), \
             mock.patch.object(manager, 'auth_ready', return_value=True), \
             mock.patch.object(manager, 'docker_list', return_value=[fake_container()]), \
             mock.patch.object(manager, '_probe_result', return_value={'status':'available'}), \
             mock.patch.object(manager.time, 'time', return_value=10000000000):
            d=manager.limit_availability('codex','developer')
            self.assertTrue(d['available']) # 1-second-old cache is fresh
        # Test with dynamically recent timestamp.
        with mock.patch.object(manager, 'LIMIT_ENABLED', True), \
             mock.patch.object(manager, 'auth_ready', return_value=True), \
             mock.patch.object(manager, 'docker_list', return_value=[fake_container(timestamp=10000000000)]), \
             mock.patch.object(manager, '_probe_result', return_value={'status':'available'}), \
             mock.patch.object(manager.time, 'time', return_value=10000000000):
            self.assertTrue(manager.limit_availability('codex','developer')['available'])

    def test_stale_available_is_blocked_even_unknown_policy_allow(self):
        with mock.patch.object(manager, 'LIMIT_ENABLED', True), \
             mock.patch.object(manager, 'LIMIT_UNKNOWN_POLICY', 'allow'), \
             mock.patch.object(manager, 'auth_ready', return_value=True), \
             mock.patch.object(manager, 'docker_list', return_value=[fake_container(timestamp=1)]), \
             mock.patch.object(manager, '_probe_result', return_value={'status':'available'}):
            self.assertFalse(manager.limit_availability('codex','developer')['available'])

    def test_probe_container_uses_no_prompt_or_workspace(self):
        saved = manager.CREATE_LOCK
        created=[]
        def engine(method,path,payload=None,**kwargs):
            if method == 'POST' and path.startswith('/containers/create'):
                created.append(payload)
                return {'Id':'abc999'}
            return {}
        with mock.patch.object(manager, 'engine', side_effect=engine), \
             mock.patch.object(manager, 'auth_ready', return_value=True), \
             mock.patch.object(manager, 'docker_list', return_value=[]):
            result=manager._start_limit_probe('codex','default')
        self.assertEqual(result['status'], 'checking')
        config=created[0]
        self.assertEqual(config['Entrypoint'], ['python3','/opt/factory/limits.py'])
        self.assertEqual(len(config['HostConfig']['Mounts']), 1)
        self.assertNotIn('FACTORY_AGENT_TASK_B64', str(config['Env']))
        self.assertNotIn('FACTORY_GITHUB_TOKEN', str(config['Env']))
        self.assertEqual(config['HostConfig']['NetworkMode'], manager.NETWORK_MODE)

    def test_parallel_running_agent_prevents_same_profile_probe(self):
        running=fake_container(role='developer',state='running')
        with mock.patch.object(manager, 'docker_list', return_value=[running]), \
             mock.patch.object(manager, 'auth_ready', return_value=True):
            with self.assertRaises(manager.ApiError) as cm:
                manager._start_limit_probe('codex','default')
        self.assertEqual(cm.exception.code,429)

    def test_missing_login_is_blocked_even_policy_allow(self):
        with mock.patch.object(manager, 'LIMIT_ENABLED', True), \
             mock.patch.object(manager, 'LIMIT_UNKNOWN_POLICY', 'allow'), \
             mock.patch.object(manager, 'auth_ready', return_value=False), \
             mock.patch.object(manager, 'docker_list', return_value=[]):
            self.assertFalse(manager.limit_availability('codex','developer')['available'])

    def test_manager_roles_are_validated(self):
        with self.assertRaises(manager.ApiError):
            manager.limit_availability('foo','developer')
        with self.assertRaises(manager.ApiError):
            manager.limit_availability('codex','smoke')

if __name__=='__main__':
    unittest.main()
