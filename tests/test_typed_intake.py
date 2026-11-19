"""Offline v5.13 contracts: intake separation, planning gate, bug first."""
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('factory_v513_app',ROOT/'github-registry'/'app.py')
app=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)
import intake
import planner

CFG='''
version: 1
repository: acme/widget
issues:
  required_labels: []
agent:
  provider: codex
  role: developer
  auto_execute: true
kanban:
  enabled: true
  owner_type: organization
  owner: acme
  auto_create: true
  project_title: 'Factory - acme/widget'
  view_name: Factory Kanban
  status_field: Status
  columns:
    development_intake: Entwicklung-Eingang
    bug_intake: Bug Eingang
    feature_intake: Feature Eingang
    backlog: Backlog
    planning: Planung
    ready: Bereit
    in_progress: In Arbeit
    waiting_for_answer: Rueckfrage
    review: Review
    done: Erledigt
    failed: Fehler
pipeline:
  enabled: true
  required_checks: [factory-tests]
planning:
  enabled: true
  max_tasks: 40
intake:
  enabled: true
  auto_install_forms: false
'''

def item(num, status, label, *, body='', source=None):
    return {'item_id':f'PVT_ITEM_{num}', 'status':status,
        'item_updated_at':'2026-11-18T21:30:00Z',
        'issue':{'number':num,'title':f'#{num}', 'body':body, 'state':'open',
          'created_at':'2026-11-18T20:00:00Z','updated_at':'2026-11-18T21:30:00Z'}}

class FakeBoard:
    def __init__(self, config, cards):
        self.config=config
        self.board=config['kanban']
        self.cards=cards
        self.transitions=[]
    def all_cards(self): return self.cards
    def current_status(self, item_id):
        return next(x['status'] for x in self.cards if x['item_id']==item_id)
    def set_status(self, item_id, status_key):
        self.transitions.append((item_id,status_key))
        for c in self.cards:
            if c['item_id']==item_id:
                c['status']=self.board['columns'][status_key]
                return
        raise KeyError(item_id)

class FakeCursor:
    def __init__(self, events): self.events=events; self.sql=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def execute(self, sql, params=None): self.sql.append((sql,params))
    def fetchall(self): return self.events
    def fetchone(self):return None

class FakeDB:
    def __init__(self,cur):self.cur=cur
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self):return self.cur

