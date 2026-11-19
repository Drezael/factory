import base64
import importlib.util
import json
import pathlib
import unittest
from unittest.mock import patch

APP_FILE = pathlib.Path(__file__).parents[1]/'github-registry'/'app.py'
spec = importlib.util.spec_from_file_location('factory_github_registry', APP_FILE)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

EXAMPLE = '''
version: 1
repository: acme/backend
enabled: true
issues:
  state: open
  required_labels: [factory:ready]
  ignored_labels: [factory:hold]
  initial_sync: existing
agent:
  provider: claude
  role: requirements
  auto_execute: false
'''


def issue(number=3, labels=('factory:ready',), updated='2026-11-18T18:00:00Z', pull=False, state='open'):
    return {'number': number,'updated_at': updated,'created_at': '2026-11-18T16:00:00Z',
            'title': 'Fix issue','body':'Please fix it','state':state,
            'labels':[{'name':x} for x in labels],
            'html_url':f'https://github.com/acme/backend/issues/{number}',
            **({'pull_request':{'url':'https://example.org/secret'}} if pull else {})}


class ConfigTests(unittest.TestCase):
    def test_example_valid(self):
        c=app.parse_config(EXAMPLE,'repos/backend.yaml')
        self.assertEqual(c['repository'],'acme/backend')
        self.assertEqual(c['agent']['provider'],'claude')
        self.assertEqual(c['issues']['required_labels'],['factory:ready'])

    def test_all_sample_files(self):
        for p in (APP_FILE.parents[1]/'registry-example'/'repos').glob('*.yaml'):
            self.assertTrue(app.parse_config(p.read_text(), str(p))['enabled'])

    def test_reject_unsafe_yaml(self):
        with self.assertRaises(app.RegistryError):
            app.parse_config('!!python/object/apply:os.system [echo pwned]','repos/attack.yaml')

    def test_reject_empty_label_allowlist(self):
        with self.assertRaises(app.RegistryError):
            app.parse_config(EXAMPLE.replace('[factory:ready]', '[]'),'repos/b.yaml')

    def test_reject_shellish_slug(self):
        with self.assertRaises(app.RegistryError):
            app.parse_config(EXAMPLE.replace('acme/backend','acme/../backend'),'repos/b.yaml')

    def test_reject_execution(self):
        with self.assertRaises(app.RegistryError):
            app.parse_config(EXAMPLE.replace('auto_execute: false','auto_execute: true'),'repos/b.yaml')

    def test_reject_extra_fields(self):
        with self.assertRaises(app.RegistryError):
            app.parse_config(EXAMPLE+'shell: curl evil\n','repos/b.yaml')

    def test_disabled_is_boolean(self):
        with self.assertRaises(app.RegistryError):
            app.parse_config(EXAMPLE.replace('enabled: true','enabled: yesplease'),'repos/b.yaml')


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.conf=app.parse_config(EXAMPLE,'repos/backend.yaml')

    def test_required_label(self):
        self.assertFalse(app.eligible(issue(labels=['bug']),self.conf))
        self.assertTrue(app.eligible(issue(),self.conf))

    def test_hold_label(self):
        self.assertFalse(app.eligible(issue(labels=['factory:ready','factory:hold']),self.conf))

    def test_no_pull_request(self):
        self.assertFalse(app.eligible(issue(pull=True),self.conf))

    def test_no_closed_when_open(self):
        self.assertFalse(app.eligible(issue(state='closed'),self.conf))

    def test_event_dedupe_revisions(self):
        same=issue()
        event1=app.make_event(same,self.conf)
        event2=app.make_event(same,self.conf)
        event3=app.make_event(issue(updated='2026-11-18T18:00:02Z'),self.conf)
        self.assertEqual(event1['event_key'],event2['event_key'])
        self.assertNotEqual(event1['event_key'],event3['event_key'])

    def test_body_truncation(self):
        long=issue();long['body']='A'*100_000
        self.assertEqual(len(app.make_event(long,self.conf)['body']),16000)


