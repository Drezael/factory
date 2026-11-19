"""Planning-Issue lifecycle: turn one plan result into feature/task Issues.

Human-facing questions always stay inside the original Issue. Only a finished
planning result creates implementation Issues. Work is released after the parent
Planning Issue is marked Done, with dependency-based incremental dispatch.
"""
from __future__ import annotations

import hashlib
import json
import re

MARKER = re.compile(r'<!-- factory:plan-parent=(\d+) kind=(feature|task) key=([a-z0-9-]{1,50})(?: deps=([a-z0-9,-]*))?(?: source=(development|bug|feature))? -->')
MAX_FEATURES = 12
MAX_TASKS = 40
MAX_PER_FEATURE = 12
KEY = re.compile(r'^[a-z][a-z0-9-]{1,49}$')


def parse_child(issue):
    """Return original Planning Issue and child marker from GitHub Issue body."""
    match = MARKER.search(str(issue.get('body') or ''))
    if not match:
        return None
    parent,kind,key,deps,source_type=match.groups()
    return {'parent':int(parent),'kind':kind,'key':key, 'source_type':source_type,
            'depends_on':[x for x in (deps or '').split(',') if x]}


def _text(text, field, minimum, maximum):
    if not isinstance(text,str) or not minimum<=len(text.strip())<=maximum:
        raise ValueError(f'Invalid planning {field} (length {minimum}..{maximum})')
    return text.strip()


def validate_plan(value):
    if not isinstance(value,dict) or set(value)!={'features'}:
        raise ValueError('Plan must contain exactly features')
    features=value['features']
    if not isinstance(features,list) or not 1<=len(features)<=MAX_FEATURES:
        raise ValueError(f'Plan must contain 1..{MAX_FEATURES} features')
    keys=set();feature_keys=set();task_count=0
    cleaned=[]
    for feat in features:
        if not isinstance(feat,dict) or set(feat)!={'key','title','description','tasks'}:
            raise ValueError('Invalid feature fields')
        fkey=feat['key']
        if not isinstance(fkey,str) or not KEY.fullmatch(fkey) or fkey in feature_keys:
            raise ValueError('Invalid or repeated feature key')
        feature_keys.add(fkey)
        tasks=feat['tasks']
        if not isinstance(tasks,list) or not 1<=len(tasks)<=MAX_PER_FEATURE:
            raise ValueError('Feature must contain 1..12 tasks')
        prepared=[]
        for t in tasks:
            if (not isinstance(t,dict) or
                set(t)!={'key','title','description','acceptance_criteria','depends_on'}):
                raise ValueError('Invalid task fields')
            key=t['key']
            if not isinstance(key,str) or not KEY.fullmatch(key) or key in keys:
                raise ValueError('Invalid or repeated task key')
            keys.add(key)
            criteria=t['acceptance_criteria']
            depends=t['depends_on']
            if (not isinstance(criteria,list) or not 1<=len(criteria)<=8 or
                any(not isinstance(x,str) or not 8<=len(x.strip())<=220 for x in criteria)):
                raise ValueError('Task needs 1..8 concrete acceptance criteria')
            if (not isinstance(depends,list) or len(depends)>12 or
                any(not isinstance(x,str) or not KEY.fullmatch(x) for x in depends) or len(set(depends))!=len(depends)):
                raise ValueError('Invalid task dependencies')
            prepared.append({'key':key,'title':_text(t['title'],'task title',5,150),
                'description':_text(t['description'],'task description',15,1400),
                'acceptance_criteria':[x.strip() for x in criteria],
                'depends_on':depends})
            task_count+=1
        cleaned.append({'key':fkey,'title':_text(feat['title'],'feature title',5,150),
                        'description':_text(feat['description'],'feature description',15,1400),
                        'tasks':prepared})
    if task_count>MAX_TASKS:
        raise ValueError(f'Maximum {MAX_TASKS} tasks per plan')
    # Dependency keys must exist. Reject cycles, including self dependencies.
    deps={t['key']:t['depends_on'] for f in cleaned for t in f['tasks']}
    for key, requirements in deps.items():
        if any(x not in deps for x in requirements):
            raise ValueError(f'Unknown task dependency in {key}')
    visiting=set();done=set()
    def visit(k):
        if k in visiting:
            raise ValueError('Circular task dependencies are not allowed')
        if k in done:
            return
        visiting.add(k)
        for prior in deps[k]:visit(prior)
        visiting.remove(k);done.add(k)
    for k in deps:visit(k)
    return {'features':cleaned}


