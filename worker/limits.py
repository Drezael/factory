#!/usr/bin/env python3
"""Read provider quota without invoking a model turn.

Emits exactly one FACTORY_RESULT JSON line for the Docker manager. Codex uses
its documented app-server JSON-RPC rateLimits/read method. Claude / Gemini
only expose interactive status views, so their TUI probes are best-effort and
unknown remains unknown (never fake a quota percentage).
"""
import json
import os
import pty
import queue
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

PROVIDER = os.environ.get('FACTORY_LIMIT_PROVIDER', '')
TIMEOUT = min(35, max(8, int(os.environ.get('FACTORY_LIMIT_CHECK_TIMEOUT_SEC', '25'))))
MIN_REMAIN = min(50.0, max(0.0, float(os.environ.get('FACTORY_LIMIT_MIN_REMAINING_PERCENT', '5'))))


def report(status, **extra):
    result = {'status': status, 'provider': PROVIDER, 'checked_at': int(time.time()),
              'method': ('codex_app_server' if PROVIDER == 'codex' else 'interactive_cli'),
              'model_calls': 0}
    result.update(extra)
    print('FACTORY_RESULT=' + json.dumps(result, ensure_ascii=False, separators=(',', ':')), flush=True)


def parse_codex_limits(result):
    """Parse exactly the fields returned by Codex (used%, reset timestamp).

    Percent is not an exact number of tokens or guaranteed remaining requests.
    Other model-specific buckets are included in diagnostics, but gating uses
    the primary 'codex' bucket by default.
    """
    if not isinstance(result, dict):
        return {'status': 'unknown', 'reason': 'invalid_codex_response'}
    buckets = result.get('rateLimitsByLimitId') or {}
    if not isinstance(buckets, dict):
        buckets = {}
    primary = result.get('rateLimits')
    if isinstance(primary, dict) and primary.get('limitId') not in buckets:
        buckets[primary.get('limitId') or 'codex'] = primary
    watched = [s.strip() for s in os.environ.get('FACTORY_LIMIT_CODEX_BUCKETS', 'codex').split(',') if s.strip()]
    all_buckets = {}
    for key, raw in buckets.items():
        if not isinstance(raw, dict):
            continue
        windows = []
        for kind in ('primary', 'secondary'):
            value = raw.get(kind)
            if not isinstance(value, dict):
                continue
            pct = value.get('usedPercent')
            if not isinstance(pct, (float, int)) or isinstance(pct, bool) or pct < 0 or pct > 100:
                continue
            windows.append({'window': kind, 'used_percent': round(float(pct), 2),
                            'remaining_percent': round(100.0-float(pct), 2),
                            'window_minutes': value.get('windowDurationMins'),
                            'resets_at': value.get('resetsAt')})
        all_buckets[str(key)] = {'windows': windows,
                                 'reached_type': raw.get('rateLimitReachedType')}
    chosen = [all_buckets[x] for x in watched if x in all_buckets]
    if len(chosen) != len(watched) or not chosen or any(not bucket['windows'] for bucket in chosen):
        status = 'unknown'
        reason = 'requested_limit_bucket_missing'
    elif any(bucket['reached_type'] or any(w['remaining_percent'] <= MIN_REMAIN
                                         for w in bucket['windows']) for bucket in chosen):
        status = 'exhausted'
        reason = 'rate_limit_reached_or_reserve_threshold'
    else:
        status = 'available'
        reason = 'below_threshold'
    return {'status': status, 'reason': reason, 'buckets': all_buckets,
            'monitored_buckets': watched, 'minimum_remaining_percent': MIN_REMAIN}