class TypedIntakeTests(unittest.TestCase):
    def setUp(self):
        self.conf=app.parse_config(CFG,'repos/widget.yaml')

    def test_typed_repo_requires_mandatory_planning_and_board_columns(self):
        self.assertTrue(self.conf['intake']['enabled'])
        self.assertTrue(self.conf['planning']['enabled'])
        self.assertTrue(self.conf['pipeline']['enabled'])
        for bad, error in [
            (CFG.replace('planning:\n  enabled: true','planning:\n  enabled: false'),'requires planning'),
            (CFG.replace('    development_intake: Entwicklung-Eingang\n',''),'development_intake'),
            (CFG.replace('    bug_intake: Bug Eingang\n',''),'bug_intake'),
            (CFG.replace('intake:\n  enabled: true','intake:\n  enabled: false'),'requires planning.enabled'),
        ]:
            with self.subTest(error=error), self.assertRaises(app.RegistryError):
                app.parse_config(bad,'repos/widget.yaml')

    def test_type_requires_single_valid_label_or_generated_origin(self):
        self.assertEqual(intake.issue_type({'labels':[{'name':'factory:bug'}]}),'bug')
        self.assertEqual(intake.issue_type({'labels':[{'name':'factory:feature'}]}),'feature')
        self.assertIsNone(intake.issue_type({'labels':[]}))
        self.assertIsNone(intake.issue_type({'labels':[{'name':'factory:bug'},{'name':'factory:feature'}]}))
        self.assertIsNone(intake.issue_type({'labels':[{'name':'factory:bug'},{'name':'factory:review-defect'}]}))
        self.assertEqual(intake.issue_type({'labels':[]},{'source_type':'bug'}),'bug')
        self.assertIsNone(intake.issue_type({'labels':[{'name':'factory:feature'}]},{'source_type':'bug'}))

    def test_plan_published_as_typed_bugfix_children_idempotently(self):
        plan={'features':[{'key':'login','title':'Login fixes','description':'Make login work correctly',
            'tasks':[{'key':'login-fix','title':'Fix incorrect login redirect',
                      'description':'Handle the invalid redirect regression',
                      'acceptance_criteria':['Redirects work for valid credentials'],
                      'depends_on':[]}]}]}
        issues=[];links={}
        class GitHub:
            def pages(self,path,query):
                if path.endswith('sub_issues'):
                    return [x for x in issues if x['id'] in links.get(path,set())]
                return list(issues)
            def request(self,method,path,data):
                if path.endswith('/sub_issues'):
                    links.setdefault(path,set()).add(data['sub_issue_id'])
                    return next(x for x in issues if x['id']==data['sub_issue_id'])
                n=len(issues)+100
                it={'id':10000+n,'number':n,'title':data['title'],
                    'body':data['body'],'labels':[{'name':x} for x in data.get('labels',[])]}
                issues.append(it)
                return it
        gh=GitHub()
        a=planner.publish_plan(gh,'acme/widget',42,plan,None,work_type='bug')
        b=planner.publish_plan(gh,'acme/widget',42,plan,None,work_type='bug')
        self.assertEqual(a['created_numbers'],[100,101]);self.assertEqual(b['created_numbers'],[])
        self.assertIn('[Fixbereich]',issues[0]['title'])
        self.assertIn('[Fix]',issues[1]['title'])
        self.assertEqual(planner.parse_child(issues[1])['source_type'],'bug')
        self.assertEqual(intake.issue_type(issues[1],planner.parse_child(issues[1])),'bug')
        self.assertEqual([x['name'] for x in issues[1]['labels']],['factory:bug','factory:planned'])

    def test_intake_columns_create_planning_events_not_direct_development(self):
        card=item(42,'Bug Eingang','factory:bug')
        board=FakeBoard(self.conf,[card])
        gh=Mock()
        gh.issue.return_value={**card['issue'],'labels':[{'name':'factory:bug'}]}
        coord=app.COORD
        with patch.object(coord,'board',return_value=board), patch.object(coord,'_track',return_value=None):
            events=coord.collect_events(gh,self.conf)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['work_type'],'bug')
        self.assertEqual(events[0]['event_type'],'github.pipeline.plan')

    def test_review_defect_never_automatically_scheduled(self):
        card=item(42,'Bereit','factory:review-defect')
        board=FakeBoard(self.conf,[card])
        gh=Mock()
        gh.issue.return_value={**card['issue'],'labels':[{'name':'factory:review-defect'}]}
        coord=app.COORD
        with patch.object(coord,'board',return_value=board), patch.object(coord,'_track',return_value=None):
            self.assertEqual(coord.collect_events(gh,self.conf),[])

    def test_claim_on_bug_intake_dispatches_planner_and_priority_query(self):
        card=item(42,'Bug Eingang','factory:bug')
        board=FakeBoard(self.conf,[card])
        gh=Mock()
        gh.issue.return_value={**card['issue'],'labels':[{'name':'factory:bug'}]}
        event={'board_item_id':card['item_id'],'repository':'acme/widget','issue_number':42,'work_type':'bug',
               'kanban':self.conf['kanban'],'pipeline':self.conf['pipeline'], 'planning':self.conf['planning']}
        cur=FakeCursor([(11,event)])
        coord=app.COORD
        with patch.dict(os.environ,{'FACTORY_GITHUB_AUTORUN_ENABLED':'true'}),\
             patch.object(coord,'_configs',return_value=(gh,{'acme/widget':self.conf})),\
             patch.object(coord,'board',return_value=board),\
             patch.object(coord,'_track',return_value=None),\
             patch.object(coord.app,'database',return_value=FakeDB(cur)):
            result=coord.claim()
        self.assertTrue(result['claimed'])
        self.assertEqual(result['stage'],'planner')
        self.assertEqual(board.transitions,[(card['item_id'],'planning')])
        self.assertIn("WHEN 'development' THEN 0 WHEN 'bug' THEN 1 WHEN 'feature' THEN 2",cur.sql[0][0])

    def test_reopen_with_ready_after_question_runs_planner_again(self):
        card=item(42,'Bereit','factory:bug')
        board=FakeBoard(self.conf,[card])
        gh=Mock()
        gh.issue.return_value={**card['issue'],'labels':[{'name':'factory:bug'}]}
        event={'board_item_id':card['item_id'],'repository':'acme/widget','issue_number':42,'work_type':'bug',
               'kanban':self.conf['kanban'],'pipeline':self.conf['pipeline'], 'planning':self.conf['planning']}
        cur=FakeCursor([(11,event)])
        coord=app.COORD
        with patch.dict(os.environ,{'FACTORY_GITHUB_AUTORUN_ENABLED':'true'}),\
             patch.object(coord,'_configs',return_value=(gh,{'acme/widget':self.conf})),\
             patch.object(coord,'board',return_value=board),\
             patch.object(coord,'_track',return_value=None),\
             patch.object(coord.app,'database',return_value=FakeDB(cur)):
            result=coord.claim()
        self.assertEqual(result['stage'],'planner')
        self.assertEqual(board.transitions,[(card['item_id'],'planning')])

    def test_forms_use_existing_type_labels(self):
        import yaml
        for name, label in [('00-factory-development.yml','factory:development'),('01-factory-bug.yml','factory:bug'),('02-factory-feature.yml','factory:feature')]:
            data=yaml.safe_load((ROOT/'github-registry'/'forms'/name).read_text())
            self.assertIn(label,data['labels'])
            self.assertTrue(data['body'])

if __name__=='__main__':unittest.main()
