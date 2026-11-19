"""Regression tests for URL-only GitHub registry, v5.16.

No network access or real GitHub token is used by these tests.
"""
import base64
import importlib.util
import pathlib
import unittest
from unittest.mock import Mock, patch

HERE = pathlib.Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('factory_allowlist_app', HERE/'github-registry'/'app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
import bootstrap
import registry_files


class AllowlistTests(unittest.TestCase):
    def test_single_url_and_git_suffix(self):
        self.assertEqual(registry_files.parse_url('https://github.com/team/app\n', 'repos/a.repo'), 'team/app')
        self.assertEqual(registry_files.parse_url('https://github.com/team/app.git', 'repos/a.repo'), 'team/app')

    def test_rejects_non_github_hosts_credentials_and_multi_lines(self):
        for value in ('', 'team/app', 'http://github.com/team/app',
                      'https://github.com.attacker.invalid/team/app',
                      'https://evil.invalid/team/app',
                      'https://hacker@github.com/team/app',
                      'https://github.com/team/app?token=something',
                      'https://github.com/team/app/issues/1',
                      'https://github.com/team/app\nhttps://github.com/team/other',
                      'https://github.com/../evil',
                      'https://github.com/team/%2e%2e'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                registry_files.parse_url(value, 'repos/bad.repo')

    def test_reads_exact_explicit_repo_file_only(self):
        gh = Mock()
        entries = [{'type':'file','name':'backend.repo','path':'repos/backend.repo','size':33},
                   {'type':'file','name':'legacy.yaml','path':'repos/legacy.yaml','size':500},
                   {'type':'file','name':'.gitkeep','path':'repos/.gitkeep','size':0}]
        content = {'encoding':'base64','content':base64.b64encode(b'https://github.com/team/backend\n').decode('ascii')}
        metadata = {'full_name':'team/backend','owner':{'login':'team','type':'Organization'},
                    'default_branch':'release','has_issues':True}
        gh.get.side_effect = [(entries,''), (content,''), (metadata,'')]
        configs = registry_files.parse_allowlist(gh,'team/factory-registry','main','repos',
                     parse_config=app.parse_config, validate_slug=app._validate_repo_slug)
        self.assertEqual(len(configs),1)
        self.assertEqual(configs[0]['repository'],'team/backend')
        self.assertEqual(configs[0]['pipeline']['base_branch'],'release')
        self.assertTrue(configs[0]['planning']['enabled'])
        self.assertEqual(configs[0]['registry_path'],'repos/backend.repo')
        self.assertEqual(gh.get.call_count,3)
        gh.pages.assert_not_called()

    def test_delete_removes_repo_without_discovery_or_recreation(self):
        gh = Mock()
        gh.get.return_value = ([{'type':'file','name':'.gitkeep','path':'repos/.gitkeep'}], '')
        configs = registry_files.parse_allowlist(gh,'team/factory-registry','main','repos',
                     parse_config=app.parse_config, validate_slug=app._validate_repo_slug)
        self.assertEqual(configs, [])
        self.assertEqual(gh.get.call_count,1)
        gh.request.assert_not_called()
        gh.pages.assert_not_called()

    def test_duplicate_urls_fail_closed(self):
        gh = Mock()
        entries = [{'name':name,'path':'repos/'+name,'type':'file'} for name in ('a.repo','b.repo')]
        contents = {'encoding':'base64','content':base64.b64encode(b'https://github.com/team/backend').decode()}
        meta = {'full_name':'team/backend','owner':{'login':'team','type':'Organization'},'default_branch':'main'}
        gh.get.side_effect = [(entries,''),(contents,''),(meta,''),(contents,'')]
        with self.assertRaisesRegex(ValueError,'duplicate'):
            registry_files.parse_allowlist(gh,'team/factory-registry','main','repos',
                         parse_config=app.parse_config, validate_slug=app._validate_repo_slug)

    def test_global_defaults_can_be_set_without_yaml_per_repo(self):
        gh = Mock()
        gh.get.side_effect = [
            ([{'name':'api.repo','path':'repos/api.repo','type':'file'}], ''),
            ({'encoding':'base64','content':base64.b64encode(b'https://github.com/team/api').decode()}, ''),
            ({'full_name':'team/api','owner':{'login':'team','type':'User'},
              'default_branch':'develop','has_issues':True}, '')]
        with patch.dict(app.os.environ,{
            'FACTORY_GITHUB_DEFAULT_PLANNER_PROVIDER':'gemini',
            'FACTORY_GITHUB_DEFAULT_REVIEWER_PROVIDER':'codex',
            'FACTORY_GITHUB_DEFAULT_REQUIRED_CHECKS':'factory-tests,lint',
            'FACTORY_GITHUB_DEFAULT_AUTO_MERGE':'true'}):
            configs = registry_files.parse_allowlist(gh,'team/factory-registry','main','repos',
                    parse_config=app.parse_config,validate_slug=app._validate_repo_slug)
        conf=configs[0]
        self.assertEqual(conf['planning']['provider'],'gemini')
        self.assertEqual(conf['pipeline']['reviewer_provider'],'codex')
        self.assertEqual(conf['pipeline']['required_checks'],['factory-tests','lint'])
        self.assertTrue(conf['pipeline']['auto_merge'])
        self.assertEqual(conf['kanban']['owner_type'],'user')

    def test_auto_merge_must_have_checks(self):
        metadata={'full_name':'team/api','owner':{'login':'team','type':'User'},'default_branch':'main'}
        with patch.dict(app.os.environ,{
            'FACTORY_GITHUB_DEFAULT_REQUIRED_CHECKS':'',
            'FACTORY_GITHUB_DEFAULT_AUTO_MERGE':'true'}):
            with self.assertRaisesRegex(app.RegistryError,'required_checks'):
                app.parse_config(bootstrap.config_for(metadata,'user'),'repos/api.repo')

    def test_reject_directory_symlink(self):
        gh = Mock()
        gh.get.return_value = ([{'name':'app.repo','path':'repos/app.repo','type':'symlink'}], '')
        with self.assertRaisesRegex(ValueError,'regular file'):
            registry_files.parse_allowlist(gh,'team/factory-registry','main','repos',
                         parse_config=app.parse_config, validate_slug=app._validate_repo_slug)


class FakeGitHub:
    def __init__(self):
        self.files = {}
        self.puts = []
        self.meta={'full_name':'team/factory-registry','private':True,
                   'owner':{'login':'team','type':'Organization'},'default_branch':'main'}
    def get(self,path,query=None):
        if path == 'repos/team/factory-registry':
            return self.meta,''
        prefix = 'repos/team/factory-registry/contents/'
        self_path = path[len(prefix):] if path.startswith(prefix) else None
        if self_path == 'repos':
            entries=[{'name':p.rsplit('/',1)[-1], 'type':'file','path':p}
                     for p in self.files if p.startswith('repos/')]
            if entries: return entries,''
        if self_path in self.files: return {'type':'file'},''
        raise app.GitHubStatusError('not found',404)
    def request(self,method,path,payload):
        assert method=='PUT'
        suffix=path.split('/contents/',1)[1]
        self.files[suffix]=base64.b64decode(payload['content']).decode('utf-8')
        self.puts.append((path,payload))
        return {'content':{'path':suffix}}


class BootstrapTests(unittest.TestCase):
    def test_empty_repo_initializes_skeleton_only_once(self):
        gh = FakeGitHub()
        first = bootstrap.bootstrap(gh,'team/factory-registry','auto','repos',validate_slug=app._validate_repo_slug)
        self.assertTrue(first['readme_created'])
        self.assertTrue(first['placeholder_created'])
        self.assertFalse(first['automatic_discovery'])
        self.assertEqual(set(gh.files),{'README.md','repos/.gitkeep'})
        self.assertEqual(len(gh.puts),2)
        second = bootstrap.bootstrap(gh,'team/factory-registry','auto','repos',validate_slug=app._validate_repo_slug)
        self.assertFalse(second['readme_created'])
        self.assertFalse(second['placeholder_created'])
        self.assertEqual(len(gh.puts),2)

    def test_public_registry_rejected(self):
        gh=FakeGitHub();gh.meta['private']=False
        with self.assertRaisesRegex(ValueError,'PRIVATE'):
            bootstrap.bootstrap(gh,'team/factory-registry','auto','repos',validate_slug=app._validate_repo_slug)
        self.assertEqual(gh.puts,[])


class FakeDB:
    def __init__(self):
        self.commands=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self):return self
    def execute(self,sql,args=()):self.commands.append((sql,args))


class RemovalTests(unittest.TestCase):
    def test_reconcile_deactivates_state_and_pending_events(self):
        db=FakeDB()
        configs=[{'repository':'team/app','enabled':True,'issues':{'enabled':True}}]
        with patch.object(app,'database',return_value=db):
            app.reconcile_monitored_repositories(configs)
        self.assertEqual(len(db.commands),2)
        self.assertIn('monitored=FALSE',db.commands[0][0])
        self.assertIn("status='acknowledged'",db.commands[1][0])
        self.assertEqual(db.commands[0][1],(['team/app'],))
        self.assertEqual(db.commands[1][1],(['team/app'],))
        db.commands=[]
        with patch.object(app,'database',return_value=db):
            app.reconcile_monitored_repositories([])
        self.assertEqual(db.commands[0][1],([],))

    def test_claimed_run_rechecks_allowlist_before_worker_spawn(self):
        db=FakeDB()
        run={'state':'claimed','stage':'developer','repository':'team/app',
             'issue_number':1, 'job_id':None}
        with patch.object(app.COORD,'read_run',return_value=run), \
             patch.object(app.COORD,'_configs',return_value=(Mock(),{})), \
             patch.object(app.COORD.app,'database',return_value=db), \
             patch('lifecycle._manager',side_effect=AssertionError('worker spawned')):
            with self.assertRaisesRegex(app.RegistryError,'removed'):
                app.COORD.start('a'*24)
        self.assertEqual(len(db.commands),1)
        self.assertIn("status='failed'",db.commands[0][0])

    def test_no_legacy_dispatch_fallback(self):
        with patch.dict(app.os.environ,{'FACTORY_GITHUB_AUTORUN_ENABLED':'true'}):
            with patch.object(app.COORD,'_configs',return_value=(Mock(),{})), \
                 patch.object(app.COORD.app,'database',return_value=FakeDB()), \
                 patch.object(app.lifecycle.Coordinator,'claim',side_effect=AssertionError('legacy dispatch called')):
                # Cursor must return rows list from fetchall().
                FakeDB.fetchall=lambda self: []
                result=app.COORD.claim()
                self.assertFalse(result['claimed'])

if __name__=='__main__':
    unittest.main()
