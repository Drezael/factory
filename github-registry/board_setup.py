"""Idempotent GitHub Projects v2 bootstrap used by n8n via registry adapter.

No AI worker is started. The GitHub write credential remains in the internal
adapter and is never passed to n8n Code nodes or to AI containers.
"""
from __future__ import annotations

import logging

LOG = logging.getLogger('factory.github-registry.board_setup')
MAX_PROJECT_PAGES = 15
STATUS_COLOR = {
    'development_intake': 'PURPLE', 'bug_intake': 'RED', 'feature_intake': 'BLUE',
    'backlog': 'GRAY', 'planning': 'PURPLE', 'ready': 'BLUE',
    'in_progress': 'YELLOW', 'waiting_for_answer': 'ORANGE',
    'review': 'PURPLE', 'done': 'GREEN', 'failed': 'RED',
}
STATUS_DESCRIPTION = {
    'development_intake': 'Neue Entwicklung oder Aenderung an laufender Entwicklung; hoechste Prioritaet',
    'bug_intake': 'Neu gemeldeter Bug; nach Entwicklungsauftraegen und vor Features priorisiert',
    'feature_intake': 'Neuer Feature Request; wartet auf Planungsfreigabe',
    'backlog': 'Noch nicht freigegeben',
    'planning': 'Planung laeuft',
    'ready': 'Freigegeben fuer den naechsten frischen Worker',
    'in_progress': 'Entwicklung oder Umsetzung',
    'waiting_for_answer': 'Rueckfrage im urspruenglichen Issue',
    'review': 'Unabhaengiger Test und Review',
    'done': 'Abgeschlossen',
    'failed': 'Fehler oder manuelle Pruefung notwendig',
}


def _owner_query(board, *, number=None, cursor=None):
    owner = 'organization' if board['owner_type'] == 'organization' else 'user'
    if number is not None:
        q = f'''query($login:String!, $number:Int!) {{
          {owner}(login:$login) {{ id projectV2(number:$number) {{ id number title url }} }}
        }}'''
        return q, {'login': board['owner'], 'number': number}
    q = f'''query($login:String!, $cursor:String) {{
      {owner}(login:$login) {{ id projectsV2(first:100,after:$cursor) {{
        nodes {{ id number title url }} pageInfo {{ hasNextPage endCursor }}
      }} }}
    }}'''
    return q, {'login':board['owner'], 'cursor':cursor}


def find_project(gh, board):
    """Return existing project or None; never create or mutate."""
    q, vars = _owner_query(board, number=board.get('project_number')) if board.get('project_number') else _owner_query(board)
    owner_key = 'organization' if board['owner_type'] == 'organization' else 'user'
    if board.get('project_number'):
        payload = gh.graphql(q, vars).get(owner_key)
        if payload is None:
            raise RuntimeError(f"GitHub Projects owner {board['owner']} not found or inaccessible")
        return payload.get('projectV2')
    cursor = None
    found = []
    seen = set()
    for _ in range(MAX_PROJECT_PAGES):
        q, vars = _owner_query(board, cursor=cursor)
        payload = gh.graphql(q, vars).get(owner_key)
        if payload is None:
            raise RuntimeError(f"GitHub Projects owner {board['owner']} not found or inaccessible")
        collection = payload.get('projectsV2') or {}
        found.extend(p for p in collection.get('nodes') or [] if p and p.get('title') == board['project_title'])
        page = collection.get('pageInfo') or {}
        if not page.get('hasNextPage'):
            break
        cursor = page.get('endCursor')
        if not cursor or cursor in seen:
            raise RuntimeError('GitHub Projects pagination returned no new cursor')
        seen.add(cursor)
    else:
        raise RuntimeError('GitHub Projects list exceeds Factory safety limit; set a project_number')
    if len(found) > 1:
        raise RuntimeError('Multiple Projects have the exact configured title; use explicit project_number')
    return found[0] if found else None


def _repo_node(gh, slug):
    owner, name = slug.split('/')
    q = '''query($owner:String!, $name:String!) {
      repository(owner:$owner,name:$name) { id nameWithOwner }
    }'''
    repo = gh.graphql(q, {'owner':owner, 'name':name}).get('repository')
    if not repo or not repo.get('id') or repo.get('nameWithOwner','').lower() != slug.lower():
        raise RuntimeError(f'GitHub repository not accessible: {slug}')
    return repo['id']


