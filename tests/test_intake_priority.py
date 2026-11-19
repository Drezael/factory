"""v5.13: typed Bug/Feature planning, issue form onboarding, and bug-first queue."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('v513_registry',ROOT/'github-registry'/'app.py')
app=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(app)
import intake
import pipeline
import planner

YAML_TEXT='''
version: 1
repository: acme/app
issues:
  enabled: true
  required_labels: []
  ignored_labels: [factory:review-defect]
agent:
  provider: codex
  role: developer
  auto_execute: true
kanban:
  enabled: true
  owner_type: organization
  owner: acme
  project_number: 5
  status_field: Status
  columns:
    development_intake: Entwicklung-Eingang
    bug_intake: Bug-Eingang
    feature_intake: Feature-Eingang
    backlog: Backlog
    ready: Bereit
    planning: Planung
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
  provider: claude
intake:
  enabled: true
  auto_install_forms: true
'''


def issue(number,labels=(),body='Ziel beschreiben',state='open'):
    return {'number':number,'id':number+10000,'node_id':f'I_{number}',
            'title':'Autorisierte Anforderung','body':body,'state':state,
            'html_url':f'https://github.com/acme/app/issues/{number}',
            'created_at':'2026-11-18T12:00:00Z',
            'updated_at':'2026-11-18T13:00:00Z',
            'labels':[{'name':value} for value in labels]}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.conf=app.parse_config(YAML_TEXT,'repos/app.yaml')

    def test_requires_planning_and_intake_for_automatic_development(self):
        for fragment in ('planning:\n  enabled: true','intake:\n  enabled: true'):
            with self.subTest(section=fragment):
                invalid=YAML_TEXT.replace(fragment,fragment.replace('true','false'))
                with self.assertRaises(app.RegistryError):
                    app.parse_config(invalid,'repos/app.yaml')

    def test_distinct_columns_are_required_for_typed_intake(self):
        for key in ('    development_intake: Entwicklung-Eingang\n','    bug_intake: Bug-Eingang\n','    feature_intake: Feature-Eingang\n'):
            with self.subTest(key=key):
                with self.assertRaisesRegex(app.RegistryError,'development_intake, bug_intake and feature_intake'):
                    app.parse_config(YAML_TEXT.replace(key,''),'repos/app.yaml')

    def test_issue_type_strict_without_title_guessing_or_qa(self):
        self.assertEqual(intake.issue_type(issue(1,['factory:bug'])),'bug')
        self.assertEqual(intake.issue_type(issue(1,['factory:feature'])),'feature')
        self.assertIsNone(intake.issue_type(issue(1)))
        self.assertIsNone(intake.issue_type(issue(1,['factory:bug','factory:feature'])))
        self.assertIsNone(intake.issue_type(issue(1,['factory:bug','factory:review-defect'])))
        self.assertEqual(intake.issue_type(issue(2),{'source_type':'bug'}),'bug')
        self.assertIsNone(intake.issue_type(issue(2,['factory:feature']),{'source_type':'bug'}))

    def test_generated_issue_forms_validate_and_assign_correct_labels(self):
        for filename,expected in (('00-factory-development.yml','factory:development'),('01-factory-bug.yml','factory:bug'),
                                  ('02-factory-feature.yml','factory:feature')):
            obj=yaml.safe_load((ROOT/'github-registry'/'forms'/filename).read_text())
            self.assertIn(expected,obj['labels'])
            self.assertTrue(obj['name'] and obj['description'] and obj['body'])
            self.assertGreaterEqual(len(obj['body']),4)

    def test_auto_install_creates_once_but_never_overwrites_form(self):
        paths={}
        labels=set()
        class GitHub:
            def get(self,path,query=None):
                if path=='repos/acme/app':return {'default_branch':'main'},''
                if '/labels/' in path:
                    if path.split('/')[-1] not in labels: raise RuntimeError('GitHub HTTP 404')
                    return {'name':path.split('/')[-1]},''
                if '/contents/' in path:
                    if path not in paths: raise RuntimeError('GitHub HTTP 404')
                    return {'type':'file'},''
                raise AssertionError(path)
            def request(self,method,path,payload):
                if '/labels' in path:
                    labels.add(payload['name']);return {'name':payload['name']}
                if '/contents/' in path:
                    self.assertEqual(method,'PUT')
                    paths[path]=payload
                    return {'content':{'path':path}}
                raise AssertionError((method,path))
        gh=GitHub()
        # bind the test assertion used inside the stub
        gh.assertEqual=self.assertEqual
        first=intake.ensure_intake(gh,self.conf)
        self.assertEqual(len(first['created_labels']),4)
        self.assertEqual(len(first['created_forms']),3)
        self.assertEqual(intake.ensure_intake(gh,self.conf)['created_forms'],[])
        self.assertEqual(len(paths),3)
        self.assertTrue(all(x['branch']=='main' for x in paths.values()))

    @patch.object(app, 'reconcile_monitored_repositories', new=lambda configs: None)
    def test_scan_onboards_bug_feature_and_unknown_separately(self):
        board=Mock();board.all_cards.return_value=[]
        coord=Mock();coord.board.return_value=board;coord.collect_events.return_value=[]
        rows=[issue(1,['factory:bug']),issue(2,['factory:feature']),issue(3)]
        with patch.dict(app.os.environ,{'FACTORY_GITHUB_REGISTRY_TOKEN':'test',
                                        'FACTORY_GITHUB_REGISTRY_REPO':'acme/registry'}),\
             patch.object(app.GitHubClient,'registry',return_value=[self.conf]),\
             patch.object(app.GitHubClient,'issues',return_value=iter(rows)),\
             patch.object(app,'last_state',return_value=None),\
             patch.object(app,'store_repo_scan',return_value=[]),\
             patch.object(app,'COORD',coord):
            response=app.scan()
        self.assertEqual(response['repositories'][0]['status'],'ok')
        self.assertEqual(board.add_issue_to_backlog.call_args_list,[(('I_1','bug_intake'),{}),
                           (('I_2','feature_intake'),{}),(('I_3',),{})])

    def test_bug_and_feature_ready_both_plan_first(self):
        co=pipeline.PipelineCoordinator(app.COORD.app)
        original=issue(42,['factory:bug'])
        card={'issue':original,'status':'Bug-Eingang','item_id':'ITEM42','item_updated_at':'2026-11-18T13:05:00Z'}
        board=Mock();board.all_cards.return_value=[card];board.board=self.conf['kanban']
        gh=Mock();gh.issue.return_value=original
        with patch.object(co,'board',return_value=board),patch.object(co,'_track',return_value=None),\
             patch.object(planner,'release_tasks',return_value=[]):
            items=co.collect_events(gh,self.conf)
            card['status']='Bereit'
            items=co.collect_events(gh,self.conf)
            self.assertEqual(items[0]['event_type'],'github.pipeline.plan')
            self.assertEqual(items[0]['work_type'],'bug')
            original=issue(42,['factory:feature'])
            gh.issue.return_value=original;card['issue']=original
            items=co.collect_events(gh,self.conf)
            self.assertEqual(items[0]['event_type'],'github.pipeline.plan')
            self.assertEqual(items[0]['work_type'],'feature')
            gh.issue.return_value=issue(42)
            self.assertEqual(co.collect_events(gh,self.conf),[])

    def test_bug_plan_creates_fix_issues_with_inherited_type_and_markers(self):
        plan={'features':[{'key':'fix-auth','title':'Login Fixbereich',
            'description':'Fix authentication error and verify edge cases',
            'tasks':[{'key':'fix-login','title':'Fix failing login',
              'description':'Add regression coverage and repair endpoint behavior',
              'acceptance_criteria':['Reproduced failure no longer occurs'],
              'depends_on':[]}]}]}
        existing=[];links={}
        class Github:
            def pages(self,path,query):
                if path.endswith('/sub_issues'):
                    return [x for x in existing if x['id'] in links.get(path,set())]
                return list(existing)
            def request(self,method,path,payload):
                if path.endswith('/sub_issues'):
                    links.setdefault(path,set()).add(payload['sub_issue_id'])
                    return next(x for x in existing if x['id']==payload['sub_issue_id'])
                n=101+len(existing)
                result=issue(n,payload.get('labels',()),body=payload['body'])
                result['title']=payload['title']; existing.append(result)
                return result
        gh=Github()
        first=planner.publish_plan(gh,'acme/app',50,plan,Mock(),work_type='bug')
        second=planner.publish_plan(gh,'acme/app',50,plan,Mock(),work_type='bug')
        self.assertEqual(first['features'],second['features'])
        self.assertEqual(len(existing),2)
        self.assertTrue(existing[0]['title'].startswith('[Fixbereich]'))
        self.assertTrue(existing[1]['title'].startswith('[Fix]'))
        self.assertEqual(planner.parse_child(existing[1])['source_type'],'bug')
        self.assertEqual(intake.issue_type(existing[1]),'bug')
        self.assertTrue(all('factory:planned' in [x['name'] for x in child['labels']] for child in existing))
        self.assertTrue(planner.verify_native_child(gh,'acme/app',existing[1],planner.parse_child(existing[1])))
        self.assertFalse(planner.verify_native_child(gh,'acme/app',issue(999),planner.parse_child(existing[1])))

    def test_sql_dispatch_orders_bugs_before_features_globally(self):
        co=pipeline.PipelineCoordinator(app.COORD.app)
        seen=[]
        cur=Mock();cur.fetchall.return_value=[]
        cur.execute.side_effect=lambda sql,*args:seen.append(sql)
        cm=Mock();cm.__enter__=Mock(return_value=cm);cm.__exit__=Mock(return_value=False)
        cm.cursor.return_value=cm
        cm.execute=cur.execute;cm.fetchall=cur.fetchall
        with patch.dict(pipeline.os.environ,{'FACTORY_GITHUB_AUTORUN_ENABLED':'true'}),\
             patch.object(co,'_configs',return_value=(Mock(),{})),\
             patch.object(co.app,'database',return_value=cm),\
             patch.object(pipeline.lifecycle.Coordinator,'claim',return_value={'claimed':False}):
            self.assertFalse(co.claim()['claimed'])
        query=' '.join(seen)
        self.assertIn("payload->>'work_type' WHEN 'development' THEN 0 WHEN 'bug' THEN 1 WHEN 'feature' THEN 2",query)
        self.assertIn('FOR UPDATE SKIP LOCKED',query)


if __name__ == '__main__':
    unittest.main()