def _all_repo_issues(gh, repo):
    from urllib.parse import quote
    owner,name=repo.split('/')
    endpoint=f'repos/{quote(owner)}/{quote(name)}/issues'
    for issue in gh.pages(endpoint,query={'state':'all','per_page':100}):
        if not issue.get('pull_request'):
            yield issue


def _find_child(existing,parent,kind,key):
    for issue in existing:
        match=parse_child(issue)
        if match and match['parent']==parent and match['kind']==kind and match['key']==key:
            return issue
    return None


def _body_for(parent,kind,key,feature,details,dep_keys=(),source_type=None):
    deps=','.join(dep_keys)
    source=f' source={source_type}' if source_type in ('development','bug','feature') else ''
    marker=f'<!-- factory:plan-parent={parent} kind={kind} key={key} deps={deps}{source} -->'
    parts=[f'Erstellt aus Planungs-Issue #{parent}.',
           f'{"Fixbereich" if source_type=="bug" else "Entwicklungsbereich" if source_type=="development" else "Feature"}: {feature}',
           '',details['description']]
    if kind=='task':
        parts+=['','### Akzeptanzkriterien']+[f'- [ ] {c}' for c in details['acceptance_criteria']]
        if dep_keys:
            parts+=['','### Voraussetzungen (Task-Schluessel)']+[f'- `{key}`' for key in dep_keys]
    parts+=['',marker]
    return '\n'.join(parts)


def _link_child(gh, endpoint, parent_number, child):
    # GitHub-native parent -> sub-issue relationship; safe on retries.
    # The same markdown marker independently records lineage in issue bodies.
    path=f'{endpoint}/{parent_number}/sub_issues'
    if not isinstance(child.get('id'),int):
        raise RuntimeError('GitHub Issue REST response missing numeric id for sub-issue link')
    for existing in gh.pages(path,query={'per_page':100}):
        if existing.get('id')==child['id']:
            return False
    linked=gh.request('POST',path,{'sub_issue_id':child['id']})
    if not linked or linked.get('id')!=child['id']:
        raise RuntimeError('Failed to link GitHub sub-issue')
    return True


def publish_plan(gh, repo, parent, plan, board, *, work_type=None):
    """Idempotently publish feature and task Issues in GitHub; preserve existing."""
    from urllib.parse import quote
    plan=validate_plan(plan)
    if work_type not in (None,'development','bug','feature'):
        raise ValueError('Invalid planning issue type')
    child_labels=([f'factory:{work_type}','factory:planned'] if work_type else [])
    endpoint='repos/'+'/'.join(quote(x,safe='') for x in repo.split('/'))+'/issues'
    existing=list(_all_repo_issues(gh,repo))
    feature_numbers={};task_numbers={};new_numbers=[];feature_issues={}
    for f in plan['features']:
        found=_find_child(existing,parent,'feature',f['key'])
        if found is None:
            found=gh.request('POST',endpoint,{
                'title':f'[{"Fixbereich" if work_type=="bug" else "Entwicklungsbereich" if work_type=="development" else "Feature"}] {f["title"]}',
                'body':_body_for(parent,'feature',f['key'],f['title'],f,source_type=work_type),
                **({'labels':child_labels} if child_labels else {})})
            existing.append(found);new_numbers.append(found['number'])
        feature_numbers[f['key']]=found['number']
        feature_issues[f['key']]=found
        _link_child(gh,endpoint,parent,found)
        # Newly created Issue joins its repo-specific Board in Backlog on next scan.
    for f in plan['features']:
        for t in f['tasks']:
            found=_find_child(existing,parent,'task',t['key'])
            if found is None:
                found=gh.request('POST',endpoint,{
                    'title':f'[Fix] {t["title"]}' if work_type=='bug' else f'[Entwicklung] {t["title"]}' if work_type=='development' else f'[Task] {t["title"]}',
                    'body':_body_for(parent,'task',t['key'],f'#{feature_numbers[f["key"]]} {f["title"]}',t,t['depends_on'],source_type=work_type),
                    **({'labels':child_labels} if child_labels else {})})
                existing.append(found);new_numbers.append(found['number'])
            task_numbers[t['key']]=found['number']
            _link_child(gh,endpoint,feature_numbers[f['key']],found)
    return {'features':feature_numbers,'tasks':task_numbers,'created_numbers':new_numbers}


