"""Typed GitHub Issue intake: Development > Bug Reports > Features.

No AI worker is needed for issue forms, labels or board intake. GitHub Issue
forms are installed only when enabled in a repository registry YAML file.
Existing templates are never overwritten by the installer.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path
from urllib.parse import quote

DEVELOPMENT_LABEL = 'factory:development'
BUG_LABEL = 'factory:bug'
FEATURE_LABEL = 'factory:feature'
GENERATED_LABEL = 'factory:planned'
LABELS = {
    DEVELOPMENT_LABEL: ('5319e7', 'Development and changes to ongoing development; top priority; planning required'),
    BUG_LABEL: ('d73a4a', 'Bug Report; plan before fix; priority below Development, above Features'),
    FEATURE_LABEL: ('1d76db', 'Feature Request; first plan, then implement'),
    GENERATED_LABEL: ('6f42c1', 'Generated from a completed Factory planning issue'),
}
FORMS = ('00-factory-development.yml', '01-factory-bug.yml', '02-factory-feature.yml')


def issue_type(issue, child=None):
    """Return development/bug/feature or None for missing, conflicting, or QA-only labels."""
    labels = {x.get('name','') for x in issue.get('labels',[]) if isinstance(x,dict)}
    if 'factory:review-defect' in labels:
        return None
    kinds = ({'development' for x in labels if x == DEVELOPMENT_LABEL}
             | {'bug' for x in labels if x == BUG_LABEL}
             | {'feature' for x in labels if x == FEATURE_LABEL})
    source = (child or {}).get('source_type')
    if source in ('development','bug','feature'):
        kinds.add(source)
    return next(iter(kinds)) if len(kinds)==1 else None


def ensure_labels(gh, repository):
    endpoint = 'repos/' + '/'.join(quote(x, safe='') for x in repository.split('/'))
    created = []
    for name,(color,description) in LABELS.items():
        path = f'{endpoint}/labels/{quote(name,safe="")}'
        try:
            gh.get(path)
        except Exception as exc:
            if '404' not in str(exc):
                raise
            gh.request('POST',f'{endpoint}/labels',{
                'name':name,'color':color,'description':description})
            created.append(name)
    return created


def ensure_forms(gh, config):
    """Install missing templates on the repository default branch only."""
    if not config.get('intake',{}).get('auto_install_forms',False):
        return []
    repo=config['repository']
    endpoint='repos/' + '/'.join(quote(x,safe='') for x in repo.split('/'))
    repo_info=gh.get(endpoint)[0]
    branch=repo_info.get('default_branch')
    if not isinstance(branch,str) or not 1<=len(branch)<=180 or '..' in branch:
        raise RuntimeError('Unable to determine GitHub default branch for Issue forms')
    created=[]
    for filename in FORMS:
        path=f'.github/ISSUE_TEMPLATE/{filename}'
        uri=f'{endpoint}/contents/{quote(path,safe="/")}'
        try:
            response=gh.get(uri,query={'ref':branch})[0]
            if response.get('type')!='file':
                raise RuntimeError(f'Issue form path is not a regular file: {path}')
            # User changes have precedence. Never overwrite existing forms.
            continue
        except Exception as exc:
            if '404' not in str(exc):
                raise
        source=(Path(__file__).resolve().parent/'forms'/filename).read_bytes()
        gh.request('PUT',uri,{
            'message':f'factory: install {filename} issue form',
            'content':base64.b64encode(source).decode('ascii'),
            'branch':branch})
        created.append(path)
    return created


def ensure_intake(gh, config):
    if not config.get('intake',{}).get('enabled',False):
        return {'enabled':False}
    labels=ensure_labels(gh,config['repository'])
    files=ensure_forms(gh,config)
    return {'enabled':True,'created_labels':labels,'created_forms':files}
