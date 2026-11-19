"""v5.14: Development is a mandatory planned intake and the top queue priority."""
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import yaml

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('factory_v514_app',ROOT/'github-registry'/'app.py')
app=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)
import intake
import planner

CFG=(ROOT/'tests'/'fixtures'/'backend.yaml').read_text().replace(
    'repository: meine-org/backend-api','repository: acme/backend').replace('owner: meine-org','owner: acme').replace('  auto_execute: false', '  auto_execute: true')


def mk_issue(number, label, body='New change'):
    return {'number':number,'id':number+1000,'node_id':f'I_{number}',
            'state':'open','title':f'Issue {number}','body':body,
            'labels':[{'name':label}] if label else [],
            'created_at':'2026-11-18T19:00:00Z','updated_at':'2026-11-18T20:00:00Z'}


class Board:
    def __init__(self,conf,cards):
        self.board=conf['kanban'];self.cards=cards;self.transitions=[]
    def all_cards(self):return self.cards
    def set_status(self,item_id,key):
        self.transitions.append((item_id,key))
        for card in self.cards:
            if card['item_id']==item_id:
                card['status']=self.board['columns'][key]
                return
        raise AssertionError(item_id)
    def current_status(self,item_id):
        return next(c['status'] for c in self.cards if c['item_id']==item_id)


def card(issue,status):
    return {'issue':issue,'status':status,'item_id':f'ITEM_{issue["number"]}',
            'item_updated_at':'2026-11-18T20:01:00Z'}


class FakeCursor:
    def __init__(self,events):self.events=events;self.sql=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def execute(self,sql,params=None):self.sql.append(sql)
    def fetchall(self):return self.events
    def fetchone(self):return None


class FakeDB:
    def __init__(self,cursor):self._cursor=cursor
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self):return self._cursor