def release_tasks(gh,board):
    """Promote unblocked Backlog task cards after their plan is Done.

    This is GitHub-only state orchestration; no AI worker is spawned.
    """
    cards=board.all_cards()
    columns=board.board['columns']
    by_number={x['issue']['number']:x for x in cards}
    task_by_key={}
    tasks=[]
    for card in cards:
        marker=parse_child(card['issue'])
        if marker and marker['kind']=='task':
            task_by_key[(marker['parent'],marker['key'])]=card
            tasks.append((card,marker))
    released=[]
    for card,marker in tasks:
        if card['status']!=columns['backlog'] or card['issue'].get('state')!='open':
            continue
        plan_card=by_number.get(marker['parent'])
        if not plan_card or plan_card['status']!=columns['done']:
            continue
        deps=[task_by_key.get((marker['parent'],key)) for key in marker['depends_on']]
        if any(x is None or x['status']!=columns['done'] for x in deps):
            continue
        board.set_status(card['item_id'],'ready')
        released.append(card['issue']['number'])
    # Features are grouping Issues. Complete them when every belonging task is
    # Done, so the Kanban board does not accumulate endless Backlog features.
    for feature_card in cards:
        marker=parse_child(feature_card['issue'])
        if not marker or marker['kind']!='feature' or feature_card['status']==columns['done']:
            continue
        number=feature_card['issue']['number']
        feature_tasks=[]
        for child, child_marker in tasks:
            if child_marker['parent']!=marker['parent']:
                continue
            if re.search(r'^(?:Feature|Fixbereich|Entwicklungsbereich): #' + str(number) + r'\b',
                         str(child['issue'].get('body') or ''), re.MULTILINE):
                feature_tasks.append(child)
        if feature_tasks and all(item['status']==columns['done'] for item in feature_tasks):
            board.set_status(feature_card['item_id'],'done')
            if feature_card['issue'].get('state')=='open':
                from urllib.parse import quote
                path='repos/'+'/'.join(quote(x,safe='') for x in board.config['repository'].split('/'))
                gh.request('PATCH',f'{path}/issues/{number}',{'state':'closed','state_reason':'completed'})
    return released


def verify_native_child(gh, repo, issue, child):
    """Reject forged `factory:plan-parent` comments without GitHub sub-Issue links.

    Only legitimate generated sub-Issues may bypass the mandatory planning
    phase. A source marker in arbitrary user-controlled text is NOT proof.
    """
    if not child or child.get('kind') not in ('feature','task'):
        return False
    issue_id=issue.get('id')
    if isinstance(issue_id,bool) or not isinstance(issue_id,int):
        return False
    from urllib.parse import quote
    endpoint='repos/'+'/'.join(quote(x,safe='') for x in repo.split('/'))+'/issues'
    parent=child['parent']
    if child['kind']=='feature':
        return any(x.get('id')==issue_id for x in gh.pages(
            f'{endpoint}/{parent}/sub_issues',query={'per_page':100}))
    # Tasks are sub-Issues of feature grouping Issues. Those in turn must be
    # genuine native sub-Issues of the planning parent.
    for feature in gh.pages(f'{endpoint}/{parent}/sub_issues',query={'per_page':100}):
        marker=parse_child(feature)
        if not marker or marker['kind']!='feature' or marker['parent']!=parent:
            continue
        if any(x.get('id')==issue_id for x in gh.pages(
                f'{endpoint}/{feature["number"]}/sub_issues',query={'per_page':100})):
            return True
    return False
