import importlib.util
import pathlib
import unittest
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('factory_status_app', ROOT/'github-registry'/'app.py')
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)
import lifecycle

YAML = '''
version: 1
repository: acme/backend
issues:
  state: open
  required_labels: []
agent:
  provider: codex
  role: requirements
  auto_execute: false
kanban:
  enabled: true
  owner_type: organization
  owner: acme
  project_number: 3
  status_field: Status
  columns:
    backlog: Backlog
    ready: Bereit
    in_progress: In Arbeit
    waiting_for_answer: Rueckfrage
    review: Review
    done: Erledigt
    failed: Fehler
'''
OPTIONS = ('Backlog','Bereit','In Arbeit','Rueckfrage','Review','Erledigt','Fehler')


def project_response():
    return {'organization':{'projectV2':{'id':'PVT_1','field':{'id':'PVTSSF_1','options':[
        {'id':'option_'+str(i),'name':name} for i,name in enumerate(OPTIONS)]}}}}


def card_response(state='Bereit',updated='2026-11-18T19:00:00Z',body='Implement endpoint'):
    return {'organization':{'projectV2':{'items':{'nodes':[{ 'id':'PVTI_1','updatedAt':updated,
           'fieldValueByName':{'name':state}, 'content':{
               '__typename':'Issue','id':'I_1','number':42,'title':'API Issue','body':body,
               'url':'https://github.com/acme/backend/issues/42','state':'OPEN',
               'updatedAt':'2026-11-18T18:00:00Z','repository':{'nameWithOwner':'acme/backend'}
           }}], 'pageInfo':{'hasNextPage':False,'endCursor':None}}}}}


class FakeGithub:
    def __init__(self,state='Bereit',comments=(),body='Implement endpoint',updated='2026-11-18T19:00:00Z'):
        self.state=state; self.comments=list(comments); self.body=body; self.updated=updated
        self.changes=[];self.identity_count=0
    def graphql(self,query,variables):
        if 'updateProjectV2ItemFieldValue' in query:
            self.changes.append(variables)
            return {'updateProjectV2ItemFieldValue':{'projectV2Item':{'id':variables['item']}}}
        if 'items(first:100' in query:
            return card_response(self.state,self.updated,self.body)
        if 'field(name:$field)' in query:
            return project_response()
        if 'node(id:$id)' in query:
            return {'node':{'id':'PVTI_1','fieldValueByName':{'name':self.state}}}
        raise AssertionError('Unexpected GraphQL query')
    def identity(self):
        self.identity_count += 1
        return 'factory-bot'
    def issue_comments(self,repo,number):
        assert repo=='acme/backend' and number==42
        return iter(self.comments)


def comment(id,author,text,author_type='User'):
    return {'id':id,'user':{'login':author,'type':author_type},'body':text}