class GitHubTests(unittest.TestCase):
    def test_github_api_no_http(self):
        with self.assertRaises(app.RegistryError):
            app.GitHubClient('token','http://api.github.com')

    def test_url_must_stay_same_origin(self):
        client=app.GitHubClient('token')
        with self.assertRaises(app.RegistryError):
            client.get('https://evil.example/next')

    def test_registry_directory_url_loading(self):
        client=app.GitHubClient('token')
        contents={'encoding':'base64','content':base64.b64encode(b'https://github.com/acme/backend\n').decode()}
        metadata={'full_name':'acme/backend','owner':{'login':'acme','type':'Organization'},
                  'default_branch':'main','has_issues':True}
        listing=[{'name':'backend.repo','type':'file','path':'repos/backend.repo','size':32}]
        with patch.object(client,'get',side_effect=[(listing,''),(contents,''),(metadata,'')]):
            defs=client.registry('acme/private-registry','main','repos')
        self.assertEqual([v['repository'] for v in defs],['acme/backend'])
        self.assertTrue(defs[0]['planning']['enabled'])
        self.assertTrue(defs[0]['intake']['enabled'])

    def test_registry_duplicates_rejected(self):
        client=app.GitHubClient('token')
        data={'encoding':'base64','content':base64.b64encode(b'https://github.com/acme/backend').decode()}
        metadata={'full_name':'acme/backend','owner':{'login':'acme','type':'Organization'},
                  'default_branch':'main','has_issues':True}
        listing=[{'name':'backend.repo','type':'file','path':'repos/backend.repo'},
                 {'name':'backend2.repo','type':'file','path':'repos/backend2.repo'}]
        with patch.object(client,'get',side_effect=[(listing,''),(data,''),(metadata,''),(data,'')]):
            with self.assertRaises(app.RegistryError):
                client.registry('acme/private-registry','main','repos')

    def test_pagination_pages(self):
        client=app.GitHubClient('token')
        with patch.object(client,'get',side_effect=[([{'id':1}],'<https://api.github.com/repos/a/b/issues?page=2>; rel="next"'),([{'id':2}],'')]):
            result=list(client.pages('repos/a/b/issues',query={'per_page':100}))
        self.assertEqual([v['id'] for v in result],[1,2])

    def test_pagination_reject_off_origin(self):
        client=app.GitHubClient('token')
        with patch.object(client,'get',side_effect=[([{'id':1}],'<https://evil.example/next>; rel="next"'),app.RegistryError('external')]) as get:
            with self.assertRaises(app.RegistryError):
                list(client.pages('repos/a/b/issues'))
            self.assertEqual(get.call_count,2)


if __name__=='__main__':
    unittest.main()

class ScanBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.conf=app.parse_config(EXAMPLE,'repos/backend.yaml')
        self.gh=app.GitHubClient('test-token')

    @patch.object(app, 'reconcile_monitored_repositories', new=lambda configs: None)
    def test_new_only_skips_first_backlog(self):
        conf=app.parse_config(EXAMPLE.replace('initial_sync: existing','initial_sync: new_only'),'repos/backend.yaml')
        with patch.dict(app.os.environ,{'FACTORY_GITHUB_REGISTRY_TOKEN':'secret',
                                        'FACTORY_GITHUB_REGISTRY_REPO':'acme/private-registry'}), \
             patch.object(app.GitHubClient,'registry',return_value=[conf]), \
             patch.object(app,'last_state',return_value=None), \
             patch.object(app,'store_repo_scan',return_value=[]) as stored, \
             patch.object(app.GitHubClient,'issues') as issues:
            r=app.scan()
        self.assertEqual(r['new_events_count'],0)
        issues.assert_not_called()
        self.assertEqual(stored.call_args.kwargs,{'skip_initial':True})

    @patch.object(app, 'reconcile_monitored_repositories', new=lambda configs: None)
    def test_updated_issue_after_new_only_baseline(self):
        conf=app.parse_config(EXAMPLE.replace('initial_sync: existing','initial_sync: new_only'),'repos/backend.yaml')
        baseline=app.iso_dt('2026-11-18T18:00:00Z')
        items=[issue(1,updated='2026-11-18T17:59:30Z'),issue(2,updated='2026-11-18T18:00:01Z')]
        with patch.dict(app.os.environ,{'FACTORY_GITHUB_REGISTRY_TOKEN':'secret',
                                        'FACTORY_GITHUB_REGISTRY_REPO':'acme/private-registry'}), \
             patch.object(app.GitHubClient,'registry',return_value=[conf]), \
             patch.object(app,'last_state',return_value=(baseline,conf['config_hash'],baseline)), \
             patch.object(app.GitHubClient,'issues',return_value=iter(items)), \
             patch.object(app,'store_repo_scan',return_value=[]) as stored:
            app.scan()
        events=stored.call_args.args[1]
        self.assertEqual([e['issue_number'] for e in events],[2])

    @patch.object(app, 'reconcile_monitored_repositories', new=lambda configs: None)
    def test_repo_errors_do_not_block_other_repos(self):
        second=app.parse_config(EXAMPLE.replace('acme/backend','acme/frontend'),'repos/frontend.yaml')
        with patch.dict(app.os.environ,{'FACTORY_GITHUB_REGISTRY_TOKEN':'secret',
                                        'FACTORY_GITHUB_REGISTRY_REPO':'acme/private-registry'}), \
             patch.object(app.GitHubClient,'registry',return_value=[self.conf,second]), \
             patch.object(app,'last_state',return_value=None), \
             patch.object(app.GitHubClient,'issues',side_effect=[app.RegistryError('timeout'),iter([issue(3)])]), \
             patch.object(app,'store_repo_scan',return_value=[{'id':10,'issue_number':3}]):
            r=app.scan()
        self.assertEqual([e['status'] for e in r['repositories']],['error','ok'])
        self.assertEqual(r['new_events_count'],1)

    def test_missing_registry_credentials(self):
        with patch.dict(app.os.environ,{'FACTORY_GITHUB_REGISTRY_TOKEN':'',
                                        'FACTORY_GITHUB_REGISTRY_REPO':''}):
            with self.assertRaises(app.RegistryError):
                app.scan()
