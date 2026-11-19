"""Factory Agent Manager.

An authenticated, allowlisted HTTP facade around the local Docker Engine.
Does NOT accept image, command, mount, network or privilege settings from clients.
Docker socket is root-equivalent: operate only on a trusted local host.
"""

import base64
import hmac
import http.client
import json
import logging
from pathlib import Path
import os
import re
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit, parse_qs

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('factory-agent-manager')

ROLES = ('smoke', 'planner', 'requirements', 'architect', 'developer', 'tester', 'reviewer', 'security')
PROVIDERS = ('codex', 'claude', 'gemini')
JOB_ID = re.compile(r'^[a-f0-9]{20}$')
PART = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$')
REF = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,139}$')

OWNER = os.getenv('FACTORY_NAME', 'factory')
TOKEN = os.getenv('FACTORY_AGENT_API_TOKEN', '')
IMAGE_PREFIX = os.getenv('FACTORY_AGENT_IMAGE_PREFIX', 'factory-agent')
IMAGE_TAG = os.getenv('FACTORY_AGENT_IMAGE_TAG', 'local')
MAX_CONCURRENT = int(os.getenv('FACTORY_AGENT_MAX_CONCURRENT', '3'))
DEFAULT_TIMEOUT = int(os.getenv('FACTORY_AGENT_DEFAULT_TIMEOUT_SEC', '900'))
MAX_TIMEOUT = int(os.getenv('FACTORY_AGENT_MAX_TIMEOUT_SEC', '1800'))
RETENTION = int(os.getenv('FACTORY_AGENT_RETENTION_SEC', '7200'))
MEMORY_MB = int(os.getenv('FACTORY_AGENT_MEMORY_MB', '1536'))
CPUS = float(os.getenv('FACTORY_AGENT_CPUS', '2.0'))
PIDS_LIMIT = int(os.getenv('FACTORY_AGENT_PIDS_LIMIT', '256'))
NETWORK_MODE = os.getenv('FACTORY_WORKER_NETWORK_MODE', 'factory_egress')
EGRESS_NETWORK = os.getenv('FACTORY_EGRESS_NETWORK', 'factory_egress')
HOST_AUTH_ROOT = Path(os.getenv('FACTORY_HOST_AUTH_ROOT', '/nonexistent/factory-auth'))
HOST_WORKSPACE_ROOT = Path(os.getenv('FACTORY_HOST_WORKSPACE_ROOT', '/nonexistent/factory-workspaces'))
WORKSPACE_ROOT = Path('/opt/factory-workspaces')
CODEX_SANDBOX = os.getenv('FACTORY_CODEX_SANDBOX', 'workspace-write')
CLAUDE_WRITE = os.getenv('FACTORY_CLAUDE_PERMISSION_WRITE', 'acceptEdits')
GEMINI_WRITE = os.getenv('FACTORY_GEMINI_APPROVAL_WRITE', 'auto_edit')
AUTH_ROOT = Path('/opt/factory-auth')
# Only allow predetermined profiles, never user-supplied image/mount/credential paths.
AUTH_PROFILES = ('default', 'planner', 'requirements', 'architect', 'developer',
                 'tester', 'reviewer', 'security')
ROLE_PROFILE = {
    provider: {role: os.getenv(f'FACTORY_{provider.upper()}_PROFILE_{role.upper()}', 'default')
               for role in ROLES if role != 'smoke'}
    for provider in PROVIDERS
}
GITHUB_TOKEN = os.getenv('FACTORY_GITHUB_TOKEN', '')
MANAGER_PORT = int(os.getenv('FACTORY_AGENT_MANAGER_PORT', '8080'))
DOCKER_SOCK = '/var/run/docker.sock'
CREATE_LOCK = threading.Lock()
LIMIT_ENABLED = os.getenv('FACTORY_LIMIT_MONITOR_ENABLED', 'false').lower() == 'true'
LIMIT_UNKNOWN_POLICY = os.getenv('FACTORY_LIMIT_UNKNOWN_POLICY', 'block')
LIMIT_REFRESH_SEC = int(os.getenv('FACTORY_LIMIT_REFRESH_SEC', '300'))
LIMIT_CHECK_TIMEOUT_SEC = int(os.getenv('FACTORY_LIMIT_CHECK_TIMEOUT_SEC', '25'))
LIMIT_MIN_REMAINING_PERCENT = float(os.getenv('FACTORY_LIMIT_MIN_REMAINING_PERCENT', '5'))
LIMIT_CODEX_BUCKETS = os.getenv('FACTORY_LIMIT_CODEX_BUCKETS', 'codex')
LIMIT_MAX_PROBES = int(os.getenv('FACTORY_LIMIT_MAX_PROBES', '3'))


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class EngineError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class EngineUnavailable(Exception):
    """A connection to the Docker Engine could not be established."""


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path=DOCKER_SOCK):
        super().__init__('localhost', timeout=15)
        self.path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.path)
        self.sock = sock