def _linked_repo_ids(gh, project_id):
    query = '''query($id:ID!, $cursor:String) { node(id:$id) {
      ... on ProjectV2 { repositories(first:100,after:$cursor) {
        nodes { id } pageInfo { hasNextPage endCursor }
      } }
    } }'''
    cursor = None
    seen = set()
    ids = set()
    for _ in range(10):
        payload = gh.graphql(query, {'id':project_id,'cursor':cursor}).get('node') or {}
        collection = payload.get('repositories') or {}
        ids.update(x['id'] for x in collection.get('nodes') or [] if x and x.get('id'))
        page = collection.get('pageInfo') or {}
        if not page.get('hasNextPage'):
            return ids
        cursor = page.get('endCursor')
        if not cursor or cursor in seen:
            raise RuntimeError('GitHub linked repositories pagination failed')
        seen.add(cursor)
    raise RuntimeError('Too many repositories linked to project; manual review needed')


def _status_options(gh, project_id, name):
    q = '''query($id:ID!, $name:String!) { node(id:$id) {
      ... on ProjectV2 { field(name:$name) {
        __typename
        ... on ProjectV2SingleSelectField { id options { id name color description } }
      } }
    } }'''
    data = gh.graphql(q, {'id':project_id,'name':name}).get('node')
    if not data:
        raise RuntimeError('Project disappeared while reading Status field')
    return data.get('field')


def _option(name, key, existing=None):
    ret={'name':name, 'color':STATUS_COLOR.get(key,'GRAY'),
         'description':STATUS_DESCRIPTION.get(key, 'Factory Status')}
    if existing:
        ret['id']=existing['id']
        ret['color']=existing.get('color') or ret['color']
        ret['description']=existing.get('description') or ret['description']
    return ret


def _ensure_status_field(gh, board, project_id):
    status_field = _status_options(gh, project_id, board['status_field'])
    configured = list(board['columns'].items())
    if not status_field:
        mutation = '''mutation($id:ID!,$name:String!,$options:[ProjectV2SingleSelectFieldOptionInput!]!) {
          createProjectV2Field(input:{projectId:$id,name:$name,dataType:SINGLE_SELECT,singleSelectOptions:$options}) {
            projectV2Field { ... on ProjectV2SingleSelectField { id } }
          }
        }'''
        data = gh.graphql(mutation, {
            'id':project_id,'name':board['status_field'],
            'options':[_option(name,key) for key,name in configured]
        })
        if not ((data.get('createProjectV2Field') or {}).get('projectV2Field') or {}).get('id'):
            raise RuntimeError('Could not create Status field')
        return configured and [x[1] for x in configured] or []
    if status_field.get('__typename')!='ProjectV2SingleSelectField':
        raise RuntimeError('Configured Status field exists but is not a single-select field')
    existing = status_field.get('options') or []
    existing_by_name = {x['name']:x for x in existing if x.get('name') and x.get('id')}
    missing = [(key,name) for key,name in configured if name not in existing_by_name]
    if not missing:
        return []
    # updateProjectV2Field REPLACES the full option list. Preserve ALL existing
    # identities/colors/descriptions, or existing board assignments would clear.
    options = [
        {'id':x['id'], 'name':x['name'], 'color':x.get('color') or 'GRAY',
         'description':x.get('description') or ''}
        for x in existing
    ]
    options.extend(_option(name,key) for key,name in missing)
    mutation = '''mutation($field:ID!,$options:[ProjectV2SingleSelectFieldOptionInput!]!) {
      updateProjectV2Field(input:{fieldId:$field,singleSelectOptions:$options}) {
        projectV2Field { ... on ProjectV2SingleSelectField { id } }
      }
    }'''
    data=gh.graphql(mutation,{'field':status_field['id'],'options':options})
    if not ((data.get('updateProjectV2Field') or {}).get('projectV2Field') or {}).get('id'):
        raise RuntimeError('Could not extend Status options')
    return [name for _,name in missing]


