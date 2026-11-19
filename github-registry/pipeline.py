"""Factory v5.11. GitHub-issue / PR automation, controlled by n8n ticks.

This module does NOT spawn an orchestrator worker: the n8n schedule calls the
registry adapter and the existing manager, which spawns disposable AI workers.
GitHub Projects Status is the sole business state; PostgreSQL is only a ledger
of runs, PR SHA/review approvals, and idempotent defect links.

The GitHub WRITE token never enters the AI worker. Its workspace is read-only
here; vetted files are published using the Git Data API.
"""
from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote
import uuid

import lifecycle
import planner
import intake

LOG = logging.getLogger('factory.github-pipeline')
SHA = re.compile(r'^[0-9a-f]{40}$')
JOB_ID = re.compile(r'^[a-f0-9]{20}$')
BRANCH = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,139}$')
MAX_FILES = 12000
MAX_CHANGED = 120
MAX_BYTES = 25 * 1024 * 1024
MAX_FILE = 3 * 1024 * 1024
FORBIDDEN_PATHS = ('.github/workflows/', '.github/CODEOWNERS', 'CODEOWNERS',
                   '.env', '.env.local', '.npmrc', '.pypirc', '.git-credentials')
# Generated folders and caches are not part of a PR. A tracked file in one of
# these folders is intentionally kept untouched on GitHub, never deleted.
SKIP_PARTS = frozenset(('.git','node_modules','.venv','venv','__pycache__',
    '.pytest_cache','.mypy_cache','.ruff_cache','.tox','.next','.cache',
    'dist','build','coverage','.idea','.vscode'))
SKIP_FILES = frozenset(('.coverage','.DS_Store'))


def parse_pipeline(raw, path, *, enabled=True):
    if raw is None:
        return {'enabled':False}
    if not isinstance(raw,dict):
        raise ValueError(f'{path}: pipeline must be mapping')
    allowed={'enabled','base_branch','developer_provider','reviewer_provider',
             'max_review_cycles','required_checks','auto_merge'}
    if set(raw)-allowed:
        raise ValueError(f'{path}: unknown pipeline keys: {sorted(set(raw)-allowed)}')
    use=raw.get('enabled',False)
    if not isinstance(use,bool):
        raise ValueError(f'{path}: pipeline.enabled must be boolean')
    if not use:
        return {'enabled':False}
    if not enabled:
        raise ValueError(f'{path}: enabled pipeline on a disabled repository')
    base=raw.get('base_branch','main')
    if not isinstance(base,str) or not BRANCH.fullmatch(base) or '..' in base or base.endswith(('/', '.')):
        raise ValueError(f'{path}: invalid base_branch')
    dev=raw.get('developer_provider','codex')
    review=raw.get('reviewer_provider','claude')
    if dev not in ('codex','claude','gemini') or review not in ('codex','claude','gemini'):
        raise ValueError(f'{path}: invalid developer_provider/reviewer_provider')
    max_cycles=raw.get('max_review_cycles',3)
    if isinstance(max_cycles,bool) or not isinstance(max_cycles,int) or not 1<=max_cycles<=10:
        raise ValueError(f'{path}: max_review_cycles must be between 1 and 10')
    checks=raw.get('required_checks', [])
    if (not isinstance(checks,list) or len(checks)>20 or
            len(set(map(str,checks)))!=len(checks) or
            any(not isinstance(x,str) or not 1<=len(x)<=140 for x in checks)):
        raise ValueError(f'{path}: required_checks must be a list of unique check names')
    if 'code-factory/agent-review' in checks:
        raise ValueError(f'{path}: agent review is implicitly required, do not list it twice')
    auto=raw.get('auto_merge',False)
    if not isinstance(auto,bool) or (auto and not checks):
        raise ValueError(f'{path}: auto_merge requires non-empty required_checks')
    return {'enabled':True,'base_branch':base,'developer_provider':dev,
            'reviewer_provider':review,'max_review_cycles':max_cycles,
            'required_checks':checks,'auto_merge':auto}