def codex():
    p = subprocess.Popen(['codex', 'app-server'], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         encoding='utf-8', errors='replace', bufsize=1)
    lines = queue.Queue()
    def read_lines():
        for line in p.stdout:
            lines.put(line)
        lines.put(None)
    threading.Thread(target=read_lines, daemon=True).start()
    def send(msg):
        p.stdin.write(json.dumps(msg, separators=(',', ':'))+'\n')
        p.stdin.flush()
    try:
        send({'method': 'initialize', 'id': 1, 'params': {
            'clientInfo': {'name': 'factory_quota_probe', 'title': 'Factory Quota Probe', 'version': '1.0.0'}}})
        deadline = time.monotonic() + TIMEOUT
        initialized = False
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=min(1, max(0.01, deadline-time.monotonic())))
            except queue.Empty:
                continue
            if line is None:
                return {'status': 'unknown', 'reason': 'codex_app_server_exited'}
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get('id') == 1:
                if msg.get('error'):
                    return {'status': 'unknown', 'reason': 'codex_initialization_error'}
                initialized = True
                send({'method': 'initialized', 'params': {}})
                send({'method': 'account/rateLimits/read', 'id': 2, 'params': {}})
            elif msg.get('id') == 2 and initialized:
                if msg.get('error'):
                    return {'status': 'unknown', 'reason': 'codex_rate_limits_error'}
                return parse_codex_limits(msg.get('result', {}))
        return {'status': 'unknown', 'reason': 'codex_rate_limits_timeout'}
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=2)


# Strip control sequences from interactive CLI output; never return raw screen
# content because it can include local account identifiers.
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)')


def interactive(command, slash):
    master, slave = pty.openpty()
    try:
        import fcntl
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 42, 160, 0, 0))
        env = dict(os.environ, TERM='xterm-256color', CI='0', NO_COLOR='1')
        p = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env,
                             start_new_session=True, close_fds=True)
        os.close(slave)
        slave = -1
        output = bytearray()
        deadline = time.monotonic() + TIMEOUT
        command_sent = False
        sent_at = None
        # Only type into a detected interactive prompt. If the CLI is in an
        # onboarding/login dialog, return unknown instead of sending a model prompt.
        while time.monotonic() < deadline and len(output) < 600_000:
            readable, _, _ = select.select([master], [], [], 0.4)
            if readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                output.extend(data)
            cleaned = ANSI.sub('', output.decode('utf-8', errors='replace'))
            if not command_sent and (re.search(r'(?m)(?:^|\n)\s*[❯›>]\s*(?:\x00|$)', cleaned[-3000:]) or
                                     re.search(r'(?i)(type a message|enter a prompt)', cleaned[-3000:])):
                os.write(master, (slash+'\r').encode())
                command_sent = True
                sent_at = time.monotonic()
            if command_sent and time.monotonic() - sent_at >= 6:
                break
            if p.poll() is not None:
                break
        sanitized = ANSI.sub('', output.decode('utf-8', errors='replace'))
        # A definitive exhausted response is actionable. Otherwise do NOT
        # guess an available quota from TUI layout or percentages.
        exhausted = bool(re.search(r'(?i)(usage limit reached|you(?:\s+have)?\s+(?:hit|reached|exceeded)\s+(?:your\s+)?(?:usage|rate|quota)\s+limit|capacity exhausted)', sanitized))
        if not command_sent:
            return {'status': 'unknown', 'reason': 'interactive_prompt_not_detected'}
        if exhausted:
            return {'status': 'exhausted', 'reason': 'provider_reports_limit_reached'}
        return {'status': 'unknown', 'reason': 'no_stable_machine_readable_quota',
                'usage_command': slash}
    finally:
        try:
            if 'p' in locals() and p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait(timeout=2)
        except (ProcessLookupError, OSError):
            pass
        os.close(master)
        if slave != -1:
            os.close(slave)


def main():
    try:
        if PROVIDER == 'codex':
            result = codex()
        elif PROVIDER == 'claude':
            result = interactive(['claude'], '/usage')
        elif PROVIDER == 'gemini':
            result = interactive(['gemini'], '/model')
        else:
            result = {'status': 'unknown', 'reason': 'unsupported_provider'}
    except (OSError, ValueError, RuntimeError, BrokenPipeError) as exc:
        result = {'status': 'unknown', 'reason': 'probe_error', 'error_type': type(exc).__name__}
    report(**result)


if __name__ == '__main__':
    main()
