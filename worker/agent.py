"""One-shot multi-provider worker; each invocation has its own container/workspace.

Roles are a trusted allowlist. Provider CLI authentication is the corresponding
dedicated bind-mounted login directory at /auth; NO model API keys are passed here.
"""
import base64
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys

WORKSPACE = Path('/workspace')
ROLES = {
    'planner': 'Act as a software planner. Break the requirement into distinct features and ordered, testable implementation tasks; DO NOT modify files or create GitHub Issues directly.',
    'requirements': 'Act as a requirements engineer: produce testable acceptance criteria, edge cases and assumptions. Do not modify files.',
    'architect': 'Act as a software architect: propose an implementable architecture, interfaces, risks and tests. Do not modify files.',
    'developer': 'Implement the request in working, maintainable code, add tests and document changed files. Do not commit or push.',
    'tester': 'Analyze the existing tests, create or correct tests, and report checks and their results. Do not commit or push.',
    'reviewer': 'You are an independent QA reviewer. Inspect the actual code, tests, and requested behavior. Do NOT modify files. Return JSON with review pass/fail and concrete findings.',
    'security': 'Audit defensively for exploitable flaws, secrets and insecure dependencies. Report remediation, do not modify files.',
}
PROVIDERS = ('codex', 'claude', 'gemini')
COMMANDS = {'codex': 'codex', 'claude': 'claude', 'gemini': 'gemini'}


def result(status, message, provider=None, question=None, review=None, findings=None, plan=None):
    data = {
        'status': status,
        'provider': provider or os.getenv('FACTORY_AGENT_PROVIDER', 'codex'),
        'role': os.getenv('FACTORY_AGENT_ROLE', 'unknown'),
        'summary': str(message)[:5000],
    }
    if status == 'needs_input' and question:
        data['question'] = str(question)[:2200]
    if plan is not None:
        data['plan'] = plan
    if review in ('pass','fail'):
        data['review'] = review
        data['findings'] = findings or []
    print('FACTORY_RESULT=' + json.dumps(data, ensure_ascii=False), flush=True)


def invoke(cmd, cwd, *, prompt=None, timeout=None, capture=False):
    return subprocess.run(cmd, cwd=str(cwd), input=prompt, text=True,
                          check=False, timeout=timeout, capture_output=capture)


def ensure_login(provider):
    home = Path('/auth')
    if provider == 'codex':
        try:
            session = json.loads((home / 'auth.json').read_text(encoding='utf-8'))
            if session.get('auth_mode') != 'chatgpt':
                return False
        except (OSError, ValueError, TypeError):
            return False
        r = invoke(['codex', 'login', 'status'], WORKSPACE, timeout=30, capture=True)
        return r.returncode == 0
    if provider == 'claude':
        if not (home / '.factory-login-ready').is_file():
            return False
        r = invoke(['claude', 'auth', 'status'], WORKSPACE, timeout=30, capture=True)
        if r.returncode != 0:
            return False
        try:
            auth = json.loads(r.stdout)
            return auth.get('authMethod') not in ('none', 'api_key', None)
        except (ValueError, TypeError):
            # Some CLI versions render human-readable status; exit 0 is the
            # CLI's documented success indication, authenticated by login step.
            return True
    if provider == 'gemini':
        # Gemini has no supported `auth status` CLI in the headless interface.
        # The next real CLI invocation verifies and may refresh this OAuth login.
        return (home / '.gemini' / 'oauth_creds.json').is_file()
    return False


def prompt_for(role, task, repo):
    source = 'The checked out Git repository is /workspace/project.' if repo else \
        'Create or analyze a new project in /workspace/project.'
    extra = ''
    if role == 'reviewer':
        extra = ('\nReturn ONLY JSON: {"factory_status":"completed","summary":"review summary",'
                 '"review":"pass","findings":[]} or '
                 '{"factory_status":"completed","summary":"review summary",'
                 '"review":"fail","findings":[{"title":"Defect",'
                 '"detail":"Reproduction steps and expected behavior","path":"src/..."}]}. '
                 'If essential information is missing, return factory_status needs_input and a question. '
                 'Never approve without inspecting the actual code; never edit files.')
    if role == 'planner':
        extra = ('\nReturn ONLY valid JSON. If required information is missing: '
            '{"factory_status":"needs_input","question":"Concrete question",'
            '"summary":"Why it is needed"}. Otherwise return '
            '{"factory_status":"completed","summary":"Concise implementation plan",'
            '"plan":{"features":[{"key":"feature-key","title":"Feature title",'
            '"description":"Feature description","tasks":[{"key":"unique-task-key",'
            '"title":"Actionable task title","description":"Implementation step",'
            '"acceptance_criteria":["Testable expected result"],'
            '"depends_on":[]}]}]}}. Create 1..12 features and up to 40 total tasks, '
            'with unique lowercase-hyphenated keys. Dependencies reference task keys; '
            'use a DAG (no circular dependencies). Group requirements into small actionable steps. '
            'Do not implement now. Do not claim the work is complete. '
            'The Factory creates GitHub Issues ONLY after your plan is verified. '
            'No feature/task is released before the Planning Issue is completed.')
    return f'{ROLES[role]}\n\nUSER TASK:\n{task}\n\n{source}\nNever read or disclose credentials from /auth.{extra}'