def _ensure_view(gh, project_id, desired_name):
    q = '''query($id:ID!) { node(id:$id) {
      ... on ProjectV2 { views(first:100) { nodes { id name layout } pageInfo { hasNextPage } } }
    } }'''
    payload=gh.graphql(q,{'id':project_id}).get('node') or {}
    collection = payload.get('views') or {}
    if (collection.get('pageInfo') or {}).get('hasNextPage'):
        raise RuntimeError('Too many project views; refusing to guess which view to use')
    views=collection.get('nodes') or []
    for item in views:
        if item and item.get('name') == desired_name:
            if item.get('layout')!='BOARD_LAYOUT':
                raise RuntimeError(f'Project view {desired_name} exists but is not a Kanban Board')
            return False
    q='''mutation($id:ID!,$name:String!) {
      createProjectV2View(input:{projectId:$id,name:$name,layout:BOARD_LAYOUT}) {
        projectV2View { id name layout }
      }
    }'''
    value=(gh.graphql(q,{'id':project_id,'name':desired_name}).get('createProjectV2View') or {}).get('projectV2View')
    if not value or value.get('layout') != 'BOARD_LAYOUT':
        raise RuntimeError('Could not create a Kanban view')
    return True


def ensure_board(gh, config):
    """Provision board + all configured categories; idempotent for existing boards."""
    board=config['kanban']
    if not board.get('enabled'):
        return {'repository':config['repository'],'status':'disabled'}
    project=find_project(gh,board)
    created=False
    repo_id=_repo_node(gh,config['repository'])
    if project is None:
        if board.get('project_number'):
            raise RuntimeError('Configured project_number not found; remove it to enable automatic creation')
        if not board.get('auto_create'):
            raise RuntimeError('Kanban project absent; set kanban.auto_create: true')
        owner_key='organization' if board['owner_type']=='organization' else 'user'
        owner_query=f'query($login:String!) {{ {owner_key}(login:$login) {{ id }} }}'
        owner=gh.graphql(owner_query,{'login':board['owner']}).get(owner_key)
        if not owner or not owner.get('id'):
            raise RuntimeError('Project owner not accessible for board creation')
        mutation='''mutation($owner:ID!,$repository:ID!,$title:String!) {
          createProjectV2(input:{ownerId:$owner,repositoryId:$repository,title:$title}) {
            projectV2 { id number title url }
          }
        }'''
        project=(gh.graphql(mutation,{'owner':owner['id'],'repository':repo_id,
                                    'title':board['project_title']}).get('createProjectV2') or {}).get('projectV2')
        if not project or not project.get('id') or not project.get('number'):
            raise RuntimeError('GitHub project create mutation returned no project ID/number')
        created=True
        # Creation normally defaults to private; enforce private explicitly.
        gh.graphql('''mutation($id:ID!) {
          updateProjectV2(input:{projectId:$id,public:false}) { projectV2 { id } }
        }''', {'id':project['id']})
    if not created and repo_id not in _linked_repo_ids(gh,project['id']):
        q='''mutation($project:ID!,$repository:ID!) {
          linkProjectV2ToRepository(input:{projectId:$project,repositoryId:$repository}) {
            repository { id }
          }
        }'''
        link=(gh.graphql(q,{'project':project['id'],'repository':repo_id}).get('linkProjectV2ToRepository') or {}).get('repository')
        if not link or link.get('id')!=repo_id:
            raise RuntimeError('Could not link board to repository')
    added=_ensure_status_field(gh,board,project['id'])
    view_created=_ensure_view(gh,project['id'],board['view_name'])
    # Verify by name instead of trusting GitHub mutation return only.
    refreshed=_status_options(gh,project['id'],board['status_field']) or {}
    found={x.get('name') for x in refreshed.get('options') or []}
    missing={name for name in board['columns'].values() if name not in found}
    if missing:
        raise RuntimeError(f'Status columns still missing after provisioning: {sorted(missing)}')
    return {'repository':config['repository'],'status':'created' if created else 'ready',
            'project_number':project['number'],'project_url':project.get('url'),
            'added_categories':added,'created_board_view':view_created,
            'categories':list(board['columns'].values())}
