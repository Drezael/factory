"""Regression tests for the full autonomous GitHub feature/bug lifecycle.
No GitHub network calls, subscription sessions or Docker daemon required.
"""
import importlib.util
import pathlib
import unittest
from unittest.mock import Mock,patch
from types import SimpleNamespace

ROOT=pathlib.Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('factory_pipeline_test_app',ROOT/'github-registry'/'app.py')
app=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(app)
import pipeline

EXAMPLE='''
version: 1
repository: acme/backend
issues:
  enabled: true
  state: open
  initial_sync: existing
  required_labels: []
  ignored_labels: [factory:review-defect]
agent:
  provider: codex
  role: developer
  auto_execute: true
  timeout_seconds: 900
kanban:
  enabled: true
  owner_type: organization
  owner: acme
  project_number: 12
  status_field: Status
  columns:
    backlog: Backlog
    development_intake: Entwicklung-Eingang
    bug_intake: Bug-Eingang
    feature_intake: Feature-Eingang
    planning: Planung
    ready: Bereit
    in_progress: In Arbeit
    waiting_for_answer: Rueckfrage
    review: Review
    done: Erledigt
    failed: Fehler
intake:
  enabled: true
  auto_install_forms: true
planning:
  enabled: true
  provider: codex
  max_tasks: 40
pipeline:
  enabled: true
  base_branch: main
  developer_provider: codex
  reviewer_provider: claude
  max_review_cycles: 3
  required_checks: [factory-tests]
  auto_merge: true
'''
A='a'*40
B='b'*40
C='c'*40
D='d'*40