def engine(method, path, payload=None, *, binary=False):
    connection = UnixConnection()
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    headers = {'Content-Type': 'application/json'} if body is not None else {}
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            raise EngineError(502, 'Docker response exceeded size limit')
        if response.status >= 400:
            try:
                message = json.loads(data).get('message', 'Docker Engine error')
            except (ValueError, UnicodeDecodeError):
                message = data.decode('utf-8', errors='replace')[:300]
            raise EngineError(response.status, message)
        if binary:
            return data
        return json.loads(data) if data.strip() else {}
    except (OSError, TimeoutError) as exc:
        raise EngineUnavailable(f'Docker Engine connection failed: {exc}') from exc
    finally:
        connection.close()


def daemon_mount_paths():
    """Discover bind sources as seen by the Docker DAEMON, not the WSL client.

    Docker Desktop may rewrite /mnt/e/... into /run/desktop/... . Copying the
    .env path directly into a Docker Engine API create request would then fail.
    The manager asks Docker which host paths back its own verified bind mounts.
    """
    container_id = socket.gethostname()
    if not re.fullmatch(r'[a-f0-9]{12,64}', container_id):
        raise RuntimeError('Cannot determine manager container ID to resolve bind paths')
    info = engine('GET', '/containers/' + container_id + '/json')
    sources = {
        entry.get('Destination'): entry.get('Source')
        for entry in info.get('Mounts', [])
        if entry.get('Type') == 'bind'
    }
    auth = sources.get('/opt/factory-auth')
    workspace = sources.get('/opt/factory-workspaces')
    if not auth or not workspace:
        raise RuntimeError('Required manager bind mounts are missing')
    if (not Path(auth).is_absolute() or not Path(workspace).is_absolute()
            or auth == workspace or auth == '/' or workspace == '/'):
        raise RuntimeError('Unsafe daemon bind mount paths')
    return Path(auth), Path(workspace)


def docker_list():
    filters = {'label': ['factory.managed=true', f'factory.owner={OWNER}']}
    query = urlencode({'all': '1', 'filters': json.dumps(filters)})
    return engine('GET', '/containers/json?' + query)


def job_name(job_id):
    return f'{OWNER}-agent-{job_id}'


def inspected(job_id):
    if not JOB_ID.fullmatch(job_id):
        raise ApiError(400, 'Invalid job ID')
    try:
        info = engine('GET', '/containers/' + job_name(job_id) + '/json')
    except EngineError as exc:
        if exc.code == 404:
            raise ApiError(404, 'Job not found') from exc
        raise
    labels = info.get('Config', {}).get('Labels', {})
    if (labels.get('factory.managed') != 'true' or
            labels.get('factory.owner') != OWNER or
            labels.get('factory.job_id') != job_id):
        raise ApiError(404, 'Job not found')
    return info


def container_logs(container_id, lines=80):
    query = urlencode({'stdout': 'true', 'stderr': 'true', 'tail': str(lines)})
    data = engine('GET', f'/containers/{container_id}/logs?' + query, binary=True)
    # Worker containers use Tty=true, so the Docker log stream has no multiplex header.
    return data.decode('utf-8', errors='replace')[-50000:]


def job_info(info, *, include_logs=True):
    state = info.get('State', {})
    labels = info.get('Config', {}).get('Labels', {})
    code = state.get('ExitCode', 0)
    raw_status = state.get('Status', 'unknown')
    status = ('running' if raw_status in ('running', 'restarting') else
              'pending' if raw_status == 'created' else
              'succeeded' if raw_status == 'exited' and code == 0 else
              'failed' if raw_status in ('exited', 'dead') else raw_status)
    out = {
        'job_id': labels.get('factory.job_id'),
        'role': labels.get('factory.role'),
        'provider': labels.get('factory.provider', 'codex'),
        'repository': labels.get('factory.repository', '') or None,
        'status': status,
        'docker_status': raw_status,
        'exit_code': code if raw_status in ('exited', 'dead') else None,
        'container_id': info.get('Id', '')[:12],
        'created_at': labels.get('factory.created_at'),
        'timeout_seconds': int(labels.get('factory.timeout', DEFAULT_TIMEOUT)),
    }
    if include_logs:
        logs = container_logs(info['Id'], 100)
        out['logs_tail'] = logs[-12000:]
        out['result'] = None
        for line in reversed(logs.splitlines()):
            if line.startswith('FACTORY_RESULT='):
                try:
                    out['result'] = json.loads(line[len('FACTORY_RESULT='):])
                except ValueError:
                    pass
                break
    return out