def repo_path(slug):
    # slug already normalized by app.py config parser
    if not isinstance(slug,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',slug):
        raise ValueError('Invalid GitHub repository')
    return 'repos/'+ '/'.join(quote(x,safe='') for x in slug.split('/'))


def read(gh,path,query=None):
    return gh.get(path,query=query)[0]


def publish_status(gh, repo, sha, state, description):
    if not SHA.fullmatch(str(sha)) or state not in ('success','failure','pending','error'):
        raise ValueError('Invalid commit SHA/status')
    return gh.request('POST',f'{repo_path(repo)}/statuses/{sha}',{
        'state':state, 'context':'code-factory/agent-review',
        'description':description[:130]})


def ignored_artifact(path):
    return any(part in SKIP_PARTS for part in path.split('/')) or path.split('/')[-1] in SKIP_FILES or path.endswith('.pyc')


def safe_files(root):
    """Collect only source files, not runtime caches or .git internals.

    Never follow symlinks or allow generated files to leak into GitHub.
    """
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Agent output directory missing or symlinked')
    collected={};total=0
    for parent, dirs, filenames in os.walk(root,topdown=True,followlinks=False):
        base=Path(parent)
        dirs[:]=[d for d in dirs if d not in SKIP_PARTS]
        for name in filenames:
            item=base/name
            relative=item.relative_to(root).as_posix()
            if ignored_artifact(relative):
                continue
            if item.is_symlink():
                raise ValueError(f'Symlinks are not supported in agent output: {relative}')
            if not item.is_file():
                raise ValueError(f'Unsupported file in workspace: {relative}')
            if len(collected)>=MAX_FILES:
                raise ValueError('Repository too large for starter publisher')
            size=item.stat().st_size
            if size>MAX_FILE:
                raise ValueError(f'Oversized source file: {relative}')
            total+=size
            if total>MAX_BYTES:
                raise ValueError('Repository output exceeds size limit')
            collected[relative]=item.read_bytes()
    return collected


def _git_sha(content):
    return hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest()


def publish_changes(gh, repo, number, job_id, pipeline, *, existing_pr=None):
    """Publish agent output using Git Trees API; GitHub token remains in adapter.

    A trusted GitHub API snapshot serves as the base. The untrusted AI checkout
    supplies file content only (never .git hooks/config or a commit command).
    """
    if not JOB_ID.fullmatch(str(job_id)):
        raise ValueError('Invalid job ID')
    root=Path('/opt/factory-workspaces')/job_id/'project'
    files=safe_files(root)
    branch=f'factory/issue-{number}'
    base=pipeline['base_branch']
    endpoint=repo_path(repo)
    if existing_pr:
        branch=existing_pr['branch'];ref_name=branch
        remote=read(gh,f'{endpoint}/git/ref/heads/{quote(ref_name,safe="/")}')
        parent=remote['object']['sha']
    else:
        parent=read(gh,f'{endpoint}/git/ref/heads/{quote(base,safe="/")}')['object']['sha']
    if not SHA.fullmatch(str(parent)):
        raise ValueError('Invalid GitHub base SHA')
    parent_commit=read(gh,f'{endpoint}/git/commits/{parent}')
    tree_sha=parent_commit['tree']['sha']
    remote_tree=read(gh,f'{endpoint}/git/trees/{tree_sha}',query={'recursive':'1'})
    if remote_tree.get('truncated'):
        raise ValueError('GitHub repository tree truncated; refusing incomplete publish')
    remote={}
    for entry in remote_tree.get('tree',[]):
        path=entry['path']
        if entry.get('type')=='blob':
            remote[path]=entry
        elif entry.get('type')=='commit':
            # Submodule contents do not appear in output, but must not be deleted.
            raise ValueError('Submodule repository unsupported by publisher')
    if len(remote)>MAX_FILES:
        raise ValueError('Remote repository too large for starter publisher')
    # Protected paths are preserved intact even if the checkout has no matching file.
    changes=[]
    for path, content in sorted(files.items()):
        previous=remote.get(path)
        digest=_git_sha(content)
        if previous and previous['sha']==digest:
            continue
        if path in FORBIDDEN_PATHS or any(path.startswith(x) for x in FORBIDDEN_PATHS if x.endswith('/')):
            raise ValueError(f'Protected configuration modified by agent: {path}')
        if b'\0' in content:
            raise ValueError(f'Binary changes not supported: {path}')
        if previous and previous.get('mode')!='100644':
            raise ValueError(f'Cannot modify file mode / symlink: {path}')
        changes.append((path,content))
    for path in sorted(set(remote)-set(files)):
        if ignored_artifact(path):
            continue
        if path in FORBIDDEN_PATHS or any(path.startswith(x) for x in FORBIDDEN_PATHS if x.endswith('/')):
            continue
        if remote[path].get('mode')!='100644':
            raise ValueError(f'Cannot delete special Git file: {path}')
        changes.append((path,None))
    if not changes:
        raise ValueError('Developer did not change any repository files')
    if len(changes)>MAX_CHANGED:
        raise ValueError('Too many changed files for a single autonomous run')
    tree_changes=[]
    for path, content in changes:
        if content is None:
            tree_changes.append({'path':path,'mode':'100644','type':'blob','sha':None})
        else:
            blob=gh.request('POST',f'{endpoint}/git/blobs',{
                'content':base64.b64encode(content).decode('ascii'), 'encoding':'base64'})
            tree_changes.append({'path':path,'mode':'100644','type':'blob','sha':blob['sha']})
    new_tree=gh.request('POST',f'{endpoint}/git/trees',{'base_tree':tree_sha,'tree':tree_changes})
    new_commit=gh.request('POST',f'{endpoint}/git/commits',{
        'message':f'factory: implement #{number}', 'tree':new_tree['sha'], 'parents':[parent]})
    head=new_commit['sha']
    if existing_pr:
        gh.request('PATCH',f'{endpoint}/git/refs/heads/{quote(branch,safe="/")}',{'sha':head,'force':False})
    else:
        gh.request('POST',f'{endpoint}/git/refs',{'ref':'refs/heads/'+branch,'sha':head})
    pr=existing_pr
    if pr is None:
        owner=repo.split('/')[0]
        listed=read(gh,f'{endpoint}/pulls',{'head':f'{owner}:{branch}','state':'open','per_page':25})
        if listed:
            pr={'number':listed[0]['number'],'branch':branch}
        else:
            created=gh.request('POST',f'{endpoint}/pulls',{
                'title':f'Factory: #{number}',
                'body':f'Automatischer Entwicklungs-Pull-Request fuer #{number}.\n\nCloses #{number}',
                'head':branch,'base':base,'draft':False})
            pr={'number':created['number'],'branch':branch}
    return {'number':pr['number'],'branch':branch,'base':base,'head_sha':head,'changes':len(changes)}


def checks_state(gh,repo,sha,checks):
    """All configured CI checks must be SUCCESS on the exact reviewed SHA."""
    endpoint=repo_path(repo)
    checks_resp=read(gh,f'{endpoint}/commits/{sha}/check-runs',{'per_page':100})
    statuses=read(gh,f'{endpoint}/commits/{sha}/status')
    records={}
    for run in checks_resp.get('check_runs',[]):
        if run['name'] in checks:
            records[run['name']]=('success' if run.get('status')=='completed' and run.get('conclusion')=='success'
                                  else 'failure' if run.get('status')=='completed' else 'pending')
    for status in statuses.get('statuses',[]):
        if status.get('context') in checks and status['context'] not in records:
            records[status['context']]=status['state']
    if any(records.get(name) in ('failure','error') for name in checks):
        return 'failure',records
    if any(records.get(name)!='success' for name in checks):
        return 'pending',records
    return 'success',records


def create_review_defect(gh,repo,parent,pr,sha,findings,*,digest=''):
    details='\n'.join(f'- **{x["title"]}** ({x.get("path") or "unbekannt"}): {x["detail"]}' for x in findings)
    title=f'[Factory QA] Fehler bei #{parent}: {findings[0]["title"]}'[:240]
    body=(f'Automatisch erkannter Review-/Testfehler.\n\n'
          f'Ursprungs-Issue: #{parent}\nPull Request: #{pr}\nCommit: `{sha}`\n\n'
          f'### Befunde\n{details}\n\n'
          f'<!-- factory:review-defect parent={parent} sha={sha} digest={digest} -->')[:60000]
    # Provision dedicated marker label so generated review defects are NEVER
    # routed as independent development jobs by the regular Issue scanner.
    try:
        read(gh,f'{repo_path(repo)}/labels/{quote("factory:review-defect",safe="")}')
    except Exception as exc:
        if '404' not in str(exc):
            raise
        gh.request('POST',f'{repo_path(repo)}/labels',{
            'name':'factory:review-defect','color':'B60205',
            'description':'Generated by independent Factory review; do not auto-dispatch'})
    return gh.request('POST',f'{repo_path(repo)}/issues',{
        'title':title,'body':body,'labels':['factory:review-defect']})['number']


class PipelineCoordinator(lifecycle.Coordinator):
    def _configs(self):
        gh=self.client()
        reg=os.getenv('FACTORY_GITHUB_REGISTRY_REPO','')
        configs=gh.registry(reg,self.app.effective_registry_ref(gh,reg),
                            os.getenv('FACTORY_GITHUB_REGISTRY_DIR','repos'))
        return gh, {c['repository'].lower():c for c in configs if c['enabled'] and c['pipeline']['enabled']}

    def _track(self,repo,number):
        with self.app.database() as conn,conn.cursor() as cur:
            cur.execute('''SELECT branch,base_branch,pr_number,head_sha,reviewed_sha,review_verdict,retry_count
                FROM factory_registry.pr_tracking WHERE repository=%s AND issue_number=%s''',(repo,number))
            row=cur.fetchone()
        if not row:
            return None
        return dict(zip(('branch','base_branch','pr_number','head_sha','reviewed_sha','review_verdict','retry_count'),row))

    def _save_track(self,repo,number,data):
        with self.app.database() as conn,conn.cursor() as cur:
            cur.execute('''INSERT INTO factory_registry.pr_tracking
                (repository,issue_number,branch,base_branch,pr_number,head_sha,reviewed_sha,review_verdict,retry_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(repository,issue_number) DO UPDATE SET
                branch=EXCLUDED.branch,base_branch=EXCLUDED.base_branch,
                pr_number=EXCLUDED.pr_number,head_sha=EXCLUDED.head_sha,
                reviewed_sha=EXCLUDED.reviewed_sha,review_verdict=EXCLUDED.review_verdict,
                retry_count=EXCLUDED.retry_count,updated_at=NOW()''',
                (repo,number,data['branch'],data['base_branch'],data.get('pr_number'),data.get('head_sha'),
                 data.get('reviewed_sha'),data.get('review_verdict'),data.get('retry_count',0)))

    def collect_events(self,gh,config,*,first_scan_at=None):
        if not config.get('pipeline',{}).get('enabled'):
            return super().collect_events(gh,config,first_scan_at=first_scan_at)
        board=self.board(gh,config)
        result=[]
        statuses=config['kanban']['columns']
        if config.get('planning',{}).get('enabled'):
            # GitHub Status is the sole business state. Automatic release does
            # not use a worker; it checks that the plan and all dependencies are Done.
            planner.release_tasks(gh,board)
        intake_statuses=({statuses['development_intake'],statuses['bug_intake'],statuses['feature_intake']}
                         if config.get('intake',{}).get('enabled') and
                            config['intake'].get('auto_plan_on_create',True) else set())
        for card in board.all_cards():
            issue=card['issue']; status=card['status']
            if issue.get('state')!='open' or status not in (
                    {statuses['ready'],statuses['in_progress'],statuses['review']} | intake_statuses):
                continue
            # Child review defects stay in GitHub as separate tickets but are not
            # independently auto-dispatched or allowed to trigger a review loop.
            details=gh.issue(config['repository'],issue['number'])
            labels={x.get('name') for x in details.get('labels',[]) if isinstance(x,dict)}
            if 'factory:review-defect' in labels:
                continue
            child=planner.parse_child(details)
            work_type=intake.issue_type(details,child)
            if config.get('intake',{}).get('enabled'):
                if work_type not in ('development','bug','feature'):
                    # Unclassified/mixed-label Issues must be categorized first.
                    continue
                if status in intake_statuses and (child is not None or
                        status!=statuses[work_type+'_intake']):
                    continue
            state=self._track(config['repository'],issue['number'])
            if status==statuses['in_progress'] and (not state or state.get('review_verdict')!='fail'):
                continue
            if status==statuses['review'] and (not state or not state.get('pr_number') or
                (state['reviewed_sha'] and state['reviewed_sha']==state['head_sha'])):
                continue
            if first_scan_at and config['issues']['initial_sync']=='new_only':
                if self.app.iso_dt(card.get('item_updated_at') or issue['updated_at'])<=first_scan_at:
                    continue
            event=self.app.make_event(issue,config)
            event['board_item_id']=card['item_id'];event['board_updated_at']=card.get('item_updated_at')
            event['work_type']=work_type
            event['event_type']='github.pipeline.'+('review' if status==statuses['review'] else
                'plan' if child is None and config.get('planning',{}).get('enabled') else 'develop')
            event['event_key']=hashlib.sha256(json.dumps([config['repository'].lower(),issue['number'],
              card['item_id'],card.get('item_updated_at'),status,state['head_sha'] if state else ''],sort_keys=True).encode()).hexdigest()
            result.append(event)
        return result

    def claim(self):
        if os.getenv('FACTORY_GITHUB_AUTORUN_ENABLED','false').lower()!='true':
            return {'claimed':False,'reason':'FACTORY_GITHUB_AUTORUN_ENABLED=false'}
        gh,configs=self._configs()
        with self.app.database() as conn,conn.cursor() as cur:
            cur.execute('''SELECT id,payload FROM factory_registry.issue_events
                WHERE status='pending' ORDER BY
                CASE payload->>'work_type' WHEN 'development' THEN 0 WHEN 'bug' THEN 1 WHEN 'feature' THEN 2 ELSE 3 END,
                id FOR UPDATE SKIP LOCKED LIMIT 100''')
            for event_id,payload in cur.fetchall():
                event=payload if isinstance(payload,dict) else json.loads(payload)
                conf=configs.get(event['repository'].lower())
                if not conf:
                    # Keep original v5.10 behavior for non-pipeline repos; dispatch
                    # through the legacy Coordinator with its own idempotence lock.
                    continue
                if not conf['agent']['auto_execute'] or not event.get('board_item_id'):
                    continue
                repo,number=event['repository'],event['issue_number']
                board=self.board(gh,conf)
                status=board.current_status(event['board_item_id'])
                track=self._track(repo,number)
                stage=None
                planning_enabled=conf.get('planning',{}).get('enabled',False)
                child=None
                if planning_enabled:
                    issue=gh.issue(repo,number)
                    child=planner.parse_child(issue)
                    if conf.get('intake',{}).get('enabled'):
                        live_type=intake.issue_type(issue,child)
                        if live_type not in ('development','bug','feature') or event.get('work_type')!=live_type:
                            # Labels were changed after scan or no valid type.
                            # Never execute with stale/missing classification.
                            cur.execute("UPDATE factory_registry.issue_events SET status='acknowledged', acknowledged_at=NOW() WHERE id=%s",(event_id,))
                            continue
                        if child:
                            # HTML markers can be pasted by anyone. Trust only
                            # a real GitHub native sub-Issue relationship and a
                            # matching, typed original planning Issue.
                            root=gh.issue(repo,child['parent'])
                            if (intake.issue_type(root)!=live_type or
                                not planner.verify_native_child(gh,repo,issue,child)):
                                board.set_status(event['board_item_id'],'backlog')
                                cur.execute("UPDATE factory_registry.issue_events SET status='acknowledged', acknowledged_at=NOW() WHERE id=%s",(event_id,))
                                continue
                    if child and child['kind']=='feature':
                        # Feature card groups execution tasks; never execute it.
                        cur.execute("UPDATE factory_registry.issue_events SET status='acknowledged' WHERE id=%s",(event_id,))
                        continue
                intake_statuses=({conf['kanban']['columns']['development_intake'],
                                  conf['kanban']['columns']['bug_intake'],
                                  conf['kanban']['columns']['feature_intake']}
                                 if conf.get('intake',{}).get('enabled') and
                                    conf['intake'].get('auto_plan_on_create',True) else set())
                if status in intake_statuses:
                    # A new typed issue automatically starts PLANNING, never development.
                    if not planning_enabled or child or status!=conf['kanban']['columns'][event['work_type']+'_intake']:
                        cur.execute("UPDATE factory_registry.issue_events SET status='acknowledged',acknowledged_at=NOW() WHERE id=%s",(event_id,))
                        continue
                    stage='planner'
                elif status==conf['kanban']['columns']['ready']:
                    # Every root Issue is planned first. Only generated tasks
                    # enter development after their Planning Issue is Done.
                    if planning_enabled and child is None:
                        stage='planner'
                    elif planning_enabled and child and child['kind']=='task':
                        cards=self.board(gh,conf).all_cards()
                        parent=next((x for x in cards if x['issue']['number']==child['parent']),None)
                        dependencies={}
                        for c in cards:
                            marker=planner.parse_child(c['issue'])
                            if marker and marker['parent']==child['parent'] and marker['kind']=='task':
                                dependencies[marker['key']]=c
                        if (parent is None or parent['status']!=conf['kanban']['columns']['done'] or
                            any(dep not in dependencies or dependencies[dep]['status']!=conf['kanban']['columns']['done']
                                for dep in child['depends_on'])):
                            # A manual Ready cannot bypass unfinished planning.
                            board.set_status(event['board_item_id'],'backlog')
                            cur.execute("UPDATE factory_registry.issue_events SET status='acknowledged' WHERE id=%s",(event_id,))
                            continue
                        stage=('reviewer' if track and track.get('pr_number') and
                               track.get('review_verdict')=='needs_input' else 'developer')
                    else:
                        stage=('reviewer' if track and track.get('pr_number')
                               and track.get('review_verdict')=='needs_input' else 'developer')
                elif status==conf['kanban']['columns']['in_progress'] and track and track['review_verdict']=='fail':
                    stage='developer'
                elif status==conf['kanban']['columns']['review'] and track and track['pr_number']:
                    live=read(gh,f'{repo_path(repo)}/pulls/{track["pr_number"]}')
                    if live.get('state')=='open' and live['head']['sha']!=track.get('reviewed_sha'):
                        stage='reviewer'
                if not stage:
                    cur.execute("UPDATE factory_registry.issue_events SET status='acknowledged', acknowledged_at=NOW() WHERE id=%s",(event_id,))
                    continue
                cur.execute('''SELECT 1 FROM factory_registry.issue_runs WHERE repository=%s AND issue_number=%s
                            AND status IN ('claimed','running') LIMIT 1''',(repo,number))
                if cur.fetchone():
                    continue
                if track and stage=='developer' and track['retry_count']>=conf['pipeline']['max_review_cycles']:
                    board.set_status(event['board_item_id'],'failed')
                    cur.execute("UPDATE factory_registry.issue_events SET status='acknowledged' WHERE id=%s",(event_id,))
                    continue
                # Do not reserve an Issue / move it into development until the
                # chosen login profile has a usable, sufficiently fresh quota.
                provider = (conf['planning'].get('provider', conf['agent']['provider'])
                            if stage == 'planner' else
                            conf['pipeline']['reviewer_provider'] if stage == 'reviewer' else
                            conf['pipeline']['developer_provider'])
                if not lifecycle.quota_ready(provider, stage):
                    continue
                if status==conf['kanban']['columns']['ready'] or status in intake_statuses:
                    board.set_status(event['board_item_id'],
                                     'review' if stage=='reviewer' else
                                     ('planning' if stage=='planner' and 'planning' in conf['kanban']['columns'] else 'in_progress'))
                run_id=uuid.uuid4().hex[:24]
                cur.execute('''INSERT INTO factory_registry.issue_runs(id,event_id,repository,issue_number,status,stage)
                    VALUES (%s,%s,%s,%s,'claimed',%s)''',(run_id,event_id,repo,number,stage))
                cur.execute("UPDATE factory_registry.issue_events SET status='acknowledged',acknowledged_at=NOW() WHERE id=%s",(event_id,))
                return {'claimed':True,'run_id':run_id,'repository':repo,'issue_number':number,'stage':stage}
        # There is no legacy fallback: it could execute stale queued events
        # after a .repo file has been deleted from the registry.
        return {'claimed':False,'reason':'No eligible work in the current registry allowlist'}

    def read_run(self,run_id):
        base=super().read_run(run_id)
        with self.app.database() as conn,conn.cursor() as cur:
            cur.execute('SELECT stage FROM factory_registry.issue_runs WHERE id=%s',(run_id,))
            row=cur.fetchone()
        base['stage']=row[0] if row else 'analysis'
        return base

    def start(self,run_id):
        run=self.read_run(run_id)
        if run['state']=='claimed':
            # A .repo file can be removed between claim and start. Re-read the
            # authoritative allowlist immediately before spawning a container.
            _, current=self._configs()
            if run['repository'].lower() not in current:
                with self.app.database() as conn,conn.cursor() as cur:
                    cur.execute("UPDATE factory_registry.issue_runs SET status='failed',updated_at=NOW() WHERE id=%s AND status='claimed'",(run_id,))
                raise self.app.RegistryError('Repository removed from registry: worker start refused')
        if run['stage'] not in ('developer','reviewer','planner'):
            return super().start(run_id)
        if run['state']!='claimed':
            if run['state']=='running' and run.get('job_id'):
                return {'run_id':run_id,'status':'running','job_id':run['job_id'],'already_started':True}
            raise self.app.RegistryError('Run cannot be started again')
        event=run['event'];gh=self.client();repo=run['repository'];number=run['issue_number']
        track=self._track(repo,number)
        live=gh.issue(repo,number)
        if live.get('state')!='open':
            raise self.app.RegistryError('Issue is closed')
        board=self.board(gh,event)
        expected=('review' if run['stage']=='reviewer' else
                  ('planning' if run['stage']=='planner' and 'planning' in event['kanban']['columns'] else 'in_progress'))
        if board.current_status(event['board_item_id'])!=event['kanban']['columns'][expected]:
            raise self.app.RegistryError('Kanban Status changed before worker start')
        task=lifecycle.build_task(event,live,list(gh.issue_comments(repo,number)))
        settings=event['pipeline'];stage=run['stage']
        provider=(event.get('planning',{}).get('provider',event['agent']['provider']) if stage=='planner' else
                  (settings['reviewer_provider'] if stage=='reviewer' else settings['developer_provider']))
        if stage=='planner':
            if event.get('work_type')=='development':
                task+='\n\nENTWICKLUNG: plan development or changes to ongoing work, with constraints and dependencies. '
                task+='Ensure changes to existing implementation are included as independent tasks and regression checks. '
            elif event.get('work_type')=='bug':
                task+='\n\nBUG REPORT: plan diagnosis, regression tests, targeted fix, verification steps. '
                task+='Treat plan.features as logical FIX AREAS, not new product features. '
            elif event.get('work_type')=='feature':
                task+='\n\nFEATURE REQUEST: plan implementation features, independent tasks, tests and acceptance criteria. '
            task+='\n\nPLANNING PHASE: no code modifications, no direct GitHub Issue creation. '
            task+='Return a structured plan with features, tasks, acceptance criteria and dependency keys. '
            task+='The Factory validates the plan and creates the Issues after this worker has finished.'
        if stage=='reviewer':
            if not track or not track.get('pr_number'):
                raise self.app.RegistryError('Review requires an open pull request')
            pr=read(gh,f'{repo_path(repo)}/pulls/{track["pr_number"]}')
            if pr.get('state')!='open':
                raise self.app.RegistryError('Pull request not open')
            task+='\n\nINDEPENDENT REVIEW. Evaluate code changes, test quality and behavior of '
            task+=f'PR #{track["pr_number"]}, HEAD {pr["head"]["sha"]}, base {track["base_branch"]}. '
            task+='Return review=pass ONLY if code and tests appear correct. Return review=fail with findings otherwise.'
            changed=read(gh,f'{repo_path(repo)}/pulls/{track["pr_number"]}/files',{'per_page':100})
            patches=[]
            for item in changed[:100]:
                patch=str(item.get('patch') or '')[:3500]
                patches.append(f'{item.get("filename", "unknown")} ({item.get("status", "?")}):\n{patch}')
            task+='\n\nPR DIFF (bounded):\n'+'\n'.join(patches)[:4200]
        else:
            if track and track.get('review_verdict')=='fail':
                task+=f'\n\nAUTO-REWORK after failed review, PR #{track["pr_number"]}. '
                task+='Fix the review defects linked in the issue, update tests. This is a fresh independent run.'
                with self.app.database() as conn,conn.cursor() as cur:
                    cur.execute('''SELECT defect_number FROM factory_registry.review_defects
                                   WHERE repository=%s AND issue_number=%s
                                   ORDER BY defect_number DESC LIMIT 3''',(repo,number))
                    children=[row[0] for row in cur.fetchall()]
                for defect in children:
                    child=gh.issue(repo,defect)
                    task+=(f'\n\nReview-Fehler-Issue #{defect}: '
                           +str(child.get('body') or '')[:2500])
        task=task[:11980]
        kwargs={'provider':provider,'role':stage,'repository':repo,'task':task,
                'timeout_seconds':int(event['agent'].get('timeout_seconds',900)),'structured_result':True}
        if track and track.get('pr_number'):
            kwargs['ref']=track['branch']
        response=lifecycle._manager('POST','/jobs',kwargs)
        with self.app.database() as conn,conn.cursor() as cur:
            cur.execute("UPDATE factory_registry.issue_runs SET status='running',job_id=%s,updated_at=NOW() WHERE id=%s AND status='claimed' RETURNING id",(response['job_id'],run_id))
            if not cur.fetchone():
                raise self.app.RegistryError('Concurrent job start detected')
        return {'run_id':run_id,'stage':stage,'status':'running','job_id':response['job_id']}

    def _post_once(self,gh,repo,number,marker,message):
        if not any(marker in str(c.get('body') or '') and (c.get('user') or {}).get('login','').lower()==gh.identity().lower() for c in gh.issue_comments(repo,number)):
            gh.post_comment(repo,number,(message+'\n\n'+marker)[:6400])

    def _question(self,run,result,gh,board):
        event=run['event'];repo=run['repository'];number=run['issue_number']
        if run.get('stage')=='reviewer':
            tracking=self._track(repo,number)
            if tracking:
                tracking['review_verdict']='needs_input'
                self._save_track(repo,number,tracking)
        marker=f'<!-- factory:question run={run["run_id"]} -->'
        self._post_once(gh,repo,number,marker,
            '### Rückfrage der Code Factory\n\n'+result['question']+
            '\n\nBitte antworte im selben Issue und verschiebe das Ticket danach manuell auf **Bereit**. '
            'Der Worker wurde beendet; ein neuer Lauf wird erst nach deiner Freigabe gestartet.')
        board.set_status(event['board_item_id'],'waiting_for_answer')
        return 'needs_input'

    def _defect_once(self,gh,repo,number,pr,sha,findings):
        digest=hashlib.sha256(json.dumps(findings,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.app.database() as conn,conn.cursor() as cur:
            cur.execute('''SELECT defect_number FROM factory_registry.review_defects
                WHERE repository=%s AND issue_number=%s AND head_sha=%s AND report_digest=%s''',
                (repo,number,sha,digest))
            old=cur.fetchone()
            if old:
                return old[0]
        # Crash recovery: a remote Issue may have been created before the
        # local ledger transaction committed. Search stable marker first.
        marker=f'<!-- factory:review-defect parent={number} sha={sha} digest={digest} -->'
        child=None
        for candidate in gh.issues(repo,required_labels=['factory:review-defect']):
            if marker in (candidate.get('body') or ''):
                child=candidate['number'];break
        if child is None:
            child=create_review_defect(gh,repo,number,pr,sha,findings,digest=digest)
        with self.app.database() as conn,conn.cursor() as cur:
            cur.execute('''INSERT INTO factory_registry.review_defects
                (repository,issue_number,head_sha,report_digest,defect_number)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (repo,number,sha,digest,child))
        return child

    def finish(self,run_id):
        run=self.read_run(run_id)
        if run['stage'] not in ('developer','reviewer','planner'):
            return super().finish(run_id)
        if run['state']!='running':
            return {'run_id':run_id,'already_finished':True,'state':run['state']}
        info=lifecycle._manager('GET',f'/jobs/{run["job_id"]}')
        if info['status'] in ('running','pending'):
            raise self.app.RegistryError('Worker still running')
        gh=self.client();event=run['event'];repo=run['repository'];number=run['issue_number']
        board=self.board(gh,event);result=info.get('result') or {}
        track=self._track(repo,number)
        success=info['status']=='succeeded' and result.get('status') in ('ok','needs_input')
        newstate='failed';outcome='failed'
        try:
            if success and result['status']=='needs_input' and str(result.get('question') or '').strip():
                outcome=self._question(run,result,gh,board)
                newstate='completed'
            elif success and result['status']=='ok' and run['stage']=='planner':
                plan=planner.validate_plan(result.get('plan'))
                task_count=sum(len(f['tasks']) for f in plan['features'])
                if task_count>event.get('planning',{}).get('max_tasks',40):
                    raise self.app.RegistryError('Planner returned too many tasks for registry configuration')
                posted=planner.publish_plan(gh,repo,number,plan,board,work_type=event.get('work_type'))
                # Publish all feature/task Issues before declaring planning Done.
                marker=f'<!-- factory:plan-complete issue={number} -->'
                summaries=[f'### Planung abgeschlossen\n\n{result.get("summary", "Plan erstellt")}\n',
                           '\n**Features:**']
                summaries += [f'- #{n} `{k}`' for k,n in posted['features'].items()]
                summaries += ['\n**Arbeitsschritte:**']
                summaries += [f'- #{n} `{k}`' for k,n in posted['tasks'].items()]
                self._post_once(gh,repo,number,marker,'\n'.join(summaries)[:5900])
                board.set_status(event['board_item_id'],'done')
                # Keep original Planning Issue history in GitHub, but close it
                # only after all sub-Issues are created and recorded.
                if gh.issue(repo,number).get('state')=='open':
                    gh.request('PATCH',f'{repo_path(repo)}/issues/{number}',
                               {'state':'closed','state_reason':'completed'})
                newstate='completed';outcome='plan_completed'
            elif success and result['status']=='ok' and run['stage']=='developer':
                branch=track if track and track.get('pr_number') else None
                # Idempotent finish when GitHub publication succeeded but the
                # subsequent issue-comment/status mutation failed transiently.
                if track and track.get('pr_number') and track.get('review_verdict') is None:
                    current_pr=read(gh,f'{repo_path(repo)}/pulls/{track["pr_number"]}')
                    if current_pr.get('state')=='open' and current_pr['head']['sha']==track['head_sha']:
                        pr={'branch':track['branch'],'base':track['base_branch'],
                            'number':track['pr_number'],'head_sha':track['head_sha'],'changes':0}
                    else:
                        pr=publish_changes(gh,repo,number,run['job_id'],event['pipeline'],existing_pr=branch)
                else:
                    pr=publish_changes(gh,repo,number,run['job_id'],event['pipeline'],existing_pr=branch)
                updated={'branch':pr['branch'],'base_branch':pr['base'],'pr_number':pr['number'],
                         'head_sha':pr['head_sha'],'reviewed_sha':None,'review_verdict':None,
                         'retry_count':track['retry_count'] if track else 0}
                self._save_track(repo,number,updated)
                marker=f'<!-- factory:pull-request issue={number} pr={pr["number"]} sha={pr["head_sha"]} -->'
                self._post_once(gh,repo,number,marker,f'### Automatischer Pull Request #{pr["number"]}\n\n'
                                f'Branch `{pr["branch"]}`, {pr["changes"]} Dateiaenderungen. '
                                'Der naechste frische Worker prueft den aktuellen PR unabhaengig.')
                board.set_status(event['board_item_id'],'review')
                newstate='completed';outcome='review'
            elif success and result['status']=='ok' and run['stage']=='reviewer':
                if not track or not track.get('pr_number'):
                    raise self.app.RegistryError('Review has no published PR')
                live_pr=read(gh,f'{repo_path(repo)}/pulls/{track["pr_number"]}')
                sha=live_pr['head']['sha']
                if sha!=track['head_sha']:
                    raise self.app.RegistryError('PR head changed during review; re-review new SHA')
                verdict=result.get('review')
                if verdict not in ('pass','fail'):
                    raise self.app.RegistryError('Reviewer did not provide structured verdict; failing closed')
                previous_verdict=track.get('review_verdict')
                previous_review_sha=track.get('reviewed_sha')
                track['reviewed_sha']=sha;track['review_verdict']=verdict
                if verdict=='pass':
                    publish_status(gh,repo,sha,'success','Independent Factory AI review passed')
                    self._save_track(repo,number,track)
                    board.set_status(event['board_item_id'],'review')
                    newstate='completed';outcome='awaiting_ci'
                else:
                    findings=result.get('findings') or []
                    if not findings:
                        raise self.app.RegistryError('Failed review lacks actionable findings')
                    was_already_failed=(previous_review_sha==sha and previous_verdict=='fail')
                    child=self._defect_once(gh,repo,number,track['pr_number'],sha,findings)
                    publish_status(gh,repo,sha,'failure',f'Independent review failed: defect #{child}')
                    if not was_already_failed:
                        track['retry_count']+=1
                    self._save_track(repo,number,track)
                    self._post_once(gh,repo,number,f'<!-- factory:review-failed sha={sha} -->',
                                    f'### Review fehlgeschlagen\n\nFehler-Issue #{child} wurde erstellt. '
                                    f'Der naechste frische Developer-Lauf behebt die Befunde im bestehenden PR #{track["pr_number"]}.')
                    if track['retry_count']>=event['pipeline']['max_review_cycles']:
                        board.set_status(event['board_item_id'],'failed')
                        outcome='manual_attention'
                    else:
                        board.set_status(event['board_item_id'],'in_progress')
                        outcome='rework'
                    newstate='completed'
            else:
                # No automatic code rework for infrastructure failures: a
                # failed login/CLI execution does not prove a source defect.
                # Keep the issue auditable and require a human to put it Ready.
                self._post_once(gh,repo,number,f'<!-- factory:job-error run={run_id} -->',
                    f'### Agent-Fehler ({run["stage"]})\n\nDer Lauf konnte nicht sicher abgeschlossen werden. '
                    f'Bitte Ausfuehrungslogs fuer Job `{run["job_id"]}` pruefen und danach das Issue erneut freigeben.')
                board.set_status(event['board_item_id'],'failed')
                outcome='failed'
        except Exception:
            LOG.exception('Pipeline finish failed for %s/%s, run %s',repo,number,run_id)
            raise
        with self.app.database() as conn,conn.cursor() as cur:
            cur.execute("UPDATE factory_registry.issue_runs SET status=%s,updated_at=NOW() WHERE id=%s AND status='running'",(newstate,run_id))
        return {'run_id':run_id,'state':outcome,'stage':run['stage'],'job_id':run['job_id'],'result':result}

    def merge_ready(self):
        if os.getenv('FACTORY_GITHUB_AUTOMERGE_ENABLED','false').lower()!='true':
            return {'enabled':False,'merged':[],'waiting':[]}
        gh,configs=self._configs()
        merged=[];waiting=[]
        with self.app.database() as conn,conn.cursor() as cur:
            cur.execute('''SELECT repository,issue_number,branch,base_branch,pr_number,head_sha,reviewed_sha,review_verdict,retry_count
                           FROM factory_registry.pr_tracking WHERE review_verdict='pass' AND reviewed_sha=head_sha''')
            rows=cur.fetchall()
        for repo,num,branch,base,pr_num,sha,reviewed,verdict,cycles in rows:
            conf=configs.get(repo.lower())
            if not conf or not conf['pipeline']['auto_merge']:
                continue
            try:
                board=self.board(gh,conf)
                cards=[x for x in board.all_cards() if x['issue']['number']==num]
                if not cards or cards[0]['status']!=conf['kanban']['columns']['review']:
                    continue
                pull=read(gh,f'{repo_path(repo)}/pulls/{pr_num}')
                if pull.get('state')!='open' or pull['head']['sha']!=sha or pull['base']['ref']!=base:
                    waiting.append({'repository':repo,'issue':num,'reason':'PR changed or closed'})
                    continue
                check_result, statuses=checks_state(gh,repo,sha,conf['pipeline']['required_checks'])
                if check_result=='pending':
                    waiting.append({'repository':repo,'issue':num,'reason':'CI pending','checks':statuses})
                    continue
                if check_result=='failure':
                    findings=[{'title':'GitHub CI check failed','detail':f'{name}: {state}', 'path':''}
                              for name,state in statuses.items() if state in ('failure','error')]
                    child=self._defect_once(gh,repo,num,pr_num,sha,findings)
                    publish_status(gh,repo,sha,'failure',f'CI failed: defect #{child}')
                    track=self._track(repo,num);track['review_verdict']='fail';track['retry_count']+=1
                    self._save_track(repo,num,track)
                    self._post_once(gh,repo,num,f'<!-- factory:ci-failed sha={sha} -->',
                                    f'### CI fehlgeschlagen\n\nFehler-Issue #{child}. Zurueck zur automatischen Entwicklung.')
                    board.set_status(cards[0]['item_id'],
                        'failed' if track['retry_count']>=conf['pipeline']['max_review_cycles'] else 'in_progress')
                    waiting.append({'repository':repo,'issue':num,'reason':'CI failed; defect created'})
                    continue
                # GitHub branch protection is the FINAL gate. Merge REST rejects
                # required checks, approvals, protected branch policy violations.
                if pull.get('mergeable') is not True or pull.get('mergeable_state') not in ('clean','unstable'):
                    waiting.append({'repository':repo,'issue':num,'reason':'GitHub mergeability pending/blocked'})
                    continue
                response=gh.request('PUT',f'{repo_path(repo)}/pulls/{pr_num}/merge',{
                    'commit_title':f'Factory #{num}: {pull["title"][:140]}',
                    'sha':sha,'merge_method':'squash'})
                if not response.get('merged'):
                    raise RuntimeError('GitHub merge was not confirmed')
                # Mark done before closing (GitHub may automatically close via "Closes")
                board.set_status(cards[0]['item_id'],'done')
                current=gh.issue(repo,num)
                if current.get('state')=='open':
                    gh.request('PATCH',f'{repo_path(repo)}/issues/{num}',{
                        'state':'closed','state_reason':'completed'})
                self._post_once(gh,repo,num,f'<!-- factory:merged sha={sha} -->',
                                f'### Automatisch gemergt\n\nPR #{pr_num} erfolgreich per Squash gemergt. '
                                'Unabhaengiges Review und alle konfigurierten CI-Checks erfolgreich.')
                # All child QA defects on this feature/bug are resolved by the
                # verified final PR revision. Retain them as CLOSED GitHub issues
                # with their history and links to the parent.
                with self.app.database() as defect_conn,defect_conn.cursor() as defect_cur:
                    defect_cur.execute('''SELECT DISTINCT defect_number FROM factory_registry.review_defects
                        WHERE repository=%s AND issue_number=%s''',(repo,num))
                    closed_defects=[r[0] for r in defect_cur.fetchall()]
                for child_number in closed_defects:
                    issue=gh.issue(repo,child_number)
                    if issue.get('state')=='open':
                        gh.post_comment(repo,child_number,f'Behoben durch PR #{pr_num} fuer Issue #{num}.')
                        gh.request('PATCH',f'{repo_path(repo)}/issues/{child_number}',
                                   {'state':'closed','state_reason':'completed'})
                    for child_card in board.all_cards():
                        if child_card['issue']['number']==child_number:
                            board.set_status(child_card['item_id'],'done')
                            break
                merged.append({'repository':repo,'issue':num,'pull_request':pr_num})
            except Exception as exc:
                LOG.exception('Merge gate failed for %s#%s',repo,num)
                waiting.append({'repository':repo,'issue':num,'reason':str(exc)[:220]})
        return {'enabled':True,'merged':merged,'waiting':waiting}

    def pipeline_status(self):
        with self.app.database() as conn,conn.cursor() as cur:
            cur.execute('''SELECT count(*) FILTER (WHERE review_verdict='pass'), count(*) FILTER (WHERE review_verdict='fail')
                           FROM factory_registry.pr_tracking''')
            row=cur.fetchone()
        return {'approved':row[0] or 0,'needs_rework':row[1] or 0,
                'automated_merging_enabled':os.getenv('FACTORY_GITHUB_AUTOMERGE_ENABLED','false').lower()=='true'}
