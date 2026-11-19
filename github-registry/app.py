"""Factory GitHub registry and stateless issue lifecycle.

n8n owns the schedule and all future agent dispatching. This internal service
reads one URL-only .repo file per monitored repository from a private GitHub repo,
collects GitHub Issues and persists a deduplicated inbox in PostgreSQL.
GitHub comments and Projects updates occur only for allowlisted configured Issues.
Worker execution is delegated to the internal Agent Manager on n8n workflow demand.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lifecycle
import pipeline
import board_setup
import planner
import intake
import bootstrap
import registry_files
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlencode, urljoin
from urllib.request import Request, urlopen

import yaml

LOG = logging.getLogger("factory.github-registry")
REPO_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
SAFE_DIR_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-/]+$")
MAX_YAML = 50_000
MAX_REPO_FILES = 250
MAX_ISSUE_PAGES = 50
MAX_EVENTS_IN_SCAN = 100
PROVIDERS = ("codex", "claude", "gemini")
ROLES = ("planner", "requirements", "architect", "developer", "tester", "reviewer", "security")
SCAN_LOCK = threading.Lock()


class RegistryError(Exception):
    pass


class GitHubStatusError(RegistryError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def now_utc():
    return datetime.now(timezone.utc)


def iso_dt(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RegistryError("GitHub updated_at missing or invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise RegistryError("Invalid GitHub updated_at") from exc


def _validate_repo_slug(value):
    if not isinstance(value, str) or not REPO_PATTERN.fullmatch(value):
        raise RegistryError("repository must be owner/repo (letters, digits, _, -, .)")
    if '..' in value or value.startswith('.') or value.endswith('.'):
        raise RegistryError("repository contains invalid segments")
    return value


def _labels(value, field, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise RegistryError(f"{field} must be a {'non-empty ' if nonempty else ''}list")
    if len(value) > 30 or any(not isinstance(x, str) or not x.strip() or len(x) > 80 for x in value):
        raise RegistryError(f"{field} contains invalid labels")
    return list(dict.fromkeys(value))


def parse_config(contents, path):
    if len(contents.encode('utf-8')) > MAX_YAML:
        raise RegistryError(f"{path}: YAML exceeds {MAX_YAML} bytes")
    try:
        record = yaml.safe_load(contents)
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(record, dict):
        raise RegistryError(f"{path}: YAML root must be a mapping")
    unknown = set(record) - {'version', 'repository', 'enabled', 'issues', 'agent', 'kanban', 'pipeline', 'planning', 'intake'}
    if unknown:
        raise RegistryError(f"{path}: unknown root fields: {sorted(unknown)}")
    if record.get('version') != 1 or isinstance(record.get('version'), bool):
        raise RegistryError(f"{path}: version must be 1")
    repo = _validate_repo_slug(record.get('repository'))
    enabled = record.get('enabled', True)
    if not isinstance(enabled, bool):
        raise RegistryError(f"{path}: enabled must be boolean")
    issue = record.get('issues', {})
    if not isinstance(issue, dict):
        raise RegistryError(f"{path}: issues must be a mapping")
    extra = set(issue) - {'enabled', 'state', 'required_labels', 'ignored_labels', 'initial_sync'}
    if extra:
        raise RegistryError(f"{path}: unknown issue fields: {sorted(extra)}")
    issue_enabled = issue.get('enabled', True)
    if not isinstance(issue_enabled, bool):
        raise RegistryError(f"{path}: issues.enabled must be boolean")
    state = issue.get('state', 'open')
    if state not in ('open', 'closed', 'all'):
        raise RegistryError(f"{path}: issues.state must be open, closed, or all")
    initial = issue.get('initial_sync', 'existing')
    if initial not in ('existing', 'new_only'):
        raise RegistryError(f"{path}: issues.initial_sync must be existing or new_only")
    status_board = bool((record.get('kanban') or {}).get('enabled', False))
    required = _labels(issue.get('required_labels', [] if status_board else ['factory:ready']), f"{path}: issues.required_labels", nonempty=not status_board)
    if status_board and required:
        raise RegistryError(f'{path}: status-driven Kanban cannot use required_labels; use []')
    ignored = _labels(issue.get('ignored_labels', ['factory:hold']), f"{path}: issues.ignored_labels")
    agent = record.get('agent', {})
    if not isinstance(agent, dict):
        raise RegistryError(f"{path}: agent must be a mapping")
    extra = set(agent) - {'provider', 'role', 'auto_execute', 'timeout_seconds'}
    if extra:
        raise RegistryError(f"{path}: unknown agent fields: {sorted(extra)}")
    provider = agent.get('provider', 'codex')
    role = agent.get('role', 'requirements')
    if provider not in PROVIDERS or role not in ROLES:
        raise RegistryError(f"{path}: unknown provider/role")
    auto_execute = agent.get('auto_execute', False)
    if not isinstance(auto_execute, bool):
        raise RegistryError(f'{path}: agent.auto_execute must be boolean')
    if auto_execute and not bool((record.get('kanban') or {}).get('enabled', False)):
        raise RegistryError(f'{path}: agent.auto_execute requires kanban.enabled=true and Status-based dispatch')
    if auto_execute and role in ('developer','tester') and not (record.get('pipeline') or {}).get('enabled', False):
        raise RegistryError(f'{path}: automated developer/tester needs GitHub PR persistence; use requirements/architect/reviewer/security until added')
    timeout = agent.get('timeout_seconds', 900)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not (10 <= timeout <= 1800):
        raise RegistryError(f'{path}: agent.timeout_seconds must be 10..1800')
    board = record.get('kanban', {})
    if not isinstance(board, dict):
        raise RegistryError(f'{path}: kanban must be mapping')
    extra = set(board) - {'enabled','owner_type','owner','project_number','project_title','auto_create','view_name','status_field','columns'}
    if extra:
        raise RegistryError(f'{path}: unexpected kanban keys: {sorted(extra)}')
    board_enabled = board.get('enabled', False)
    if not isinstance(board_enabled, bool):
        raise RegistryError(f'{path}: kanban.enabled must be boolean')
    if board_enabled:
        owner_type = board.get('owner_type', 'organization')
        owner = board.get('owner', repo.split('/')[0])
        project_number = board.get('project_number')
        auto_create = board.get('auto_create', project_number is None)
        title = board.get('project_title', f'Factory - {repo}')
        view = board.get('view_name', 'Factory Kanban')
        field = board.get('status_field', 'Status')
        columns = board.get('columns')
        if (owner_type not in ('organization','user') or
            not isinstance(owner,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,39}',owner) or
            (project_number is not None and (isinstance(project_number,bool) or not isinstance(project_number,int) or not 1 <= project_number <= 100000)) or
            not isinstance(auto_create,bool) or
            not isinstance(title,str) or not 5 <= len(title) <= 120 or
            not isinstance(view,str) or not 1 <= len(view) <= 80 or
            not isinstance(field,str) or not 1 <= len(field) <= 80):
            raise RegistryError(f'{path}: invalid Kanban settings')
        statuses = {'backlog','ready','in_progress','waiting_for_answer','review','done','failed'}
        if (not isinstance(columns,dict) or not statuses.issubset(columns) or
            set(columns)-statuses-{'planning','development_intake','bug_intake','feature_intake'} or
            any(not isinstance(columns[k],str) or not 1 <= len(columns[k]) <= 80 for k in columns) or
            len(set(columns.values()))!=len(columns)):
            raise RegistryError(f'{path}: kanban.columns requires {sorted(statuses)}, optional planning, distinct values')
        board = {'enabled':True, 'owner_type':owner_type,'owner':owner,
                 'project_number':project_number,'auto_create':auto_create,
                 'project_title':title,'view_name':view,
                 'status_field':field,'columns':columns}
    else:
        board = {'enabled':False}
    config = {
        'version': 1, 'repository': repo, 'enabled': enabled,
        'issues': {'enabled': issue_enabled, 'state': state,
                   'required_labels': required, 'ignored_labels': ignored,
                   'initial_sync': initial},
        'agent': {'provider': provider, 'role': role, 'auto_execute': auto_execute, 'timeout_seconds': timeout},
        'kanban': board,
        'registry_path': path,
    }
    try:
        config['pipeline'] = pipeline.parse_pipeline(record.get('pipeline'), path, enabled=enabled)
    except ValueError as exc:
        raise RegistryError(str(exc)) from exc
    planning = record.get('planning', {})
    if not isinstance(planning,dict) or set(planning)-{'enabled','provider','max_tasks'}:
        raise RegistryError(f'{path}: invalid planning section')
    planning_enabled = planning.get('enabled', False)
    if not isinstance(planning_enabled,bool):
        raise RegistryError(f'{path}: planning.enabled must be boolean')
    planner_provider = planning.get('provider',provider)
    if planner_provider not in PROVIDERS:
        raise RegistryError(f'{path}: invalid planning.provider')
    max_tasks = planning.get('max_tasks',40)
    if isinstance(max_tasks,bool) or not isinstance(max_tasks,int) or not 1<=max_tasks<=40:
        raise RegistryError(f'{path}: planning.max_tasks must be 1..40')
    if planning_enabled and not config['pipeline'].get('enabled'):
        raise RegistryError(f'{path}: planning.enabled requires pipeline.enabled=true')
    if planning_enabled and not board.get('enabled'):
        raise RegistryError(f'{path}: planning.enabled requires kanban.enabled=true')
    config['planning'] = {'enabled':planning_enabled,'provider':planner_provider,'max_tasks':max_tasks}
    intake_raw=record.get('intake',{})
    if not isinstance(intake_raw,dict) or set(intake_raw)-{'enabled','auto_install_forms','auto_plan_on_create'}:
        raise RegistryError(f'{path}: invalid intake section')
    intake_enabled=intake_raw.get('enabled',False)
    install_forms=intake_raw.get('auto_install_forms',False)
    auto_plan=intake_raw.get('auto_plan_on_create',True)
    if not isinstance(intake_enabled,bool) or not isinstance(install_forms,bool) or not isinstance(auto_plan,bool):
        raise RegistryError(f'{path}: intake fields must be booleans')
    if install_forms and not intake_enabled:
        raise RegistryError(f'{path}: auto_install_forms requires intake.enabled=true')
    if intake_enabled:
        if not planning_enabled or not config['pipeline'].get('enabled') or not board.get('enabled'):
            raise RegistryError(f'{path}: typed intake requires planning, pipeline and kanban enabled')
        if {'development_intake','bug_intake','feature_intake'}-board['columns'].keys():
            raise RegistryError(f'{path}: intake requires kanban.columns.development_intake, bug_intake and feature_intake')
    # Fail closed: no developer work may run directly from a root Feature/Bug
    # without BOTH planning and typed intake enabled.
    if config['pipeline'].get('enabled') and auto_execute and not (planning_enabled and intake_enabled):
        raise RegistryError(f'{path}: pipeline agent.auto_execute requires planning.enabled=true and intake.enabled=true')
    config['intake']={'enabled':intake_enabled,'auto_install_forms':install_forms,'auto_plan_on_create':auto_plan}
    config['config_hash'] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    return config


def eligible(issue, config):
    rules = config['issues']
    if 'pull_request' in issue or issue.get('state') not in (('open','closed') if rules['state'] == 'all' else (rules['state'],)):
        return False
    labels = {label['name'] for label in issue.get('labels', []) if isinstance(label, dict) and isinstance(label.get('name'), str)}
    return all(label in labels for label in rules['required_labels']) and not any(
        label in labels for label in rules['ignored_labels'])


def make_event(issue, config):
    repo = config['repository']
    updated_at = iso_dt(issue['updated_at'])
    number = issue['number']
    if not isinstance(number, int) or number < 1:
        raise RegistryError("Issue number invalid")
    key = hashlib.sha256(f"{repo.lower()}#{number}@{updated_at.isoformat()}".encode()).hexdigest()
    return {
        'event_key': key,
        'event_type': 'github.issue.updated',
        'repository': repo,
        'issue_number': number,
        'issue_url': issue.get('html_url', ''),
        'title': str(issue.get('title', ''))[:500],
        'body': str(issue.get('body') or '')[:16000],
        'state': issue.get('state'),
        'labels': [x.get('name') for x in issue.get('labels', []) if isinstance(x, dict) and isinstance(x.get('name'), str)],
        'updated_at': updated_at.isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'created_at': issue.get('created_at'),
        'agent': config['agent'],
        'kanban': config['kanban'],
        'pipeline': config.get('pipeline', {'enabled':False}),
        'planning': config.get('planning', {'enabled':False}),
        'registry_path': config['registry_path'],
    }


class GitHubClient:
    def __init__(self, token, api_base='https://api.github.com', version='2022-11-28'):
        self.token = token
        parsed = urlsplit(api_base)
        if parsed.scheme != 'https' or parsed.username or parsed.password or not parsed.hostname:
            raise RegistryError('GitHub API URL must be https')
        self.base = api_base.rstrip('/') + '/'
        self.version = version
        self.origin = (parsed.scheme, parsed.netloc)
        self._identity = None

    def get(self, path_or_url, *, query=None):
        url = path_or_url if path_or_url.startswith('https://') else urljoin(self.base, path_or_url.lstrip('/'))
        if query:
            url += ('&' if '?' in url else '?') + urlencode(query)
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != self.origin:
            raise RegistryError('Refusing off-origin GitHub API redirect/pagination')
        req = Request(url, headers={
            'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': self.version, 'User-Agent': 'factory-repo-registry/1.0'
        })
        try:
            with urlopen(req, timeout=20) as response:
                body = response.read(3_000_001)
                if len(body) > 3_000_000:
                    raise RegistryError('GitHub API response too large')
                result = json.loads(body)
                return result, response.headers.get('Link','')
        except HTTPError as exc:
            raise GitHubStatusError(f'GitHub HTTP {exc.code} for {parsed.path} (token/permissions/rate limit prüfen)', exc.code) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RegistryError(f'GitHub API request failed for {parsed.path}: {type(exc).__name__}') from exc

    def pages(self, endpoint, *, query=None):
        next_url = endpoint
        for _ in range(MAX_ISSUE_PAGES):
            entries, links = self.get(next_url, query=query)
            query = None
            if not isinstance(entries, list):
                raise RegistryError('Expected GitHub API array')
            for entry in entries:
                yield entry
            next_url = None
            for part in links.split(','):
                if 'rel="next"' in part:
                    match = re.search(r'<([^>]+)>', part)
                    if match:
                        next_url = match.group(1)
            if not next_url:
                return
        raise RegistryError('GitHub issue pagination limit exceeded; checkpoint NOT advanced')

    def registry(self, slug, ref, directory):
        """Read ONLY user-created *.repo URL files; never auto-discover repos."""
        try:
            return registry_files.parse_allowlist(
                self, slug, ref, directory, parse_config=parse_config,
                validate_slug=_validate_repo_slug, max_files=MAX_REPO_FILES)
        except ValueError as exc:
            raise RegistryError(str(exc)) from exc

    def issues(self, slug, since=None, required_labels=()):
        owner, repo = _validate_repo_slug(slug).split('/')
        q = {'state':'all','sort':'updated','direction':'asc','per_page':100}
        if required_labels:
            q['labels'] = ','.join(required_labels)
        if since:
            q['since'] = since.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        yield from self.pages(f'repos/{quote(owner)}/{quote(repo)}/issues', query=q)


    def request(self, method, endpoint, payload=None):
        url = urljoin(self.base, endpoint.lstrip('/'))
        parts = urlsplit(url)
        if (parts.scheme,parts.netloc) != self.origin:
            raise RegistryError('Refusing off-origin GitHub write')
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        req = Request(url, method=method, data=data, headers={
            'Authorization': f'Bearer {self.token}', 'Accept':'application/vnd.github+json',
            'Content-Type':'application/json','X-GitHub-Api-Version':self.version,
            'User-Agent':'factory-repo-registry/2.0'})
        try:
            with urlopen(req, timeout=25) as resp:
                body = resp.read(3_000_001)
                if len(body)>3_000_000:
                    raise RegistryError('GitHub response too large')
                return json.loads(body)
        except HTTPError as exc:
            raise GitHubStatusError(f'GitHub {exc.code} on {parts.path}; check Issues/Projects permissions', exc.code) from exc
        except (URLError,TimeoutError,ValueError) as exc:
            raise RegistryError(f'GitHub API failed on {parts.path}: {type(exc).__name__}') from exc

    def graphql(self, query, variables):
        data=self.request('POST','graphql',{'query':query,'variables':variables})
        if data.get('errors'):
            raise RegistryError('GitHub Projects GraphQL failed: ' +
                                str(data['errors'][0].get('message','unknown'))[:140])
        return data.get('data') or {}

    def issue(self, slug, number):
        owner,repo = _validate_repo_slug(slug).split('/')
        if isinstance(number,bool) or not isinstance(number,int) or number<1:
            raise RegistryError('Invalid issue number')
        result,_=self.get(f'repos/{quote(owner)}/{quote(repo)}/issues/{number}')
        if 'pull_request' in result:
            raise RegistryError('Cannot operate on a pull request')
        return result

    def issue_comments(self, slug, number):
        owner,repo=_validate_repo_slug(slug).split('/')
        return self.pages(f'repos/{quote(owner)}/{quote(repo)}/issues/{number}/comments',
                          query={'per_page':100,'sort':'created','direction':'asc'})

    def post_comment(self, slug, number, body):
        owner,repo=_validate_repo_slug(slug).split('/')
        if not isinstance(body,str) or not 1<=len(body)<=6500:
            raise RegistryError('Invalid question comment length')
        return self.request('POST',f'repos/{quote(owner)}/{quote(repo)}/issues/{number}/comments',
                            {'body':body})

    def identity(self):
        if self._identity is None:
            account,_=self.get('user')
            self._identity=str(account.get('login') or '')
        return self._identity


def database():
    import psycopg
    return psycopg.connect(
        host=os.environ.get('DB_HOST','postgres'), port=int(os.environ.get('DB_PORT','5432')),
        dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'], connect_timeout=6)


DDL = '''
CREATE SCHEMA IF NOT EXISTS factory_registry;
CREATE TABLE IF NOT EXISTS factory_registry.repo_state (
  repository TEXT PRIMARY KEY,
  config_hash TEXT NOT NULL,
  last_scan_at TIMESTAMPTZ NOT NULL,
  first_scan_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS factory_registry.issue_events (
  id BIGSERIAL PRIMARY KEY,
  event_key TEXT UNIQUE NOT NULL,
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  issue_updated_at TIMESTAMPTZ NOT NULL,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'acknowledged')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  acknowledged_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_issue_events_status ON factory_registry.issue_events(status, created_at);
CREATE TABLE IF NOT EXISTS factory_registry.issue_runs (
  id TEXT PRIMARY KEY,
  event_id BIGINT NOT NULL UNIQUE REFERENCES factory_registry.issue_events(id),
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  job_id TEXT,
  status TEXT NOT NULL CHECK(status IN ('claimed','running','waiting_for_answer','completed','failed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_issue_runs_state ON factory_registry.issue_runs(status,created_at);
ALTER TABLE factory_registry.issue_runs ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'analysis';
CREATE TABLE IF NOT EXISTS factory_registry.pr_tracking (
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  branch TEXT NOT NULL,
  base_branch TEXT NOT NULL,
  pr_number INTEGER,
  head_sha TEXT,
  reviewed_sha TEXT,
  review_verdict TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(repository, issue_number)
);
CREATE TABLE IF NOT EXISTS factory_registry.review_defects (
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  head_sha TEXT NOT NULL,
  report_digest TEXT NOT NULL,
  defect_number INTEGER NOT NULL,
  PRIMARY KEY(repository,issue_number,head_sha,report_digest)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_issue_runs_active
    ON factory_registry.issue_runs(repository,issue_number)
    WHERE status IN ('claimed','running');
ALTER TABLE factory_registry.repo_state
    ADD COLUMN IF NOT EXISTS monitored BOOLEAN NOT NULL DEFAULT FALSE;
'''


def ensure_schema():
    with database() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL, prepare=False)


def status():
    configured = bool(os.environ.get('FACTORY_GITHUB_REGISTRY_REPO','').strip() and os.environ.get('FACTORY_GITHUB_REGISTRY_TOKEN','').strip())
    try:
        with database() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM factory_registry.issue_events WHERE status = 'pending'")
                pending = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM factory_registry.repo_state WHERE monitored = TRUE")
                monitored = cur.fetchone()[0]
        return {'status': 'ready' if configured else 'needs_configuration', 'configured': configured,
                'pending_events': pending, 'repositories_scanned': monitored}
    except Exception as exc:
        return {'status':'database_unavailable','configured':configured,'error':type(exc).__name__}


def store_repo_scan(config, events, start, *, skip_initial=False):
    import psycopg
    inserted = []
    with database() as conn:
        with conn.cursor() as cur:
            # checkpoint and event insertion commit atomically; replays are idempotent
            cur.execute('SELECT last_scan_at, config_hash FROM factory_registry.repo_state WHERE repository=%s FOR UPDATE',
                        (config['repository'],))
            previous = cur.fetchone()
            if previous is None and skip_initial:
                events = []
            for event in events:
                cur.execute('''INSERT INTO factory_registry.issue_events
                    (event_key,repository,issue_number,issue_updated_at,payload)
                    VALUES (%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (event_key) DO NOTHING RETURNING id''',
                    (event['event_key'],event['repository'],event['issue_number'],event['updated_at'],json.dumps(event)))
                row = cur.fetchone()
                if row:
                    inserted.append({'id':row[0], **event})
            cur.execute('''INSERT INTO factory_registry.repo_state(repository,config_hash,last_scan_at,monitored)
                         VALUES (%s,%s,%s,TRUE)
                         ON CONFLICT(repository) DO UPDATE SET config_hash=EXCLUDED.config_hash,
                         last_scan_at=EXCLUDED.last_scan_at, monitored=TRUE, updated_at=NOW()''',
                        (config['repository'], config['config_hash'], start))
    return inserted


def last_state(slug):
    with database() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT last_scan_at,config_hash,first_scan_at FROM factory_registry.repo_state WHERE repository=%s', (slug,))
            return cur.fetchone()


COORD = pipeline.PipelineCoordinator(SimpleNamespace(
    GitHubClient=GitHubClient, database=database, RegistryError=RegistryError,
    iso_dt=iso_dt, make_event=make_event,
    effective_registry_ref=lambda gh, registry: effective_registry_ref(gh, registry)
))

def bootstrap_registry():
    """Called by n8n; create an empty registry skeleton, NEVER discover repos."""
    token = os.environ.get('FACTORY_GITHUB_REGISTRY_TOKEN', '').strip()
    registry = os.environ.get('FACTORY_GITHUB_REGISTRY_REPO', '').strip()
    if not token or not registry:
        raise RegistryError('FACTORY_GITHUB_REGISTRY_REPO and FACTORY_GITHUB_REGISTRY_TOKEN must be set in .env')
    gh = GitHubClient(token, os.environ.get('FACTORY_GITHUB_API_URL', 'https://api.github.com'),
                      os.environ.get('FACTORY_GITHUB_API_VERSION', '2022-11-28'))
    try:
        return bootstrap.bootstrap(
            gh, registry, os.environ.get('FACTORY_GITHUB_REGISTRY_REF', 'main'),
            os.environ.get('FACTORY_GITHUB_REGISTRY_DIR', 'repos'),
            validate_slug=_validate_repo_slug)
    except (ValueError, RegistryError) as exc:
        raise RegistryError(str(exc)) from exc


def reconcile_monitored_repositories(configs):
    """Commit the current allowlist before a scan; deleted repos stop dispatch.

    This is only called after the complete remote registry parsed successfully.
    Old pending events from deleted repos become inert; existing GitHub resources
    and already running containers are deliberately not deleted.
    """
    monitored = [c['repository'] for c in configs if c['enabled'] and c['issues']['enabled']]
    with database() as conn, conn.cursor() as cur:
        cur.execute("UPDATE factory_registry.repo_state SET monitored=FALSE, updated_at=NOW() WHERE NOT (repository=ANY(%s::text[])) AND monitored=TRUE", (monitored,))
        cur.execute("UPDATE factory_registry.issue_events SET status='acknowledged', acknowledged_at=NOW() WHERE status='pending' AND NOT (repository=ANY(%s::text[]))", (monitored,))


def effective_registry_ref(github, registry):
    ref = os.environ.get('FACTORY_GITHUB_REGISTRY_REF', 'main').strip()
    if ref and ref != 'auto':
        return ref
    owner, name = _validate_repo_slug(registry).split('/')
    metadata, _ = github.get(f'repos/{quote(owner)}/{quote(name)}')
    branch = metadata.get('default_branch')
    if not isinstance(branch, str) or not branch.strip():
        raise RegistryError('Registry has no default branch; run GitHub bootstrap first')
    return branch


def scan():
    token = os.environ.get('FACTORY_GITHUB_REGISTRY_TOKEN','').strip()
    registry = os.environ.get('FACTORY_GITHUB_REGISTRY_REPO','').strip()
    if not token or not registry:
        raise RegistryError('FACTORY_GITHUB_REGISTRY_REPO and FACTORY_GITHUB_REGISTRY_TOKEN must be set in .env')
    github = GitHubClient(token, os.environ.get('FACTORY_GITHUB_API_URL','https://api.github.com'),
                          os.environ.get('FACTORY_GITHUB_API_VERSION','2022-11-28'))
    configs = github.registry(registry, effective_registry_ref(github, registry),
                              os.environ.get('FACTORY_GITHUB_REGISTRY_DIR','repos'))
    # A deleted .repo file is authoritative. Do not re-add it from discovery.
    reconcile_monitored_repositories(configs)
    start = now_utc()
    results = []
    new_events = []
    for conf in configs:
        repo = conf['repository']
        if not conf['enabled'] or not conf['issues']['enabled']:
            results.append({'repository':repo,'status':'disabled','new_events':0})
            continue
        try:
            state = last_state(repo)
            if conf['kanban']['enabled']:
                # New GitHub issues are linked to the repo-specific board in
                # Backlog. Existing cards are never overwritten on scans.
                board = COORD.board(github, conf)
                cards=board.all_cards()
                card_numbers = {c['issue']['number'] for c in cards}
                def initial_column(issue):
                    labels = {label.get('name') for label in issue.get('labels', []) if isinstance(label, dict)}
                    if 'factory:review-defect' in labels:
                        return 'failed'
                    if conf.get('intake',{}).get('enabled') and not planner.parse_child(issue):
                        kind=intake.issue_type(issue)
                        if kind in ('development','bug','feature'):
                            return kind+'_intake'
                    return 'backlog'
                # GitHub's own Project auto-add may have inserted a card with
                # an empty Status already. Initialize ONLY missing Status values.
                # Never reset a user-selected status during a normal scan.
                for card in cards:
                    if card.get('status') is None and card['issue'].get('state')=='open':
                        live=github.issue(repo,card['issue']['number'])
                        if 'pull_request' not in live:
                            board.set_status(card['item_id'],initial_column(live))
                since = state[0] - timedelta(seconds=120) if state else None
                for issue in github.issues(repo, since=since):
                    if ('pull_request' in issue or issue.get('state') != 'open'
                            or issue.get('number') in card_numbers):
                        continue
                    if not issue.get('node_id'):
                        raise RegistryError('GitHub issue missing node_id')
                    initial=initial_column(issue)
                    new_item = (board.add_issue_to_backlog(issue['node_id'], initial)
                                if initial!='backlog' else board.add_issue_to_backlog(issue['node_id']))
                    card_numbers.add(issue['number'])
                # GitHub Project Status is authoritative. Board changes may NOT
                # update issue.updated_at, so never use a REST `since` filter here.
                events = COORD.collect_events(github, conf,
                         first_scan_at=state[2] if state else (start if conf['issues']['initial_sync']=='new_only' else None))
                inserted = store_repo_scan(conf, events, start)
                new_events.extend(inserted)
                results.append({'repository':repo,'status':'ok','new_events':len(inserted),
                                'source':'github_project_status'})
                continue
            if state is None and conf['issues']['initial_sync'] == 'new_only':
                inserted = store_repo_scan(conf, [], start, skip_initial=True)
            else:
                since = None
                if state and conf['config_hash'] == state[1]:
                    since = state[0] - timedelta(seconds=120)
                items = list(github.issues(repo, since=since, required_labels=conf['issues']['required_labels']))
                events = [make_event(item, conf) for item in items
                          if eligible(item, conf) and
                          (conf['issues']['initial_sync'] != 'new_only' or
                           not state or iso_dt(item['updated_at']) > state[2])]
                inserted = store_repo_scan(conf, events, start)
            new_events.extend(inserted)
            results.append({'repository':repo,'status':'ok','new_events':len(inserted)})
        except Exception as exc:
            LOG.warning('scan failed for %s: %s',repo,exc)
            results.append({'repository':repo,'status':'error','error':str(exc)[:250],'new_events':0})
    return {'registry':registry, 'config_count':len(configs), 'repositories':results,
            'new_events_count':len(new_events), 'new_events':new_events[:MAX_EVENTS_IN_SCAN],
            'truncated':len(new_events)>MAX_EVENTS_IN_SCAN}


def ensure_boards():
    # n8n calls this through the internal Docker network; no AI worker is used.
    token = os.environ.get('FACTORY_GITHUB_REGISTRY_TOKEN','').strip()
    registry = os.environ.get('FACTORY_GITHUB_REGISTRY_REPO','').strip()
    if not token or not registry:
        raise RegistryError('GitHub registry repository/token missing in .env')
    github = GitHubClient(token, os.environ.get('FACTORY_GITHUB_API_URL','https://api.github.com'),
                          os.environ.get('FACTORY_GITHUB_API_VERSION','2022-11-28'))
    configs = github.registry(registry, effective_registry_ref(github, registry),
                              os.environ.get('FACTORY_GITHUB_REGISTRY_DIR','repos'))
    results=[]
    for conf in configs:
        if not conf['enabled'] or not conf['kanban']['enabled']:
            continue
        try:
            provisioned=board_setup.ensure_board(github,conf)
            if conf.get('intake',{}).get('enabled'):
                provisioned['intake']=intake.ensure_intake(github,conf)
            results.append(provisioned)
        except Exception as exc:
            LOG.warning('board provisioning failed for %s: %s', conf['repository'],exc)
            results.append({'repository':conf['repository'],'status':'error','error':str(exc)[:350]})
    return {'registry':registry,'boards':results, 'errors':sum(x['status']=='error' for x in results)}


def list_events(status_filter='pending', limit=50):
    if status_filter not in ('pending','acknowledged','all'):
        raise RegistryError('invalid event status')
    limit = min(max(int(limit),1), 100)
    with database() as conn:
        with conn.cursor() as cur:
            if status_filter == 'all':
                cur.execute('SELECT id, status, payload FROM factory_registry.issue_events ORDER BY created_at DESC LIMIT %s', (limit,))
            else:
                cur.execute('SELECT id, status, payload FROM factory_registry.issue_events WHERE status=%s ORDER BY created_at DESC LIMIT %s', (status_filter,limit))
            return [{'id':id,'status':status,**(payload if isinstance(payload,dict) else json.loads(payload))} for id,status,payload in cur.fetchall()]


def ack_event(event_id):
    with database() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE factory_registry.issue_events SET status='acknowledged', acknowledged_at=NOW() WHERE id=%s RETURNING id", (event_id,))
            return bool(cur.fetchone())


class Handler(BaseHTTPRequestHandler):
    server_version = 'FactoryRegistry/1.0'

    def _send(self, code, body):
        data = json.dumps(body, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.path == '/limits':
                self._send(200, lifecycle._manager('GET', '/limits'));return
            if parsed.path == '/runner/pipeline/status':
                self._send(200, COORD.pipeline_status());return
            if parsed.path == '/health':
                self._send(200, status()); return
            match = re.fullmatch(r'/runner/([0-9a-f]{24})/status', parsed.path)
            if match:
                self._send(200,COORD.status(match.group(1)));return
            if parsed.path == '/events':
                from urllib.parse import parse_qs
                args = parse_qs(parsed.query)
                self._send(200, {'events':list_events(args.get('status',['pending'])[0], args.get('limit',['50'])[0])});return
            self._send(404, {'error':'not found'})
        except (RegistryError,ValueError) as exc:
            self._send(400,{'error':str(exc)})
        except Exception as exc:
            LOG.exception('GET failure')
            self._send(500,{'error':type(exc).__name__})

    def do_POST(self):
        try:
            if self.headers.get('Content-Length') not in (None,'0'):
                self._send(400, {'error':'request body not supported'}); return
            if self.path == '/limits/refresh':
                self._send(200, lifecycle._manager('POST', '/limits/refresh', {}));return
            if self.path == '/runner/merge-ready':
                self._send(200,COORD.merge_ready());return
            if self.path == '/runner/claim':
                self._send(200,COORD.claim());return
            run_match = re.fullmatch(r'/runner/([0-9a-f]{24})/(start|finish)',self.path)
            if run_match:
                run_id,operation=run_match.groups()
                self._send(200,COORD.start(run_id) if operation=='start' else COORD.finish(run_id));return
            if self.path == '/bootstrap':
                if not SCAN_LOCK.acquire(blocking=False):
                    self._send(409, {'error':'bootstrap/scan already running'});return
                try:
                    result = bootstrap_registry()
                finally:
                    SCAN_LOCK.release()
                self._send(200, result);return
            if self.path == '/boards/ensure':
                if not SCAN_LOCK.acquire(blocking=False):
                    self._send(409, {'error':'scan/board provisioning already running'});return
                try:
                    result = ensure_boards()
                finally:
                    SCAN_LOCK.release()
                self._send(200 if result['errors']==0 else 207, result);return
            if self.path == '/scan':
                if not SCAN_LOCK.acquire(blocking=False):
                    self._send(409,{'error':'scan already running'});return
                try:
                    data = scan()
                finally:
                    SCAN_LOCK.release()
                self._send(200, data);return
            match = re.fullmatch(r'/events/([1-9][0-9]*)/ack', self.path)
            if match:
                found = ack_event(int(match.group(1)))
                self._send(200 if found else 404, {'acknowledged':found});return
            self._send(404, {'error':'not found'})
        except RegistryError as exc:
            self._send(424,{'error':str(exc)})
        except Exception as exc:
            LOG.exception('POST failure')
            self._send(500,{'error':type(exc).__name__})

    def log_message(self, fmt, *args):
        LOG.info('%s %s',self.client_address[0],fmt % args)


def serve():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    ensure_schema()
    port = int(os.environ.get('FACTORY_GITHUB_REGISTRY_PORT','8090'))
    server = ThreadingHTTPServer(('0.0.0.0',port),Handler)
    LOG.info('GitHub Registry watcher serving on %s (internal-only), n8n orchestrates stateless worker runs',port)
    server.serve_forever(poll_interval=0.5)


if __name__ == '__main__':
    serve()