def check_repository(repo):
    if not repo:
        return ''
    if not isinstance(repo, str) or len(repo) > 201:
        raise ApiError(400, 'repository must be owner/repository')
    parts = repo.split('/')
    if len(parts) != 2 or any(not PART.fullmatch(p) or p in ('.', '..') or '..' in p for p in parts):
        raise ApiError(400, 'repository must be a valid GitHub owner/repository')
    return repo


def check_ref(ref):
    if not ref:
        return ''
    if (not isinstance(ref, str) or not REF.fullmatch(ref) or '..' in ref or
            '//' in ref or ref.endswith('/') or ref.endswith('.lock')):
        raise ApiError(400, 'Invalid Git branch/ref')
    return ref


def auth_root_for(provider, profile):
    return AUTH_ROOT / provider / profile


def auth_ready(provider, profile):
    """Check for stored login files, without reading secrets into logs.

    This is only a preflight. Worker checks the provider CLI at runtime.
    """
    if provider not in PROVIDERS or profile not in AUTH_PROFILES:
        return False
    root = auth_root_for(provider, profile)
    try:
        if provider == 'codex':
            credentials = json.loads((root / 'auth.json').read_text(encoding='utf-8'))
            return (isinstance(credentials, dict)
                    and credentials.get('auth_mode') == 'chatgpt'
                    and bool(credentials.get('tokens', {}).get('access_token')))
        if provider == 'claude':
            return (root / '.factory-login-ready').is_file()
        if provider == 'gemini':
            return (root / '.gemini' / 'oauth_creds.json').is_file()
        return False
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def login_profile(provider, role):
    profile = ROLE_PROFILE.get(provider, {}).get(role)
    if profile not in AUTH_PROFILES:
        raise ApiError(500, f'Invalid {provider} login profile configured for role {role}')
    return profile


def is_limit_probe(container):
    return container.get('Labels', {}).get('factory.role') == 'limit-check'


def _probe_result(container):
    """Read only the structured result, never raw terminal/account output."""
    try:
        info = inspected(container['Labels']['factory.job_id'])
        data = job_info(info)
        result = data.get('result')
        if data['status'] == 'succeeded' and isinstance(result, dict) and result.get('status') in ('available', 'exhausted', 'unknown'):
            return result
    except (EngineError, ApiError, KeyError, ValueError):
        pass
    return {'status': 'unknown', 'reason': 'probe_incomplete_or_failed'}


def limit_snapshot(provider, profile, containers=None):
    if containers is None:
        containers = docker_list()
    if not auth_ready(provider, profile):
        return {'provider': provider, 'profile': profile, 'status': 'missing_login',
                'fresh': False, 'reason': 'Login is not configured'}
    matches = [c for c in containers if is_limit_probe(c)
               and c.get('Labels', {}).get('factory.provider') == provider
               and c.get('Labels', {}).get('factory.profile') == profile]
    matches.sort(key=lambda c: int(c.get('Labels', {}).get('factory.created_at', 0)), reverse=True)
    if not matches:
        return {'provider': provider, 'profile': profile, 'status': 'unknown',
                'fresh': False, 'reason': 'No limit check completed'}
    item = matches[0]
    age = max(0, int(time.time()) - int(item['Labels'].get('factory.created_at', '0')))
    if item.get('State') in ('running', 'created', 'restarting'):
        return {'provider': provider, 'profile': profile, 'status': 'checking',
                'fresh': False, 'age_seconds': age}
    result = _probe_result(item)
    return {'provider': provider, 'profile': profile, 'status': result['status'],
            'fresh': age < LIMIT_REFRESH_SEC, 'age_seconds': age,
            'checked_at': result.get('checked_at'), 'details': result}


def limit_availability(provider, role, containers=None):
    if provider not in PROVIDERS or role not in ROLES or role == 'smoke':
        raise ApiError(400, 'Invalid provider/role for quota check')
    profile = login_profile(provider, role)
    if not LIMIT_ENABLED:
        return {'available': True, 'status': 'disabled', 'provider': provider,
                'profile': profile, 'reason': 'Quota monitoring is disabled'}
    snapshot = limit_snapshot(provider, profile, containers)
    if snapshot['status'] == 'available' and snapshot['fresh']:
        available = True
    elif snapshot['status'] in ('missing_login', 'exhausted'):
        available = False
    elif snapshot['status'] == 'checking':
        available = False
    else:
        available = LIMIT_UNKNOWN_POLICY == 'allow' and snapshot['status'] == 'unknown'
        # A stale positive observation does not qualify, even with allow policy.
    return {'available': available, **snapshot,
            'reason': snapshot.get('reason') or snapshot.get('details', {}).get('reason', '')}


