"""Minimal, idempotent bootstrap of an EMPTY PRIVATE GitHub registry.

The registry is an explicit allowlist: only user-created repos/*.repo files with
one HTTPS URL are read. Bootstrap NEVER discovers or adds other repositories.
"""
from __future__ import annotations

import base64
import os
from urllib.parse import quote

import yaml

COLUMNS = {
    'development_intake': 'Entwicklung Eingang',
    'bug_intake': 'Bug Eingang',
    'feature_intake': 'Feature Eingang',
    'backlog': 'Backlog',
    'planning': 'Planung',
    'ready': 'Bereit',
    'in_progress': 'In Arbeit',
    'waiting_for_answer': 'Rueckfrage',
    'review': 'Review',
    'done': 'Erledigt',
    'failed': 'Fehler',
}
README = """# Factory Repository Registry

Die Repo-Registry ist eine explizite Allowlist. Sie wird von n8n ausgelesen.

## Repository aufnehmen

In `repos/` eine neue Datei mit der Endung `.repo` erstellen, z. B.
`repos/backend.repo`. **Ihr vollstaendiger Inhalt besteht aus einer einzigen Zeile:**

    https://github.com/DEINE-ORG/backend

Nach dem naechsten n8n-Scan (~2 Minuten) wird das Repository ueberwacht.
Die Factory erstellt Kanban-Board, Statuskategorien und Issue-Vorlagen
mit ihren zentralen Standardregeln automatisch.

## Repository entfernen

Die entsprechende `.repo`-Datei loeschen und committen. Beim naechsten
Scan werden keine weiteren Auftraege fuer dieses Repo gestartet. GitHub-Issues,
Pull Requests, Kanban-Boards und bestehende Codes bleiben unangetastet.
Bereits laufende Container werden nicht mit Gewalt abgebrochen.

Es werden NIEMALS weitere Repositories automatisch entdeckt oder registriert.
Keine Zugangsdaten in diesem Repository ablegen!
"""


def _env_bool(key, fallback):
    text = os.environ.get(key, 'true' if fallback else 'false').lower().strip()
    if text not in ('true', 'false'):
        raise ValueError(f'{key} must be true or false')
    return text == 'true'


def _env_int(key, fallback):
    try:
        return int(os.environ.get(key, str(fallback)).strip())
    except ValueError as exc:
        raise ValueError(f'{key} must be an integer') from exc


def config_for(repo, owner_type):
    """Create an in-memory default config from trusted GitHub repo metadata.

    This config is NOT committed to the registry. Every tracked repo has exactly
    one URL-only .repo file; settings come from centrally versioned defaults.
    """
    slug = repo['full_name']
    owner = repo['owner']['login']
    base_branch = repo.get('default_branch') or 'main'
    planner = os.environ.get('FACTORY_GITHUB_DEFAULT_PLANNER_PROVIDER', 'codex').strip()
    developer = os.environ.get('FACTORY_GITHUB_DEFAULT_DEVELOPER_PROVIDER', 'codex').strip()
    reviewer = os.environ.get('FACTORY_GITHUB_DEFAULT_REVIEWER_PROVIDER', 'claude').strip()
    checks = [x.strip() for x in os.environ.get('FACTORY_GITHUB_DEFAULT_REQUIRED_CHECKS', '').split(',') if x.strip()]
    config = {
        'version': 1, 'repository': slug, 'enabled': True,
        'issues': {'enabled': True, 'state': 'open', 'initial_sync': 'existing',
                   'required_labels': [], 'ignored_labels': ['factory:hold']},
        'agent': {'provider': 'codex', 'role': 'developer',
                  'auto_execute': True, 'timeout_seconds': _env_int('FACTORY_GITHUB_DEFAULT_TIMEOUT_SEC', 900)},
        'kanban': {'enabled': True, 'owner_type': owner_type, 'owner': owner,
                   'auto_create': True, 'project_title': f'Factory - {slug}',
                   'view_name': 'Factory Kanban', 'status_field': 'Status',
                   'columns': dict(COLUMNS)},
        'pipeline': {'enabled': True, 'base_branch': base_branch,
                     'developer_provider': developer, 'reviewer_provider': reviewer,
                     'max_review_cycles': _env_int('FACTORY_GITHUB_DEFAULT_MAX_REVIEW_CYCLES', 3), 'required_checks': checks,
                     'auto_merge': _env_bool('FACTORY_GITHUB_DEFAULT_AUTO_MERGE', False)},
        'planning': {'enabled': True, 'provider': planner,
                     'max_tasks': _env_int('FACTORY_GITHUB_DEFAULT_MAX_PLAN_TASKS', 40)},
        'intake': {'enabled': True,
                   'auto_install_forms': _env_bool('FACTORY_GITHUB_DEFAULT_AUTO_INSTALL_FORMS', True),
                   'auto_plan_on_create': _env_bool('FACTORY_GITHUB_DEFAULT_AUTO_PLAN_ON_CREATE', True)},
    }
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def is_missing(exc):
    return getattr(exc, 'status_code', None) == 404


def _exists(gh, endpoint, branch):
    try:
        gh.get(endpoint, query={'ref': branch})
        return True
    except Exception as exc:
        if is_missing(exc):
            return False
        raise


def _write_new(gh, endpoint, content, *, branch=None):
    payload = {'message': 'factory: initialize repository registry',
               'content': base64.b64encode(content.encode('utf-8')).decode('ascii')}
    # Without `branch`, GitHub can create the very first commit in an empty repo.
    if branch is not None:
        payload['branch'] = branch
    return gh.request('PUT', endpoint, payload)


def bootstrap(gh, slug, ref, directory, *, validate_slug):
    slug = validate_slug(slug)
    owner, name = slug.split('/')
    prefix = 'repos/' + quote(owner, safe='') + '/' + quote(name, safe='')
    meta, _ = gh.get(prefix)
    if meta.get('full_name', '').lower() != slug.lower():
        raise ValueError('Registry repository identity mismatch')
    if not meta.get('private'):
        raise ValueError('Registry repository must be PRIVATE')
    branch = meta.get('default_branch') or 'main'
    if ref not in ('', 'auto', branch):
        raise ValueError(f'Registry branch is {branch!r}; use FACTORY_GITHUB_REGISTRY_REF=auto')
    if not directory or directory.startswith('/') or '..' in directory.split('/'):
        raise ValueError('Invalid registry directory')
    readme_endpoint = prefix + '/contents/README.md'
    created_readme = False
    if not _exists(gh, readme_endpoint, branch):
        _write_new(gh, readme_endpoint, README)
        created_readme = True
    directory_endpoint = prefix + '/contents/' + quote(directory.strip('/'), safe='/')
    try:
        entries, _ = gh.get(directory_endpoint, query={'ref': branch})
    except Exception as exc:
        if not is_missing(exc):
            raise
        entries = []
    if not isinstance(entries, list):
        raise ValueError('Registry directory must be a directory')
    placeholder_created = False
    if not entries:
        endpoint = directory_endpoint + '/.gitkeep'
        if not _exists(gh, endpoint, branch):
            _write_new(gh, endpoint, '', branch=branch)
            placeholder_created = True
    return {'registry': slug, 'branch': branch, 'private': True,
            'readme_created': created_readme,
            'placeholder_created': placeholder_created,
            'registered_files': sum(item.get('name', '').endswith('.repo') for item in entries),
            'automatic_discovery': False}
