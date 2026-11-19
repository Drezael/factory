"""Read the URL-only allowlist of monitored GitHub repositories.

A file in repos/ is one URL, NOT a configuration mapping. Only approved URLs
are resolved through the authenticated GitHub API. Deleting a file removes the
repo from the allowlist on the next scan and prevents further worker dispatch.
"""
from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import quote, urlsplit

from bootstrap import config_for

URL_MAX_BYTES = 512
REGISTRY_FILE_EXT = '.repo'
SLUG = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')


def parse_url(text, path):
    if not isinstance(text, str) or not 1 <= len(text.encode('utf-8')) <= URL_MAX_BYTES:
        raise ValueError(f'{path}: repository URL is empty or too long')
    value = text.strip()
    if any(x.isspace() for x in value):
        raise ValueError(f'{path}: exactly one repository URL per file')
    url = urlsplit(value)
    if (url.scheme.lower() != 'https' or (url.hostname or '').lower() != 'github.com'
            or url.port is not None or url.username or url.password or url.query or url.fragment):
        raise ValueError(f'{path}: use https://github.com/OWNER/REPO (no query or fragment)')
    raw_parts = url.path.strip('/').split('/')
    if len(raw_parts) != 2:
        raise ValueError(f'{path}: URL must identify exactly one GitHub repository')
    owner, name = raw_parts
    if name.endswith('.git'):
        name = name[:-4]
    slug = owner + '/' + name
    if (not SLUG.fullmatch(slug) or '..' in slug or
            owner.startswith('.') or name.startswith('.') or
            owner.endswith('.') or name.endswith('.')):
        raise ValueError(f'{path}: invalid GitHub repository path')
    return slug


def parse_allowlist(gh, registry_slug, ref, directory, *, parse_config,
                    validate_slug, max_files=250):
    owner, repo = validate_slug(registry_slug).split('/')
    if (not directory or not re.fullmatch(r'[A-Za-z0-9_./-]+', directory)
            or '..' in directory.split('/') or directory.startswith('/') or directory.endswith('/')):
        raise ValueError('Invalid registry directory')
    base = f'repos/{quote(owner,safe="")}/{quote(repo,safe="")}/contents/'
    entries, _ = gh.get(base + quote(directory, safe='/'), query={'ref': ref})
    if not isinstance(entries, list) or len(entries) > max_files:
        raise ValueError('Registry directory invalid or too large')
    configs = []
    used = set()
    for entry in entries:
        file_name = entry.get('name', '')
        if not file_name.endswith(REGISTRY_FILE_EXT):
            continue
        if entry.get('type') != 'file':
            raise ValueError(f'{file_name}: registry entry must be a regular file')
        path = entry.get('path', '')
        if (path != directory + '/' + file_name or '/' in file_name or
                not re.fullmatch(r'[A-Za-z0-9_.-]+\.repo', file_name)):
            raise ValueError('Unsafe registry file path')
        if entry.get('size', 0) > URL_MAX_BYTES:
            raise ValueError(f'{path}: repository URL file too large')
        content, _ = gh.get(base + quote(path, safe='/'), query={'ref': ref})
        if content.get('encoding') != 'base64' or not isinstance(content.get('content'), str):
            raise ValueError(f'{path}: expected base64 GitHub file contents')
        try:
            raw = base64.b64decode(content['content'], validate=False)
            text = raw.decode('utf-8')
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError(f'{path}: file is not valid UTF-8') from exc
        repo_slug = parse_url(text, path)
        if repo_slug.lower() == registry_slug.lower():
            raise ValueError(f'{path}: the registry cannot monitor itself')
        if repo_slug.lower() in used:
            raise ValueError(f'{path}: duplicate monitored repository: {repo_slug}')
        used.add(repo_slug.lower())
        parts = repo_slug.split('/')
        metadata, _ = gh.get('repos/' + '/'.join(quote(x, safe='') for x in parts))
        if metadata.get('full_name', '').lower() != repo_slug.lower():
            raise ValueError(f'{path}: repository unavailable or identity mismatch')
        if metadata.get('archived') or metadata.get('disabled') or metadata.get('has_issues') is False:
            raise ValueError(f'{path}: repository is disabled, archived or Issues are disabled')
        kind = (metadata.get('owner') or {}).get('type')
        if kind not in ('Organization', 'User'):
            raise ValueError(f'{path}: unknown GitHub owner type')
        config = parse_config(config_for(metadata, 'organization' if kind == 'Organization' else 'user'), path)
        config['registry_path'] = path
        configs.append(config)
    return configs
