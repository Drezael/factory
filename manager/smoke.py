"""Run inside agent-manager, test actual Docker spawn without AI credentials."""
import json
import os
import time
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

PORT = os.environ.get('FACTORY_AGENT_MANAGER_PORT', '8080')
TOKEN = os.environ['FACTORY_AGENT_API_TOKEN']
BASE = f'http://127.0.0.1:{PORT}'


def api(path, method='GET', data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(BASE + path, method=method, data=body,
                  headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        print('API error', exc.status, exc.read().decode())
        raise


provider = sys.argv[1] if len(sys.argv) > 1 else 'codex'
if provider not in ('codex', 'claude', 'gemini'):
    raise SystemExit('Unknown provider')
print('Spawning smoke agent for ' + provider + '...', flush=True)
job = api('/jobs', 'POST', {'provider': provider, 'role': 'smoke', 'task': 'Verify runtime tools'})
print('job_id:', job['job_id'], flush=True)
for i in range(45):
    status = api('/jobs/' + job['job_id'])
    if status['status'] not in ('running', 'pending'):
        print(json.dumps(status, ensure_ascii=False, indent=2))
        raise SystemExit(0 if status['status'] == 'succeeded' else 1)
    time.sleep(2)
raise SystemExit('Smoke agent did not finish within 90 seconds; inspect with ./factory.sh agents')