class DevelopmentIntakeTests(unittest.TestCase):
    def setUp(self):
        self.conf=app.parse_config(CFG,'repos/backend.yaml')

    def test_all_three_statuses_required_when_intake_enabled(self):
        self.assertIn('development_intake',self.conf['kanban']['columns'])
        for key in ('development_intake','bug_intake','feature_intake'):
            with self.subTest(key=key):
                text=CFG.replace(f'    {key}: {self.conf["kanban"]["columns"][key]}\n','')
                with self.assertRaisesRegex(app.RegistryError,'requires kanban.columns.development_intake'):
                    app.parse_config(text,'repos/backend.yaml')

    def test_development_classification_strict_and_inherited(self):
        issue=mk_issue(4,'factory:development')
        self.assertEqual(intake.issue_type(issue),'development')
        self.assertIsNone(intake.issue_type({**issue,'labels':[{'name':'factory:bug'},{'name':'factory:development'}]}))
        self.assertIsNone(intake.issue_type({**issue,'labels':[{'name':'factory:review-defect'},{'name':'factory:development'}]}))
        self.assertEqual(intake.issue_type({**issue,'labels':[]},{'source_type':'development'}),'development')
        self.assertIsNone(intake.issue_type({**issue,'labels':[{'name':'factory:feature'}]},{'source_type':'development'}))

    def test_development_form_has_change_and_related_issue_fields(self):
        form=yaml.safe_load((ROOT/'github-registry/forms/00-factory-development.yml').read_text())
        self.assertEqual(form['labels'],['factory:development'])
        ids={x.get('id') for x in form['body']}
        self.assertTrue({'kind','goal','related_issue','current_state','acceptance'}.issubset(ids))
        self.assertIn('00-factory-development.yml',intake.FORMS)

    @patch.object(app, 'reconcile_monitored_repositories', new=lambda configs: None)
    def test_scan_onboards_development_in_separate_column(self):
        issues=[mk_issue(1,'factory:development'),mk_issue(2,'factory:bug'),mk_issue(3,'factory:feature')]
        board=Mock();board.all_cards.return_value=[]
        coord=Mock();coord.board.return_value=board;coord.collect_events.return_value=[]
        with patch.dict(app.os.environ,{'FACTORY_GITHUB_REGISTRY_TOKEN':'test','FACTORY_GITHUB_REGISTRY_REPO':'acme/registry'}),\
             patch.object(app.GitHubClient,'registry',return_value=[self.conf]),\
             patch.object(app.GitHubClient,'issues',return_value=iter(issues)),\
             patch.object(app,'last_state',return_value=None),\
             patch.object(app,'store_repo_scan',return_value=[]),\
             patch.object(app,'COORD',coord):
            response=app.scan()
        self.assertEqual(response['repositories'][0]['status'],'ok')
        self.assertEqual([c.args[1] for c in board.add_issue_to_backlog.call_args_list],
                         ['development_intake','bug_intake','feature_intake'])

    def test_development_root_is_planned_not_developed(self):
        issue=mk_issue(50,'factory:development')
        board=Board(self.conf,[card(issue,'Entwicklung Eingang')])
        gh=Mock();gh.issue.return_value=issue
        with patch.object(app.COORD,'board',return_value=board),patch.object(app.COORD,'_track',return_value=None):
            events=app.COORD.collect_events(gh,self.conf)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['work_type'],'development')
        self.assertEqual(events[0]['event_type'],'github.pipeline.plan')

    def test_development_issue_claim_starts_planner_and_global_sql_priority(self):
        issue=mk_issue(50,'factory:development')
        board=Board(self.conf,[card(issue,'Entwicklung Eingang')])
        gh=Mock();gh.issue.return_value=issue
        event={'board_item_id':board.cards[0]['item_id'],'repository':'acme/backend',
               'issue_number':50,'work_type':'development','kanban':self.conf['kanban'],
               'planning':self.conf['planning'],'pipeline':self.conf['pipeline']}
        cursor=FakeCursor([(25,event)])
        with patch.dict(os.environ,{'FACTORY_GITHUB_AUTORUN_ENABLED':'true'}),\
             patch.object(app.COORD,'_configs',return_value=(gh,{'acme/backend':self.conf})),\
             patch.object(app.COORD,'board',return_value=board),\
             patch.object(app.COORD,'_track',return_value=None),\
             patch.object(app.COORD.app,'database',return_value=FakeDB(cursor)):
            result=app.COORD.claim()
        self.assertEqual(result['stage'],'planner')
        self.assertEqual(board.transitions,[(board.cards[0]['item_id'],'planning')])
        self.assertIn("WHEN 'development' THEN 0 WHEN 'bug' THEN 1 WHEN 'feature' THEN 2",cursor.sql[0])
        self.assertIn('FOR UPDATE SKIP LOCKED',cursor.sql[0])

    def test_development_plan_tasks_keep_type_and_idempotent(self):
        plan={'features':[{'key':'existing-architecture','title':'Existing code changes',
             'description':'Adjust implementation while keeping the API compatible',
             'tasks':[{'key':'adjust-endpoint','title':'Adjust existing endpoint',
                       'description':'Modify existing endpoint and verify regression behavior',
                       'acceptance_criteria':['Existing consumer requests continue to succeed'],
                       'depends_on':[]}]}]}
        issues=[];links={}
        class GH:
            def pages(self,path,query=None):
                if path.endswith('/sub_issues'):
                    return [x for x in issues if x['id'] in links.get(path,set())]
                return list(issues)
            def request(self,method,path,data):
                if path.endswith('/sub_issues'):
                    links.setdefault(path,set()).add(data['sub_issue_id'])
                    return next(x for x in issues if x['id']==data['sub_issue_id'])
                n=101+len(issues)
                obj=mk_issue(n,None,body=data['body'])
                obj['title']=data['title'];obj['labels']=[{'name':x} for x in data.get('labels',[])]
                issues.append(obj)
                return obj
        gh=GH()
        a=planner.publish_plan(gh,'acme/backend',50,plan,Mock(),work_type='development')
        b=planner.publish_plan(gh,'acme/backend',50,plan,Mock(),work_type='development')
        self.assertEqual(a['created_numbers'],[101,102])
        self.assertEqual(b['created_numbers'],[])
        self.assertEqual(issues[0]['title'],'[Entwicklungsbereich] Existing code changes')
        self.assertEqual(issues[1]['title'],'[Entwicklung] Adjust existing endpoint')
        self.assertEqual(planner.parse_child(issues[1])['source_type'],'development')
        self.assertEqual(intake.issue_type(issues[1],planner.parse_child(issues[1])),'development')
        self.assertIn('factory:planned',[x['name'] for x in issues[1]['labels']])

    def test_development_groups_complete_when_tasks_done(self):
        # Regression: the original grouping regex must recognize Entwicklungsbereich.
        parent=mk_issue(50,'factory:development')
        area=mk_issue(101,'factory:development',
            body='<!-- factory:plan-parent=50 kind=feature key=existing-architecture deps= source=development -->')
        task=mk_issue(102,'factory:development',
            body='Entwicklungsbereich: #101 Existing code changes\n'
                 '<!-- factory:plan-parent=50 kind=task key=adjust-endpoint deps= source=development -->')
        plan_card=card(parent,'Erledigt')
        area_card=card(area,'Backlog')
        task_card=card(task,'Erledigt')
        board=Board(self.conf,[plan_card,area_card,task_card])
        board.config={'repository':'acme/backend'}
        gh=Mock()
        result=planner.release_tasks(gh,board)
        self.assertEqual(result,[])
        self.assertIn((area_card['item_id'],'done'),board.transitions)
        gh.request.assert_called_once()

if __name__=='__main__':unittest.main()