def parse_final_message(summary, *, require_structured=False, role=None):
    """Do not mistake a free-form question for a successful GitHub task."""
    def pack(state, text, question=None, review=None, findings=None):
        return ((state,text,question,review,findings) if role == 'reviewer'
                else (state,text,question))
    body = str(summary or '').strip()
    if body.startswith('```') and body.endswith('```'):
        body = '\n'.join(body.splitlines()[1:-1]).strip()
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        state = parsed.get('factory_status')
        report = parsed.get('summary')
        if state == 'needs_input':
            question = parsed.get('question')
            if isinstance(question, str) and 8 <= len(question.strip()) <= 2200:
                return pack('needs_input',(report if isinstance(report, str) else 'Rueckfrage')[:5000], question.strip())
        if state == 'completed' and isinstance(report, str) and report.strip():
            if role == 'planner':
                plan = parsed.get('plan')
                if not isinstance(plan,dict) or not isinstance(plan.get('features'),list):
                    return ('error','Planner must provide a structured features/tasks plan',None,None)
                try:
                    if len(json.dumps(plan,ensure_ascii=False).encode()) > 24000:
                        raise ValueError('Plan exceeds size limit')
                except (ValueError,TypeError):
                    return ('error','Planner plan is too large or invalid',None,None)
                return ('ok',report[:5000],None,plan)
            if role == 'reviewer':
                verdict = parsed.get('review')
                findings = parsed.get('findings', [])
                if verdict not in ('pass','fail') or not isinstance(findings,list) or len(findings)>20:
                    return pack('error','Reviewer must return review: pass or fail and findings array')
                if verdict=='fail' and not findings:
                    return pack('error','A failed review requires actionable findings')
                if verdict=='pass' and findings:
                    return pack('error','A passing review must have no findings')
                valid=[]
                for finding in findings:
                    if not isinstance(finding,dict) or set(finding)-{'title','detail','path'}:
                        return pack('error','Invalid reviewer finding object')
                    title,detail = finding.get('title'),finding.get('detail')
                    if not isinstance(title,str) or not 4<=len(title)<=180 or not isinstance(detail,str) or not 8<=len(detail)<=2000:
                        return pack('error','Invalid reviewer finding title/detail')
                    if finding.get('path') is not None and (not isinstance(finding['path'],str) or len(finding['path'])>300):
                        return pack('error','Invalid reviewer finding path')
                    valid.append({'title':title,'detail':detail,'path':finding.get('path','')})
                return pack('ok',report[:5000],None,verdict,valid)
            return pack('ok',report[:5000])
    if require_structured:
        return pack('error','Agent response violates required factory_status JSON protocol')
    return pack('ok',str(summary)[:5000])


def gemini_isolated_session():
    """Copy ONLY login credentials into a job-private tmpfs home, never chat history.

    Gemini stores chats in ~/.gemini/tmp. Its profile under /auth can be
    long-lived for OAuth refresh; the active CLI home is fresh each run.
    """
    root = Path('/tmp/factory-gemini-job-home')
    home = root / '.gemini'
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    original = Path('/auth/.gemini')
    for filename in ('oauth_creds.json', 'settings.json', 'google_accounts.json'):
        source = original / filename
        if source.is_file() and not source.is_symlink():
            target = home / filename
            shutil.copyfile(source, target)
            target.chmod(0o600)
    return root


def sync_gemini_oauth(root):
    """Persist refreshed OAuth token, NOT the per-run session or agent history."""
    source = root / '.gemini' / 'oauth_creds.json'
    target = Path('/auth/.gemini/oauth_creds.json')
    if source.is_file() and not source.is_symlink() and target.is_file() and not target.is_symlink():
        content = source.read_bytes()
        if content and len(content) <= 200_000:
            # Keep the existing auth volume; all other files stay in tmpfs.
            target.write_bytes(content)
            target.chmod(0o600)


