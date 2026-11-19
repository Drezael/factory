"""Offline Contract tests; real GitHub GraphQL remains an integration test."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

BASE=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('factory_v512_app',BASE/'github-registry'/'app.py')
app=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)
import board_setup
import planner

YAML = '''
version: 1
repository: acme/widget
issues:
  required_labels: []
agent:
  auto_execute: false
kanban:
  enabled: true
  owner_type: organization
  owner: acme
  auto_create: true
  project_title: 'Factory - acme/widget'
  view_name: Factory Kanban
  status_field: Status
  columns:
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
  provider: codex
  max_tasks: 20
'''

PLAN = {'features':[
    {'key':'auth','title':'Authentication','description':'Implement secure login and session handling',
     'tasks':[
         {'key':'auth-login','title':'Implement login endpoint',
          'description':'Provide a POST login endpoint with hashed passwords',
          'acceptance_criteria':['Valid credentials return success'], 'depends_on':[]},
         {'key':'auth-tests','title':'Test authentication flow',
          'description':'Add automated negative and positive authentication tests',
          'acceptance_criteria':['Invalid passwords are rejected safely'],
          'depends_on':['auth-login']}
     ]}
]}

class BoardFake:
    def __init__(self, exists=False):
        self.exists=exists
        self.status_options=([{'id':'old0','name':'Backlog','color':'GRAY','description':'prior'}]
                             if exists else [])
        self.linked=exists
        self.view_exists=exists
        self.calls=[]
    def graphql(self,q,variables):
        self.calls.append((q,variables))
        if 'repository(owner:$owner,name:$name)' in q:
            return {'repository':{'id':'R_1','nameWithOwner':'acme/widget'}}
        if 'projectsV2(first:100' in q:
            return {'organization':{'projectsV2':{'nodes':(
                [{'id':'PVT_1','number':4,'title':'Factory - acme/widget','url':'https://github.com/orgs/acme/projects/4'}]
                if self.exists else []),'pageInfo':{'hasNextPage':False,'endCursor':None}}}}
        if 'organization(login:$login) { id }' in q:
            return {'organization':{'id':'ORG_1'}}
        if 'createProjectV2(' in q:
            self.exists=True
            return {'createProjectV2':{'projectV2':{'id':'PVT_1','number':4,'title':'Factory - acme/widget','url':'https://github.com/orgs/acme/projects/4'}}}
        if 'updateProjectV2(input:' in q:
            return {'updateProjectV2':{'projectV2':{'id':'PVT_1'}}}
        if 'repositories(first:100' in q:
            return {'node':{'repositories':{'nodes':[{'id':'R_1'}] if self.linked else [],'pageInfo':{'hasNextPage':False}}}}
        if 'linkProjectV2ToRepository' in q:
            self.linked=True
            return {'linkProjectV2ToRepository':{'repository':{'id':'R_1'}}}
        if 'field(name:$name)' in q:
            return {'node':{'field':{'__typename':'ProjectV2SingleSelectField','id':'STATUS_1','options':self.status_options.copy()}}
                    } if self.exists and self.status_options else {'node':{'field':None}}
        if 'createProjectV2Field' in q:
            self.status_options=[{'id':f'id-{i}',**item} for i,item in enumerate(variables['options'])]
            return {'createProjectV2Field':{'projectV2Field':{'id':'STATUS_1'}}}
        if 'updateProjectV2Field' in q:
            options=variables['options']
            self.status_options=[{'id':o.get('id',f'id-{i}'),**o} for i,o in enumerate(options)]
            return {'updateProjectV2Field':{'projectV2Field':{'id':'STATUS_1'}}}
        if 'views(first:100)' in q:
            return {'node':{'views':{'nodes':[{'id':'V_1','name':'Factory Kanban','layout':'BOARD_LAYOUT'}] if self.view_exists else [],
                                   'pageInfo':{'hasNextPage':False}}}}
        if 'createProjectV2View' in q:
            self.view_exists=True
            return {'createProjectV2View':{'projectV2View':{'id':'V_1','name':variables['name'],'layout':'BOARD_LAYOUT'}}}
        raise AssertionError(q)

class BoardProvisionTests(unittest.TestCase):
    def test_autocreate_project_with_all_columns_view_and_private(self):
        cfg=app.parse_config(YAML,'repos/widget.yaml')
        self.assertIsNone(cfg['kanban']['project_number'])
        fake=BoardFake()
        result=board_setup.ensure_board(fake,cfg)
        self.assertEqual(result['status'],'created')
        self.assertEqual(len(fake.status_options),8)
        self.assertTrue(result['created_board_view'])
        self.assertTrue(any('public:false' in q for q,_ in fake.calls))
        # No second project, field update, or new view on rerun
        fake.calls.clear()
        other=board_setup.ensure_board(fake,cfg)
        self.assertEqual(other['status'],'ready')
        self.assertEqual(other['added_categories'],[])
        self.assertFalse(other['created_board_view'])
        self.assertFalse(any('createProjectV2(' in q for q,_ in fake.calls))

    def test_existing_status_ids_are_preserved(self):
        cfg=app.parse_config(YAML,'repos/widget.yaml')
        fake=BoardFake(exists=True)
        result=board_setup.ensure_board(fake,cfg)
        self.assertIn('Bereit',result['added_categories'])
        self.assertEqual(fake.status_options[0]['id'],'old0')
        self.assertEqual(len(fake.status_options),8)
        updated=[v for q,v in fake.calls if 'updateProjectV2Field' in q]
        self.assertEqual(updated[0]['options'][0]['id'],'old0')

    def test_explicit_number_missing_fails_safely(self):
        config=app.parse_config(YAML.replace('  auto_create: true','  auto_create: true\n  project_number: 3'),'repos/widget.yaml')
        fake=BoardFake()
        original=fake.graphql
        fake.graphql=Mock(side_effect=lambda q,v: {'organization':{'projectV2':None}} if 'projectV2(number:$number)' in q else original(q,v))
        with self.assertRaisesRegex(RuntimeError,'project_number'):
            board_setup.ensure_board(fake,config)

    def test_planning_requires_pipeline_and_board(self):
        with self.assertRaisesRegex(app.RegistryError,'requires pipeline'):
            app.parse_config(YAML.replace('pipeline:\n  enabled: true', 'pipeline:\n  enabled: false'),'repos/widget.yaml')

    def test_event_keeps_pipeline_planning_config(self):
        conf=app.parse_config(YAML,'repos/widget.yaml')
        event=app.make_event({'number':42,'title':'Refactor authentication',
            'body':'Improve credential handling','state':'open','labels':[],
            'updated_at':'2026-11-18T12:00:00Z','created_at':'2026-11-18T11:00:00Z'},conf)
        self.assertTrue(event['planning']['enabled'])
        self.assertTrue(event['pipeline']['enabled'])
        self.assertEqual(event['planning']['provider'],'codex')

    def test_missing_columns_prevent_duplicate_names(self):
        with self.assertRaises(app.RegistryError):
            app.parse_config(YAML.replace('    planning: Planung','    planning: Bereit'),'repos/widget.yaml')

class PlanningTests(unittest.TestCase):
    def test_plan_validate_structure_dependencies_and_cycles(self):
        cleaned=planner.validate_plan(PLAN)
        self.assertEqual(len(cleaned['features'][0]['tasks']),2)
        bad=json.loads(json.dumps(PLAN))
        bad['features'][0]['tasks'][0]['depends_on']=['auth-tests']
        with self.assertRaisesRegex(ValueError,'Circular'):
            planner.validate_plan(bad)
        bad['features'][0]['tasks'][0]['depends_on']=['unknown']
        with self.assertRaisesRegex(ValueError,'Unknown'):
            planner.validate_plan(bad)

    def test_marker_and_idempotent_child_publication(self):
        issues=[]; links={}
        class GH:
            def pages(self,path,query):
                if path.endswith('/sub_issues'):
                    return [x for x in issues if x['id'] in links.get(path,set())]
                return list(issues)
            def request(self,method,path,body):
                if path.endswith('/sub_issues'):
                    links.setdefault(path,set()).add(body['sub_issue_id'])
                    return next(x for x in issues if x['id']==body['sub_issue_id'])
                self_n=len(issues)+71
                issue={'number':self_n,'id':self_n+10000,'body':body['body'],'title':body['title']}
                issues.append(issue)
                return issue
        gh=GH()
        fake_board=Mock()
        a=planner.publish_plan(gh,'acme/widget',42,PLAN,fake_board)
        b=planner.publish_plan(gh,'acme/widget',42,PLAN,fake_board)
        self.assertEqual(a['features'],b['features'])
        self.assertEqual(a['tasks'],b['tasks'])
        self.assertEqual(len(issues),3)
        self.assertEqual(sum(len(v) for v in links.values()),3)
        self.assertEqual(b['created_numbers'],[])
        self.assertEqual(planner.parse_child(issues[0])['kind'],'feature')
        self.assertEqual(planner.parse_child(issues[2])['depends_on'],['auth-login'])

    def test_no_ready_until_plan_done_and_dependencies_done(self):
        cards=[
          {'item_id':'root','status':'In Arbeit','issue':{'number':42,'state':'closed','body':'Plan'}},
          {'item_id':'task1','status':'Backlog','issue':{'number':43,'state':'open','body':'Feature: #44\n<!-- factory:plan-parent=42 kind=task key=auth-login deps= -->'}},
          {'item_id':'task2','status':'Backlog','issue':{'number':45,'state':'open','body':'Feature: #44\n<!-- factory:plan-parent=42 kind=task key=auth-tests deps=auth-login -->'}},
        ]
        class Board:
            board={'columns':{'backlog':'Backlog','ready':'Bereit','done':'Erledigt'}}
            def all_cards(self):return cards
            def set_status(self,card,status):
                for x in cards:
                    if x['item_id']==card:
                        x['status']=self.board['columns'][status]
        board=Board()
        self.assertEqual(planner.release_tasks(None,board),[])
        cards[0]['status']='Erledigt'
        self.assertEqual(planner.release_tasks(None,board),[43])
        self.assertEqual(planner.release_tasks(None,board),[])
        cards[1]['status']='Erledigt'
        self.assertEqual(planner.release_tasks(None,board),[45])

    def test_planner_role_reports_plan_data(self):
        spec=importlib.util.spec_from_file_location('agent_v512',BASE/'worker'/'agent.py')
        worker=importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
        parsed=worker.parse_final_message(json.dumps({'factory_status':'completed',
            'summary':'Plan created','plan':PLAN}),require_structured=True,role='planner')
        self.assertEqual(parsed[0],'ok')
        self.assertEqual(parsed[3]['features'][0]['key'],'auth')
        self.assertEqual(worker.parse_final_message('Free text',require_structured=True,role='planner')[0],'error')

if __name__=='__main__':unittest.main()