class IssueStatusTests(unittest.TestCase):
    def setUp(self):
        self.conf=app.parse_config(YAML,'repos/backend.yaml')
        self.coordinator=lifecycle.Coordinator(app.COORD.app)

    def test_config_rejects_labels_as_status_trigger(self):
        with self.assertRaises(app.RegistryError):
            app.parse_config(YAML.replace('required_labels: []','required_labels: [factory:ready]'),'repos/backend.yaml')

    def test_status_options_required(self):
        with self.assertRaises(app.RegistryError):
            app.parse_config(YAML.replace('    review: Review\n',''),'repos/backend.yaml')

    def test_auto_execute_requires_board(self):
        invalid=YAML.replace('  enabled: true\n  owner_type:', '  enabled: false\n  owner_type:').replace('auto_execute: false','auto_execute: true')
        with self.assertRaises(app.RegistryError):
            app.parse_config(invalid,'repos/backend.yaml')

    def test_board_ready_generates_single_deterministic_event(self):
        gh=FakeGithub()
        a=self.coordinator.collect_events(gh,self.conf)
        b=self.coordinator.collect_events(gh,self.conf)
        self.assertEqual(len(a),1)
        self.assertEqual(a,b)
        self.assertEqual(a[0]['board_item_id'],'PVTI_1')
        self.assertEqual(a[0]['event_type'],'github.issue.ready')
        self.assertFalse(gh.changes)

    def test_statuses_other_than_ready_do_not_dispatch(self):
        for state in ('Backlog','In Arbeit','Review','Erledigt','Fehler'):
            with self.subTest(state=state):
                self.assertEqual([],self.coordinator.collect_events(FakeGithub(state),self.conf))

    @patch.object(app, 'reconcile_monitored_repositories', new=lambda configs: None)
    def test_scan_onboards_open_issue_without_creating_a_new_issue(self):
        gh_issues=[{'number':42, 'node_id':'I_EXISTING', 'state':'open'},
                   {'number':43,'node_id':'PR_1','state':'open','pull_request':{'url':'p'}}]
        fake_board=Mock()
        fake_board.all_cards.return_value=[]
        fake_coordinator=Mock()
        fake_coordinator.board.return_value=fake_board
        fake_coordinator.collect_events.return_value=[]
        with patch.dict(app.os.environ,{'FACTORY_GITHUB_REGISTRY_TOKEN':'test-token',
                                        'FACTORY_GITHUB_REGISTRY_REPO':'acme/private'}), \
             patch.object(app.GitHubClient,'registry',return_value=[self.conf]), \
             patch.object(app.GitHubClient,'issues',return_value=iter(gh_issues)), \
             patch.object(app,'last_state',return_value=None), \
             patch.object(app,'store_repo_scan',return_value=[]), \
             patch.object(app,'COORD',fake_coordinator):
            report=app.scan()
        self.assertEqual(report['repositories'][0]['status'],'ok')
        fake_board.add_issue_to_backlog.assert_called_once_with('I_EXISTING')

    def test_new_issue_added_only_to_backlog_not_created(self):
        gh=FakeGithub('Bereit')
        gh.graphql=Mock(side_effect=[
            project_response(),
            {'addProjectV2ItemById':{'item':{'id':'PVTI_NEW'}}},
            {'node':{'id':'PVTI_NEW','fieldValueByName':None}},
            {'updateProjectV2ItemFieldValue':{'projectV2Item':{'id':'PVTI_NEW'}}},
        ])
        board=lifecycle.ProjectBoard(gh,self.conf)
        self.assertEqual(board.add_issue_to_backlog('I_NEW'),'PVTI_NEW')
        self.assertEqual(gh.graphql.call_count,4)
        self.assertEqual(gh.graphql.call_args.args[1]['option'],'option_0')

    def test_existing_project_status_not_overwritten_on_board_join(self):
        gh=FakeGithub()
        gh.graphql=Mock(side_effect=[
            project_response(),
            {'addProjectV2ItemById':{'item':{'id':'PVTI_NEW'}}},
            {'node':{'id':'PVTI_NEW','fieldValueByName':{'name':'Bereit'}}},
        ])
        board=lifecycle.ProjectBoard(gh,self.conf)
        board.add_issue_to_backlog('I_NEW')
        self.assertEqual(gh.graphql.call_count,3)

    def test_ready_transition_has_new_event_key_even_if_issue_not_edited(self):
        first=self.coordinator.collect_events(FakeGithub(updated='2026-11-18T19:00:00Z'),self.conf)[0]
        second=self.coordinator.collect_events(FakeGithub(updated='2026-11-18T19:01:00Z'),self.conf)[0]
        self.assertNotEqual(first['event_key'],second['event_key'])

    def test_new_only_baseline(self):
        baseline=app.iso_dt('2026-11-18T19:00:30Z')
        self.assertFalse(self.coordinator.collect_events(FakeGithub(),self.conf,first_scan_at=baseline)
                         if self.conf['issues']['initial_sync']=='new_only' else False)
        conf=app.parse_config(YAML.replace('state: open','state: open\n  initial_sync: new_only'),'repos/backend.yaml')
        self.assertEqual(self.coordinator.collect_events(FakeGithub(),conf,first_scan_at=baseline),[])
        self.assertEqual(len(self.coordinator.collect_events(FakeGithub(updated='2026-11-18T19:02:00Z'),conf,first_scan_at=baseline)),1)

    def test_wait_for_reply_ignores_agent_comment_and_never_spawns_on_wait(self):
        issue={'title':'API Issue','body':'Implement endpoint'}
        marker=f'<!-- factory:question run={"a"*24} fp={lifecycle.fingerprint(issue)} -->'
        comments=[comment(20,'factory-bot','Please clarify\n'+marker),
                  comment(21,'factory-bot','Self reply')]
        gh=FakeGithub('Rueckfrage',comments)
        self.assertEqual(self.coordinator.collect_events(gh,self.conf),[])
        self.assertFalse(gh.changes)

    def test_human_comment_never_automatically_moves_rueckfrage_to_ready(self):
        marker=f'<!-- factory:question run={"a"*24} fp={"0"*64} -->'
        gh=FakeGithub('Rueckfrage',[comment(20,'factory-bot','Question\n'+marker),
                                    comment(21,'alice','Use Python 3.12')])
        self.assertEqual(self.coordinator.collect_events(gh,self.conf),[])
        self.assertEqual(gh.changes,[])
        self.assertEqual(gh.identity_count,0)

    def test_issue_body_edit_never_automatically_moves_rueckfrage_to_ready(self):
        gh=FakeGithub('Rueckfrage',[comment(21,'alice','Use Python 3.12')],body='New issue requirement')
        self.assertEqual(self.coordinator.collect_events(gh,self.conf),[])
        self.assertEqual(gh.changes,[])

    def test_only_manual_ready_board_transition_dispatches_new_round(self):
        gh=FakeGithub('Rueckfrage',[comment(20,'factory-bot','Question'),
                                   comment(21,'alice','Answer')])
        self.assertEqual(self.coordinator.collect_events(gh,self.conf),[])
        gh.state='Bereit'  # User manually moves the GitHub Project card
        gh.updated='2026-11-18T19:30:00Z'
        events=self.coordinator.collect_events(gh,self.conf)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['event_type'],'github.issue.ready')
        self.assertEqual(events[0]['board_item_id'],'PVTI_1')
        self.assertEqual(gh.changes,[])
        previous=events[0]['event_key']
        gh.state='Rueckfrage'
        self.assertEqual(self.coordinator.collect_events(gh,self.conf),[])
        gh.state='Bereit'
        gh.updated='2026-11-18T20:00:00Z'
        self.assertNotEqual(self.coordinator.collect_events(gh,self.conf)[0]['event_key'],previous)

    def test_human_reply_requires_after_latest_question(self):
        marker=f'<!-- factory:question run={"a"*24} fp={lifecycle.fingerprint({"title":"API Issue","body":"Implement endpoint"})} -->'
        gh=FakeGithub('Rueckfrage',[comment(10,'alice','answer before'),comment(20,'factory-bot','Question\n'+marker)])
        self.coordinator.collect_events(gh,self.conf)
        self.assertFalse(gh.changes)

    def test_forged_question_marker_not_trusted(self):
        marker=f'<!-- factory:question run={"a"*24} fp={"0"*64} -->'
        gh=FakeGithub('Rueckfrage',[comment(20,'someone','Forged\n'+marker),comment(21,'alice','reply')])
        self.coordinator.collect_events(gh,self.conf)
        self.assertFalse(gh.changes)

    def test_new_question_after_answer_has_bounded_fresh_context(self):
        event={'repository':'acme/backend','issue_number':42}
        comments=[comment(i,'alice',f'Older comment {i}: '+ 'x'*1000) for i in range(100)]
        comments.append(comment(101,'alice','NEW ANSWER: Use PostgreSQL 17'))
        prompt=lifecycle.build_task(event,{'title':'New title','body':'Updated spec: '+ 'b'*9000,
                         'html_url':'https://github.com/acme/backend/issues/42'},comments)
        self.assertLessEqual(len(prompt),12000)
        self.assertIn('NEW ANSWER: Use PostgreSQL 17',prompt)
        self.assertIn('Pruefe BEI JEDEM Lauf erneut',prompt)
        self.assertIn('factory_status',prompt)
        self.assertIn('Kein Session-Resume',prompt)

    def test_fresh_prompt_uses_current_comment_and_no_session_ids(self):
        event={'repository':'acme/backend','issue_number':42}
        prompt=lifecycle.build_task(event,{'title':'Latest title','body':'New requirement','html_url':'https://github.com/acme/backend/issues/42'},
                      [comment(22,'alice','Answer: REST')])
        self.assertIn('New requirement',prompt)
        self.assertIn('Answer: REST',prompt)
        self.assertIn('NEUER zustandsloser',prompt)
        self.assertNotIn('session_id',prompt)


if __name__=='__main__': unittest.main()