def _start_limit_probe(provider, profile, existing=None):
    if provider not in PROVIDERS or profile not in AUTH_PROFILES:
        raise ApiError(400, 'Invalid provider/profile')
    if not auth_ready(provider, profile):
        raise ApiError(409, 'Login missing for requested quota profile')
    with CREATE_LOCK:
        current = docker_list() if existing is None else existing
        active_probes = [item for item in current if is_limit_probe(item)
                         and item['State'] in ('created', 'running', 'restarting')]
        if len(active_probes) >= LIMIT_MAX_PROBES:
            raise ApiError(429, 'Maximum concurrent quota checks reached')
        if any(item['State'] in ('created', 'running', 'restarting')
               and item['Labels'].get('factory.provider') == provider
               and item['Labels'].get('factory.profile') == profile
               for item in current):
            raise ApiError(429, 'Login profile is currently busy')
        job_id = uuid.uuid4().hex[:20]
        labels = {
            'factory.managed': 'true', 'factory.owner': OWNER,
            'factory.job_id': job_id, 'factory.role': 'limit-check',
            'factory.provider': provider, 'factory.profile': profile,
            'factory.created_at': str(int(time.time())),
            'factory.timeout': str(LIMIT_CHECK_TIMEOUT_SEC + 15),
        }
        config = {
            'Image': f'{IMAGE_PREFIX}-{provider}:{IMAGE_TAG}',
            'Entrypoint': ['python3', '/opt/factory/limits.py'],
            'Env': [f'FACTORY_LIMIT_PROVIDER={provider}', 'HOME=/auth',
                    'CODEX_HOME=/auth', 'CLAUDE_CONFIG_DIR=/auth/.claude',
                    'XDG_CACHE_HOME=/tmp/.cache', 'DISABLE_AUTOUPDATER=1',
                    f'FACTORY_LIMIT_CHECK_TIMEOUT_SEC={LIMIT_CHECK_TIMEOUT_SEC}',
                    f'FACTORY_LIMIT_MIN_REMAINING_PERCENT={LIMIT_MIN_REMAINING_PERCENT}',
                    f'FACTORY_LIMIT_CODEX_BUCKETS={LIMIT_CODEX_BUCKETS}',
                    'GEMINI_TELEMETRY_ENABLED=false', 'GEMINI_CLI_TRUST_WORKSPACE=true'],
            'Labels': labels, 'User': '1000:1000',
            'WorkingDir': '/tmp', 'Tty': True, 'OpenStdin': False,
            'HostConfig': {'NetworkMode': NETWORK_MODE,
                'Mounts': [{'Type': 'bind',
                            'Source': str(HOST_AUTH_ROOT / provider / profile),
                            'Target': '/auth', 'ReadOnly': False}],
                'Memory': min(MEMORY_MB, 1024) * 1024 * 1024,
                'NanoCpus': int(min(CPUS, 1.0) * 1e9),
                'PidsLimit': 128, 'ReadonlyRootfs': True,
                'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges'],
                'Tmpfs': {'/tmp': 'rw,nosuid,noexec,size=96m'},
                'AutoRemove': False,
                'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '2m', 'max-file': '1'}}},
        }
        ctr = engine('POST', '/containers/create?' + urlencode({'name': job_name(job_id)}), config)
        try:
            engine('POST', f'/containers/{ctr["Id"]}/start')
        except Exception:
            engine('DELETE', f'/containers/{ctr["Id"]}?force=true&v=true')
            raise
        return {'job_id': job_id, 'provider': provider, 'profile': profile,
                'status': 'checking', 'container_id': ctr['Id'][:12]}


def refresh_limits():
    """Launch at most LIMIT_MAX_PROBES for stale profiles, no model prompts."""
    if not LIMIT_ENABLED:
        return {'enabled': False, 'started': [], 'reason': 'Quota monitoring disabled'}
    current = docker_list()
    profiles = sorted({(provider, login_profile(provider, role))
                       for provider in PROVIDERS for role in ROLES if role != 'smoke'})
    started, skipped = [], []
    active = sum(1 for c in current if is_limit_probe(c)
                 and c.get('State') in ('running', 'created', 'restarting'))
    for provider, profile in profiles:
        if not auth_ready(provider, profile):
            skipped.append({'provider': provider, 'profile': profile, 'reason': 'missing_login'})
            continue
        state = limit_snapshot(provider, profile, current)
        if state['fresh'] or state['status'] == 'checking':
            skipped.append({'provider': provider, 'profile': profile, 'reason': state['status']})
            continue
        if active >= LIMIT_MAX_PROBES:
            skipped.append({'provider': provider, 'profile': profile, 'reason': 'capacity'})
            continue
        try:
            job = _start_limit_probe(provider, profile)
            started.append(job)
            active += 1
        except ApiError as exc:
            skipped.append({'provider': provider, 'profile': profile, 'reason': exc.message})
    return {'enabled': True, 'started': started, 'skipped': skipped}