def call_model(provider, role, prompt, project):
    writing = role in ('developer', 'tester')
    if provider == 'codex':
        sandbox = 'workspace-write' if writing else 'read-only'
        if writing:
            override = os.getenv('FACTORY_CODEX_SANDBOX', 'workspace-write')
            if override in ('workspace-write', 'read-only'):
                sandbox = override
        output = WORKSPACE / 'last-message.txt'
        cmd = ['codex', 'exec', '--ephemeral', '--ignore-user-config',
               '--sandbox', sandbox, '--skip-git-repo-check',
               '--output-last-message', str(output), '-']
        completed = invoke(cmd, project, prompt=prompt)
        summary = output.read_text(encoding='utf-8') if output.is_file() else 'No final Codex message; inspect logs'
        return completed.returncode, summary
    if provider == 'claude':
        permission = os.getenv('FACTORY_CLAUDE_PERMISSION_WRITE', 'acceptEdits') if writing else 'plan'
        cmd = ['claude', '-p', '--output-format', 'json', '--no-session-persistence',
               '--permission-mode', permission, '--permission-prompts', 'none', '--max-turns', '24']
        completed = invoke(cmd, project, prompt=prompt, capture=True)
        try:
            parsed = json.loads(completed.stdout)
            summary = parsed.get('result') or parsed.get('error') or 'No final Claude response'
            if parsed.get('is_error'):
                return completed.returncode or 1, summary
        except (ValueError, TypeError):
            summary = (completed.stdout or 'Claude did not emit JSON')[-5000:]
        return completed.returncode, summary
    if provider == 'gemini':
        mode = os.getenv('FACTORY_GEMINI_APPROVAL_WRITE', 'auto_edit') if writing else 'plan'
        cmd = ['gemini', '--prompt', prompt, '--approval-mode', mode, '--skip-trust',
               '--output-format', 'json']
        isolated = gemini_isolated_session()
        previous = os.environ.get('GEMINI_CLI_HOME')
        os.environ['GEMINI_CLI_HOME'] = str(isolated)
        try:
            completed = invoke(cmd, project, capture=True)
        finally:
            if previous is None:
                os.environ.pop('GEMINI_CLI_HOME', None)
            else:
                os.environ['GEMINI_CLI_HOME'] = previous
            sync_gemini_oauth(isolated)
        try:
            parsed = json.loads(completed.stdout)
            summary = parsed.get('response') or parsed.get('error') or 'No final Gemini response'
            if parsed.get('error'):
                return completed.returncode or 1, summary
        except (ValueError, TypeError):
            summary = (completed.stdout or 'Gemini did not emit JSON')[-5000:]
        return completed.returncode, summary
    raise ValueError('Unknown provider')


def main():
    provider = os.getenv('FACTORY_AGENT_PROVIDER', 'codex')
    role = os.getenv('FACTORY_AGENT_ROLE', '')
    if provider not in PROVIDERS:
        result('error', 'Unknown provider')
        return 2
    if role == 'smoke':
        versions = []
        for cmd in (['python3', '--version'], ['node', '--version'], ['git', '--version'],
                    [COMMANDS[provider], '--version']):
            proc = invoke(cmd, WORKSPACE, capture=True, timeout=15)
            if proc.returncode:
                result('error', f'Missing tool: {cmd[0]}')
                return 2
            versions.append((proc.stdout or proc.stderr).strip()[:120])
        result('ok', 'Worker container started: ' + ', '.join(versions))
        return 0
    if role not in ROLES:
        result('error', 'Unknown role')
        return 2
    if not ensure_login(provider):
        result('error', f'No valid {provider} subscription login. Run ./factory.sh auth-login {provider} default')
        return 2

    task = base64.b64decode(os.environ['FACTORY_AGENT_TASK_B64'], validate=True).decode('utf-8')
    repo = os.getenv('FACTORY_AGENT_REPOSITORY', '')
    ref = os.getenv('FACTORY_AGENT_REF', '')
    project = WORKSPACE / 'project'
    if repo:
        cmd = ['git', 'clone', '--depth', '1']
        if ref:
            cmd.extend(['--branch', ref, '--single-branch'])
        cmd.extend(['--', 'https://github.com/' + repo + '.git', str(project)])
        print('Cloning GitHub repository:', repo, flush=True)
        clone = invoke(cmd, WORKSPACE, timeout=180)
        if clone.returncode:
            result('error', 'Git clone failed; inspect logs')
            return clone.returncode
    else:
        project.mkdir(parents=True, exist_ok=True)
    print(f'Starting provider={provider}, role={role}', flush=True)
    code, summary = call_model(provider, role, prompt_for(role, task, bool(repo)), project)
    if code == 0:
        strict = os.getenv('FACTORY_AGENT_STRUCTURED_RESULT', 'false').lower() == 'true'
        parsed = parse_final_message(summary, require_structured=strict, role=role)
        status, description, question = parsed[:3]
        review, findings = parsed[3:] if role == 'reviewer' and len(parsed) == 5 else (None,None)
        plan = parsed[3] if role == 'planner' and len(parsed) == 4 else None
        result(status, description, question=question, review=review, findings=findings, plan=plan)
        if status == 'error':
            return 2
    else:
        result('error', summary)
    return code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        print('Worker failed:', type(exc).__name__, file=sys.stderr)
        result('error', f'Worker error: {type(exc).__name__}')
        sys.exit(1)