class FakeGitHub:
    def __init__(self,existing=None):
        self.calls=[];self.existing=existing
    def get(self,path,query=None):
        self.calls.append(('GET',path,query))
        if '/git/ref/heads/' in path:
            return {'object':{'sha':A}},''
        if '/git/commits/' in path:
            return {'tree':{'sha':B}},''
        if '/git/trees/' in path:
            return {'truncated':False,'tree':[{'type':'blob','path':'readme.txt','mode':'100644','sha':pipeline._git_sha(b'previous')} ]},''
        if path.endswith('/pulls'):
            return (self.existing or []),''
        if '/commits/' in path and path.endswith('/check-runs'):
            return {'check_runs':[{'name':'factory-tests','status':'completed','conclusion':'success'}]},''
        if '/commits/' in path and path.endswith('/status'):
            return {'statuses':[{'context':'code-factory/agent-review','state':'success'}]},''
        raise AssertionError(path)
    def request(self,method,path,data=None):
        self.calls.append((method,path,data))
        if path.endswith('/git/blobs'):return {'sha':B}
        if path.endswith('/git/trees'):return {'sha':C}
        if path.endswith('/git/commits'):return {'sha':D}
        if path.endswith('/git/refs') or '/git/refs/heads/' in path:return {'ref':'refs/heads/factory/issue-42'}
        if path.endswith('/pulls'):return {'number':99}
        return {}

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.conf=app.parse_config(EXAMPLE,'repos/backend.yaml')

    def test_pipeline_two_providers_auto_merge_and_required_checks(self):
        self.assertTrue(self.conf['pipeline']['enabled'])
        self.assertEqual(self.conf['pipeline']['reviewer_provider'],'claude')
        self.assertTrue(self.conf['pipeline']['auto_merge'])
        self.assertTrue(self.conf['agent']['auto_execute'])

    def test_auto_merge_fails_closed_without_ci(self):
        invalid=EXAMPLE.replace('required_checks: [factory-tests]','required_checks: []')
        with self.assertRaises(app.RegistryError):app.parse_config(invalid,'repos/backend.yaml')

    def test_invalid_branch_and_cycles_rejected(self):
        for change in ('  base_branch: ../main','  max_review_cycles: 999'):
            lines=EXAMPLE.replace('  base_branch: main' if 'base_branch' in change else '  max_review_cycles: 3',change)
            with self.assertRaises(app.RegistryError):app.parse_config(lines,'repos/backend.yaml')

    def test_protected_branch_files_cannot_be_published(self):
        gh=FakeGitHub()
        with patch.object(pipeline,'safe_files',return_value={'.github/workflows/ci.yml':b'evil'}):
            with self.assertRaisesRegex(ValueError,'Protected configuration'):
                pipeline.publish_changes(gh,'acme/backend',42,'f'*20,self.conf['pipeline'])

    def test_publisher_pushes_branch_and_creates_pr_without_worker_write_token(self):
        gh=FakeGitHub()
        with patch.object(pipeline,'safe_files',return_value={'readme.txt':b'fixed issue', 'src/new.py':b'print(42)\n'}):
            pr=pipeline.publish_changes(gh,'acme/backend',42,'f'*20,self.conf['pipeline'])
        self.assertEqual(pr['number'],99)
        self.assertEqual(pr['head_sha'],D)
        self.assertEqual(pr['branch'],'factory/issue-42')
        self.assertTrue(any(x[0]=='POST' and x[1].endswith('/git/refs') for x in gh.calls))
        self.assertTrue(any(x[0]=='POST' and x[1].endswith('/pulls') for x in gh.calls))
        self.assertFalse(any('token' in str(x).lower() for x in gh.calls))

    def test_ci_status_exact_sha_and_pending_or_failed(self):
        gh=FakeGitHub()
        state,checks=pipeline.checks_state(gh,'acme/backend',D,['factory-tests'])
        self.assertEqual(state,'success')
        self.assertEqual(checks['factory-tests'],'success')
        state,_=pipeline.checks_state(gh,'acme/backend',D,['factory-tests','lint'])
        self.assertEqual(state,'pending')
        with patch.object(pipeline,'read',side_effect=[
            {'check_runs':[{'name':'factory-tests','status':'completed','conclusion':'failure'}]},
            {'statuses':[]}]):
            state,_=pipeline.checks_state(gh,'acme/backend',D,['factory-tests'])
            self.assertEqual(state,'failure')

    def test_review_defect_creates_label_then_linked_issue(self):
        gh=FakeGitHub();calls=[]
        def send(method,path,data=None):
            calls.append((method,path,data))
            return {'number':177}
        gh.request=send
        with patch.object(pipeline,'read',side_effect=[Exception('GitHub HTTP 404')]):
            number=pipeline.create_review_defect(gh,'acme/backend',42,99,D,
              [{'title':'Endpoint broken','detail':'POST /api returns 500', 'path':'src/api.py'}],digest='abcd')
        self.assertEqual(number,177)
        self.assertEqual(calls[0][2]['name'],'factory:review-defect')
        self.assertEqual(calls[1][2]['labels'],['factory:review-defect'])
        self.assertIn('Ursprungs-Issue: #42',calls[1][2]['body'])

    def test_ready_and_review_are_claimable_but_rueckfrage_not(self):
        co=pipeline.PipelineCoordinator(app.COORD.app)
        item={'issue':{'number':42,'state':'open','updated_at':'2026-11-18T18:00:00Z'},
              'item_id':'ITEM_42','item_updated_at':'2026-11-18T19:00:00Z','status':'Bereit'}
        board=Mock();board.board=self.conf['kanban'];board.all_cards.return_value=[item]
        gh=Mock();gh.issue.return_value={'labels':[{'name':'factory:bug'}]}
        with patch.object(co,'board',return_value=board),patch.object(co,'_track',return_value=None):
            self.assertEqual(co.collect_events(gh,self.conf)[0]['event_type'],'github.pipeline.plan')
            item['status']='Rueckfrage'
            self.assertEqual(co.collect_events(gh,self.conf),[])
            item['status']='Review'
            self.assertEqual(co.collect_events(gh,self.conf),[])
        with patch.object(co,'board',return_value=board),patch.object(co,'_track',return_value={
            'pr_number':99,'head_sha':D,'reviewed_sha':None,'review_verdict':None}):
            self.assertEqual(co.collect_events(gh,self.conf)[0]['event_type'],'github.pipeline.review')

    def test_child_issues_not_dispatched_as_independent_worker_jobs(self):
        co=pipeline.PipelineCoordinator(app.COORD.app)
        board=Mock();board.board=self.conf['kanban'];board.all_cards.return_value=[{'issue':{'number':102,'state':'open',
            'updated_at':'2026-11-18T18:00:00Z'},'item_id':'ITEM_102',
            'item_updated_at':'2026-11-18T19:00:00Z','status':'Bereit'}]
        gh=Mock();gh.issue.return_value={'labels':[{'name':'factory:review-defect'}]}
        with patch.object(co,'board',return_value=board),patch.object(co,'_track',return_value=None):
            self.assertEqual(co.collect_events(gh,self.conf),[])

    def test_reviewer_requires_fail_findings_and_no_files_changed(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('worker_v511',ROOT/'worker'/'agent.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        okay='{"factory_status":"completed","summary":"looks good","review":"pass","findings":[]}'
        fail='{"factory_status":"completed","summary":"broken","review":"fail","findings":[{"title":"Fails tests","detail":"python -m pytest fails","path":"tests/test_app.py"}]}'
        self.assertEqual(worker.parse_final_message(okay,require_structured=True,role='reviewer')[3],'pass')
        self.assertEqual(worker.parse_final_message(fail,require_structured=True,role='reviewer')[3],'fail')
        invalid='{"factory_status":"completed","summary":"bad","review":"fail","findings":[]}'
        self.assertEqual(worker.parse_final_message(invalid,require_structured=True,role='reviewer')[0],'error')

if __name__=='__main__':unittest.main()

# The merge gate is intentionally tested with no Docker and no GitHub network:
# branch protection and CI outcome must be honored on the same HEAD SHA.
class MergeGateTests(unittest.TestCase):
    def setUp(self):
        self.conf=app.parse_config(EXAMPLE,'repos/backend.yaml')
        self.co=pipeline.PipelineCoordinator(app.COORD.app)
        self.github=Mock()
        self.github.issue.return_value={'number':42,'state':'open'}
        self.github.request.return_value={'merged':True}
        self.board=Mock()
        self.board.all_cards.return_value=[{'issue':{'number':42},'item_id':'ITEM42','status':'Review'}]
        self.row=('acme/backend',42,'factory/issue-42','main',99,D,D,'pass',0)
        self.calls=[]

    def fake_db(self,rows):
        cursor=Mock()
        cursor.fetchall.side_effect=lambda : rows
        cursor.execute.side_effect=lambda sql,*args:self.calls.append(sql)
        connection=Mock()
        connection.__enter__=Mock(return_value=connection)
        connection.__exit__=Mock(return_value=False)
        cursor.__enter__=Mock(return_value=cursor)
        cursor.__exit__=Mock(return_value=False)
        connection.cursor.return_value=cursor
        return Mock(return_value=connection)

    def test_waits_for_ci_or_mergeability(self):
        with patch.dict(pipeline.os.environ,{'FACTORY_GITHUB_AUTOMERGE_ENABLED':'true'}),\
             patch.object(self.co,'_configs',return_value=(self.github,{'acme/backend':self.conf})),\
             patch.object(self.co,'board',return_value=self.board),\
             patch.object(self.co.app,'database',self.fake_db([self.row])),\
             patch.object(pipeline,'read',return_value={'state':'open','head':{'sha':D},
                'base':{'ref':'main'},'mergeable':True,'mergeable_state':'clean','title':'Feature'}),\
             patch.object(pipeline,'checks_state',return_value=('pending',{'factory-tests':'pending'})):
            outcome=self.co.merge_ready()
        self.assertEqual(outcome['merged'],[])
        self.assertEqual(outcome['waiting'][0]['reason'],'CI pending')
        self.github.request.assert_not_called()

    def test_successful_review_and_ci_can_squash_merge(self):
        self.github.issue.return_value={'state':'closed'}
        with patch.dict(pipeline.os.environ,{'FACTORY_GITHUB_AUTOMERGE_ENABLED':'true'}),\
             patch.object(self.co,'_configs',return_value=(self.github,{'acme/backend':self.conf})),\
             patch.object(self.co,'board',return_value=self.board),\
             patch.object(self.co.app,'database',self.fake_db([self.row])),\
             patch.object(pipeline,'read',return_value={'state':'open','head':{'sha':D},
                'base':{'ref':'main'},'mergeable':True,'mergeable_state':'clean','title':'Feature'}),\
             patch.object(pipeline,'checks_state',return_value=('success',{'factory-tests':'success'})),\
             patch.object(self.co,'_post_once'):
            result=self.co.merge_ready()
        self.assertEqual(result['merged'][0]['pull_request'],99)
        self.board.set_status.assert_called_with('ITEM42','done')
        self.assertEqual(self.github.request.call_args.args[0],'PUT')
        self.assertEqual(self.github.request.call_args.args[2]['merge_method'],'squash')

    def test_ci_failed_creates_review_child_and_reverts_development(self):
        tracking={'branch':'factory/issue-42','base_branch':'main','pr_number':99,
                  'head_sha':D,'reviewed_sha':D,'review_verdict':'pass','retry_count':0}
        with patch.dict(pipeline.os.environ,{'FACTORY_GITHUB_AUTOMERGE_ENABLED':'true'}),\
             patch.object(self.co,'_configs',return_value=(self.github,{'acme/backend':self.conf})),\
             patch.object(self.co,'board',return_value=self.board),\
             patch.object(self.co.app,'database',self.fake_db([self.row])),\
             patch.object(pipeline,'read',return_value={'state':'open','head':{'sha':D},
                'base':{'ref':'main'},'mergeable':True,'mergeable_state':'clean','title':'Feature'}),\
             patch.object(pipeline,'checks_state',return_value=('failure',{'factory-tests':'failure'})),\
             patch.object(self.co,'_defect_once',return_value=177) as created,\
             patch.object(pipeline,'publish_status') as status,\
             patch.object(self.co,'_track',return_value=tracking),\
             patch.object(self.co,'_save_track') as saved,\
             patch.object(self.co,'_post_once'):
            result=self.co.merge_ready()
        self.assertEqual(result['merged'],[])
        self.board.set_status.assert_called_with('ITEM42','in_progress')
        self.assertEqual(status.call_args.args[3],'failure')
        self.assertEqual(saved.call_args.args[2]['review_verdict'],'fail')
        created.assert_called_once()
        self.github.request.assert_not_called()