def all_limits():
    current = docker_list()
    profiles = sorted({(provider, login_profile(provider, role))
                       for provider in PROVIDERS for role in ROLES if role != 'smoke'})
    return {'enabled': LIMIT_ENABLED, 'unknown_policy': LIMIT_UNKNOWN_POLICY,
            'refresh_seconds': LIMIT_REFRESH_SEC,
            'profiles': [limit_snapshot(provider, profile, current)
                         for provider, profile in profiles]}


def create_job(payload):
    if not isinstance(payload, dict):
        raise ApiError(400, 'JSON object expected')
    allowed = {'provider', 'role', 'task', 'repository', 'ref', 'timeout_seconds', 'structured_result'}
    if set(payload) - allowed:
        raise ApiError(400, 'Unknown request fields; image/command/volumes are not accepted')
    provider = payload.get('provider', 'codex')
    if provider not in PROVIDERS:
        raise ApiError(400, f'provider must be one of: {", ".join(PROVIDERS)}')
    role = payload.get('role')
    if role not in ROLES:
        raise ApiError(400, f'role must be one of: {", ".join(ROLES)}')
    task = payload.get('task', '')
    if not isinstance(task, str) or len(task) > 12000 or '\x00' in task:
        raise ApiError(400, 'task must be a string of at most 12000 characters')
    if role != 'smoke' and not task.strip():
        raise ApiError(400, 'task is required for this role')
    profile = login_profile(provider, role) if role != 'smoke' else ''
    if profile and not auth_ready(provider, profile):
        raise ApiError(409, f'{provider} login missing for profile {profile}. '
                       f'Run ./factory.sh auth-login {provider} {profile} (or auth-import)')
    repo = check_repository(payload.get('repository', ''))
    ref = check_ref(payload.get('ref', ''))
    if ref and not repo:
        raise ApiError(400, 'ref requires repository')
    structured = payload.get('structured_result', False)
    if not isinstance(structured, bool):
        raise ApiError(400, 'structured_result must be a boolean')
    timeout = payload.get('timeout_seconds', DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not (10 <= timeout <= MAX_TIMEOUT):
        raise ApiError(400, f'timeout_seconds must be an integer between 10 and {MAX_TIMEOUT}')

    with CREATE_LOCK:
        existing = docker_list()
        active = sum(1 for entry in existing if not is_limit_probe(entry) and entry.get('State') in ('running', 'created', 'restarting'))
        if active >= MAX_CONCURRENT:
            raise ApiError(429, 'Maximum concurrent agent containers reached')
        # Refresh tokens are rotated in auth.json. Avoid concurrent writers
        # using the same login profile; use distinct profiles for parallel roles.
        if role != 'smoke':
            admission = limit_availability(provider, role, existing)
            if not admission['available']:
                raise ApiError(429, f'Quota preflight blocked {provider}/{profile}: '
                               f'{admission["status"]}. Refresh quota status before dispatch.')
        if profile and any(
            entry.get('State') in ('running', 'created', 'restarting')
            and entry.get('Labels', {}).get('factory.provider', 'codex') == provider
            and entry.get('Labels', {}).get('factory.profile', entry.get('Labels', {}).get('factory.codex_profile')) == profile
            for entry in existing
        ):
            raise ApiError(429, f'{provider} profile {profile} is already in use; '
                           'use separate login profiles for parallel agents')
        job_id = uuid.uuid4().hex[:20]
        image = f'{IMAGE_PREFIX}-{provider}:{IMAGE_TAG}'
        labels = {
            'factory.managed': 'true',
            'factory.owner': OWNER,
            'factory.job_id': job_id,
            'factory.role': role,
            'factory.provider': provider,
            'factory.profile': profile,
            'factory.repository': repo,
            'factory.created_at': str(int(time.time())),
            'factory.timeout': str(timeout),
        }
        if provider == 'codex':
            labels['factory.codex_profile'] = profile  # v3 active-session compatibility
        env = [
            f'FACTORY_AGENT_ROLE={role}',
            f'FACTORY_AGENT_PROVIDER={provider}',
            f'FACTORY_AGENT_TASK_B64={base64.b64encode(task.encode()).decode()}',
            f'FACTORY_AGENT_STRUCTURED_RESULT={str(structured).lower()}',
            f'FACTORY_AGENT_REPOSITORY={repo}',
            f'FACTORY_AGENT_REF={ref}',
            f'FACTORY_CODEX_SANDBOX={CODEX_SANDBOX}',
            f'FACTORY_CLAUDE_PERMISSION_WRITE={CLAUDE_WRITE}',
            f'FACTORY_GEMINI_APPROVAL_WRITE={GEMINI_WRITE}',
        ]
        if role != 'smoke':
            env += [
                'HOME=/auth',
                'CODEX_HOME=/auth',
                'CLAUDE_CONFIG_DIR=/auth/.claude',
                'XDG_CACHE_HOME=/auth/.cache',
                'GIT_TERMINAL_PROMPT=0',
                'GIT_ASKPASS=/opt/factory/git-askpass.sh',
            ]
            if GITHUB_TOKEN:
                # Optional GitHub repository credential; NOT an AI API key.
                env.append(f'FACTORY_GITHUB_TOKEN={GITHUB_TOKEN}')
        # Each job owns a host bind-mounted workspace; no anonymous Docker volumes.
        # The daemon sees HOST_WORKSPACE_ROOT; this manager container sees WORKSPACE_ROOT.
        workspace = WORKSPACE_ROOT / job_id
        try:
            workspace.mkdir(mode=0o750)
            # IMPORTANT: chmod BEFORE chown. The manager intentionally has
            # CAP_CHOWN and CAP_DAC_OVERRIDE, but not CAP_FOWNER. After chown
            # to UID 1000 it is no longer the owner and chmod returns EPERM.
            workspace.chmod(0o750)
            if os.geteuid() == 0:
                os.chown(workspace, 1000, 1000)
        except OSError as exc:
            LOG.error('Workspace preparation denied at %s: %s', workspace, exc)
            try:
                workspace.rmdir()
            except OSError:
                pass
            raise ApiError(
                500,
                'Agent workspace bind mount is not writable. '
                'Run sudo ./factory.sh repair-workspaces and sudo ./factory.sh doctor. '
                f'Filesystem error: {exc.strerror or type(exc).__name__}'
            ) from exc
        mounts = [
            {'Type': 'bind', 'Source': str(HOST_WORKSPACE_ROOT / job_id),
             'Target': '/workspace', 'ReadOnly': False},
        ]
        if profile:
            # Only one predetermined provider/profile is mounted per worker.
            # OAuth logins must be writable for refreshing tokens.
            mounts.append({'Type': 'bind',
                           'Source': str(HOST_AUTH_ROOT / provider / profile),
                           'Target': '/auth', 'ReadOnly': False})
        config = {
            'Image': image,
            'Env': env,
            'Labels': labels,
            'User': '1000:1000',
            'WorkingDir': '/workspace',
            'Tty': True,
            'OpenStdin': False,
            'HostConfig': {
                'NetworkMode': NETWORK_MODE,
                # Only the selected login bind directory is visible to the worker.
                # Writable because providers rotate OAuth refresh tokens.
                'Mounts': mounts,
                'Memory': MEMORY_MB * 1024 * 1024,
                'NanoCpus': int(CPUS * 1000000000),
                'PidsLimit': PIDS_LIMIT,
                'ReadonlyRootfs': True,
                'CapDrop': ['ALL'],
                'SecurityOpt': ['no-new-privileges'],
                'Tmpfs': {'/tmp': 'rw,nosuid,noexec,size=256m'},
                'AutoRemove': False,
                'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '10m', 'max-file': '2'}},
            },
        }
        url = '/containers/create?' + urlencode({'name': job_name(job_id)})
        try:
            container = engine('POST', url, config)
        except Exception as exc:
            # A failed create leaves no worker container; keep the directory only
            # if it holds files, otherwise remove the abandoned empty directory.
            try:
                workspace.rmdir()
            except OSError:
                pass
            if isinstance(exc, EngineError) and exc.code == 404:
                raise ApiError(503, f'Agent image missing: {image}. Run ./factory.sh build-agents') from exc
            raise
        try:
            engine('POST', f'/containers/{container["Id"]}/start')
        except Exception:
            engine('DELETE', f'/containers/{container["Id"]}?force=true&v=true')
            raise
        LOG.info('Started provider=%s role=%s job=%s repo=%s', provider, role, job_id, repo or '(new)')
        return {'job_id': job_id, 'provider': provider, 'role': role, 'status': 'running',
                'status_url': f'/jobs/{job_id}', 'container_id': container['Id'][:12]}


def sweeper():
    while True:
        try:
            now = time.time()
            for container in docker_list():
                labels = container.get('Labels', {})
                cid = container['Id']
                created = int(labels.get('factory.created_at', int(now)))
                timeout = int(labels.get('factory.timeout', DEFAULT_TIMEOUT))
                status = container.get('State')
                if status == 'created' and now - created > 120:
                    LOG.warning('Removing abandoned created job=%s', labels.get('factory.job_id'))
                    engine('DELETE', f'/containers/{cid}?force=true&v=true')
                elif status in ('running', 'restarting') and now - created > timeout:
                    LOG.warning('Stopping timed-out job=%s', labels.get('factory.job_id'))
                    try:
                        engine('POST', f'/containers/{cid}/stop?t=3')
                    except EngineError as exc:
                        if exc.code not in (304, 404):
                            raise
                elif status in ('exited', 'dead') and now - created > timeout + RETENTION:
                    LOG.info('Removing expired job=%s', labels.get('factory.job_id'))
                    engine('DELETE', f'/containers/{cid}?force=true&v=true')
        except Exception as exc:
            LOG.error('Sweeper error: %s', exc)
        time.sleep(15)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def reply(self, code, content):
        payload = json.dumps(content, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        header = self.headers.get('Authorization', '')
        if not hmac.compare_digest(header, f'Bearer {TOKEN}'):
            raise ApiError(401, 'Authorization: Bearer <token> required')

    def run_route(self, operation):
        try:
            if operation != 'health':
                self.authorized()
            result = self.dispatch()
            if result is not None:
                code, data = result
                self.reply(code, data)
        except ApiError as exc:
            self.reply(exc.code, {'error': exc.message})
        except EngineError as exc:
            LOG.warning('Docker request failed: %s', exc)
            self.reply(502, {'error': 'Docker Engine failed', 'detail': str(exc)[:240]})
        except EngineUnavailable as exc:
            LOG.warning('Docker Engine unavailable: %s', exc)
            self.reply(503, {'error': 'Docker Engine unavailable'})
        except OSError as exc:
            LOG.warning('Local file access failed: %s', exc)
            self.reply(500, {'error': 'Local filesystem access failed',
                             'hint': 'Run sudo ./factory.sh doctor'})
        except Exception:
            LOG.exception('Unexpected server error')
            self.reply(500, {'error': 'Internal server error'})

    def read_json(self):
        value = self.headers.get('Content-Length')
        if value is None or not value.isdigit():
            raise ApiError(411, 'Content-Length required')
        length = int(value)
        if length > 32768:
            raise ApiError(413, 'Request too large')
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, 'Invalid JSON') from None

    def dispatch(self):
        url = urlsplit(self.path)
        path = url.path
        if self.command == 'GET' and path == '/health':
            ping = engine('GET', '/_ping', binary=True)
            return 200, {'service': 'factory-agent-manager', 'docker': ping.decode().strip(), 'status': 'ok'}
        if self.command == 'GET' and path == '/limits':
            return 200, all_limits()
        if self.command == 'POST' and path == '/limits/refresh':
            body = self.read_json()
            if body != {}:
                raise ApiError(400, 'Use an empty JSON body for quota refresh')
            return 200, refresh_limits()
        if self.command == 'POST' and path == '/limits/check':
            body = self.read_json()
            if not isinstance(body, dict) or set(body) - {'provider', 'role'}:
                raise ApiError(400, 'Only provider/role are accepted')
            provider = body.get('provider', 'codex')
            role = body.get('role', 'developer')
            if provider not in PROVIDERS or role not in ROLES or role == 'smoke':
                raise ApiError(400, 'Invalid provider or role')
            return 201, _start_limit_probe(provider, login_profile(provider, role))
        if self.command == 'GET' and path == '/limits/availability':
            query = parse_qs(url.query)
            provider, role = query.get('provider', ['codex'])[0], query.get('role', ['developer'])[0]
            return 200, limit_availability(provider, role)
        if self.command == 'GET' and path == '/roles':
            return 200, {
                'roles': list(ROLES), 'providers': list(PROVIDERS),
                'images': {provider: f'{IMAGE_PREFIX}-{provider}:{IMAGE_TAG}'
                           for provider in PROVIDERS},
                'logins': {provider: {role: {'profile': login_profile(provider, role),
                                            'ready': auth_ready(provider, login_profile(provider, role))}
                                      for role in ROLES if role != 'smoke'}
                           for provider in PROVIDERS},
            }
        if self.command == 'POST' and path == '/jobs':
            return 201, create_job(self.read_json())
        if self.command == 'GET' and path == '/jobs':
            jobs = []
            for item in docker_list()[:200]:
                labels = item.get('Labels', {})
                jobs.append({'job_id': labels.get('factory.job_id'),
                             'role': labels.get('factory.role'),
                             'provider': labels.get('factory.provider', 'codex'),
                             'docker_status': item.get('State'), 'container_id': item['Id'][:12]})
            return 200, {'jobs': jobs}
        match = re.fullmatch(r'/jobs/([^/]+)(/logs|/cancel)?', path)
        if not match:
            raise ApiError(404, 'Endpoint not found')
        job_id, suffix = match.groups()
        info = inspected(job_id)
        if self.command == 'GET' and not suffix:
            return 200, job_info(info)
        if self.command == 'GET' and suffix == '/logs':
            return 200, {'job_id': job_id, 'logs': container_logs(info['Id'], 200)}
        if self.command == 'POST' and suffix == '/cancel':
            if info.get('State', {}).get('Running'):
                engine('POST', f'/containers/{info["Id"]}/stop?t=3')
            return 200, job_info(inspected(job_id))
        if self.command == 'DELETE' and not suffix:
            engine('DELETE', f'/containers/{info["Id"]}?force=true&v=true')
            return 200, {'job_id': job_id, 'removed': True}
        raise ApiError(405, 'Method not allowed')

    def do_GET(self):
        self.run_route('health' if urlsplit(self.path).path == '/health' else 'api')

    def do_POST(self):
        self.run_route('api')

    def do_DELETE(self):
        self.run_route('api')

    def log_message(self, format, *args):
        LOG.info('%s %s', self.client_address[0], format % args)


def main():
    global HOST_AUTH_ROOT, HOST_WORKSPACE_ROOT
    if (len(TOKEN) < 32 or not re.fullmatch(r'[0-9a-fA-F]+', TOKEN) or
            NETWORK_MODE != EGRESS_NETWORK or
            not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', EGRESS_NETWORK) or
            not HOST_AUTH_ROOT.is_absolute() or not HOST_WORKSPACE_ROOT.is_absolute() or
            str(HOST_AUTH_ROOT) == '/' or str(HOST_WORKSPACE_ROOT) == '/' or
            not WORKSPACE_ROOT.is_dir() or
            CODEX_SANDBOX not in ('read-only', 'workspace-write') or
            CLAUDE_WRITE != 'acceptEdits' or GEMINI_WRITE != 'auto_edit' or
            not re.fullmatch(r'[a-zA-Z0-9_-]{1,50}', OWNER) or
            any(profile not in AUTH_PROFILES
                for provider_profiles in ROLE_PROFILE.values()
                for profile in provider_profiles.values())):
        raise RuntimeError('Invalid manager configuration: check .env token, bind paths, egress network and sandbox')
    if (MAX_CONCURRENT < 1 or MAX_CONCURRENT > 16 or MEMORY_MB < 256 or
            PIDS_LIMIT < 32 or CPUS <= 0 or RETENTION < 60 or
            DEFAULT_TIMEOUT < 10 or MAX_TIMEOUT < DEFAULT_TIMEOUT or
            LIMIT_UNKNOWN_POLICY not in ('block', 'allow') or
            LIMIT_REFRESH_SEC < 30 or LIMIT_REFRESH_SEC > 86400 or
            LIMIT_CHECK_TIMEOUT_SEC < 8 or LIMIT_CHECK_TIMEOUT_SEC > 35 or
            LIMIT_MAX_PROBES < 1 or LIMIT_MAX_PROBES > 8 or
            LIMIT_MIN_REMAINING_PERCENT < 0 or LIMIT_MIN_REMAINING_PERCENT > 50 or
            not re.fullmatch(r'[a-zA-Z0-9_,.-]{1,128}', LIMIT_CODEX_BUCKETS)):
        raise RuntimeError('Invalid manager capacity/timeout configuration in .env')
    HOST_AUTH_ROOT, HOST_WORKSPACE_ROOT = daemon_mount_paths()
    LOG.info('Docker daemon bind mount roots resolved: auth=%s, workspaces=%s',
             HOST_AUTH_ROOT, HOST_WORKSPACE_ROOT)
    threading.Thread(target=sweeper, daemon=True, name='job-sweeper').start()
    server = ThreadingHTTPServer(('0.0.0.0', MANAGER_PORT), Handler)
    LOG.info('Agent manager listening port=%s, owner=%s, max_concurrent=%s',
             MANAGER_PORT, OWNER, MAX_CONCURRENT)
    server.serve_forever()


if __name__ == '__main__':
    main()
