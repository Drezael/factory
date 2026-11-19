#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -f .env || ! -f compose.yaml || ! -f .env.example ]]; then
  echo 'Fehler: .env, .env.example oder compose.yaml fehlt.' >&2
  exit 1
fi
command -v docker >/dev/null || { echo 'Docker nicht gefunden.' >&2; exit 1; }

# The generated .env is the sole source of truth for the Factory.
# Keep old FACTORY_DB_* and newer POSTGRES_* aliases in sync. Never replace
# an existing DB password or n8n encryption key with a newly generated one.
env_value_from() {
  local file=$1 key=$2 line found=0 result=''
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    if [[ "$line" == "$key="* ]]; then
      result=${line#*=}
      found=1
    fi
  done < "$file"
  [[ $found -eq 1 ]] || return 1
  printf '%s' "$result"
}
env_value() { env_value_from .env "$1"; }
env_unquote() {
  local value=$1
  if [[ ${#value} -ge 2 ]]; then
    if [[ ( "${value:0:1}" == '"' && "${value: -1}" == '"' ) || ( "${value:0:1}" == "'" && "${value: -1}" == "'" ) ]]; then
      value=${value:1:${#value}-2}
    fi
  fi
  printf '%s' "$value"
}
new_hex_secret() { od -An -N 32 -tx1 /dev/urandom | tr -d ' \n'; }
factory_name=$(env_value FACTORY_NAME 2>/dev/null || printf 'factory')
[[ "$factory_name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || { echo 'Ungültiger FACTORY_NAME.' >&2; exit 2; }

env_backup_created=0
env_repair_count=0
ensure_env_backup() {
  if [[ $env_backup_created -eq 0 ]]; then
    local backup=".env.backup.$(date +%Y%m%d-%H%M%S).$$"
    cp -p -- .env "$backup"
    chmod 600 -- "$backup"
    env_backup_created=1
    echo "Sicherung: $backup"
  fi
}
set_env_value() {
  local key=$1 value=$2 line found=0 tmp
  local current
  if current=$(env_value "$key" 2>/dev/null) && [[ "$current" == "$value" ]]; then
    return 0
  fi
  ensure_env_backup
  tmp=".env.tmp.$$"
  cp -p -- .env "$tmp"
  {
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == "$key="* ]]; then
        if [[ $found -eq 0 ]]; then printf '%s=%s\n' "$key" "$value"; found=1; fi
      else
        printf '%s\n' "$line"
      fi
    done < .env
    if [[ $found -eq 0 ]]; then printf '%s=%s\n' "$key" "$value"; fi
  } > "$tmp"
  chmod 600 -- "$tmp"
  mv -f -- "$tmp" .env
  env_repair_count=$((env_repair_count + 1))
}
reconcile_alias_pair() {
  local primary=$1 legacy=$2 default_value=${3:-} a b
  a=$(env_value "$primary" 2>/dev/null || true)
  b=$(env_value "$legacy" 2>/dev/null || true)
  if [[ -n "$a" && -n "$b" && "$(env_unquote "$a")" != "$(env_unquote "$b")" ]]; then
    echo "Konflikt in .env: $primary und $legacy haben unterschiedliche Werte." >&2
    echo 'Bitte manuell auf den tatsächlich verwendeten Wert angleichen; kein Passwort wurde geändert.' >&2
    return 2
  fi
  if [[ -z "$a" && -z "$b" ]]; then
    if [[ -z "$default_value" ]]; then
      echo "Datenbankpasswort fehlt: $primary / $legacy." >&2
      echo 'Altes Passwort aus der letzten .env-Sicherung wiederherstellen. Keine automatische Passwort-Neuvergabe.' >&2
      return 2
    fi
    a=$default_value
  else
    a=${a:-$b}
  fi
  if [[ "$(env_unquote "$a")" == REPLACE_* || "$(env_unquote "$a")" == CHANGE_ME* ]]; then
    echo "$primary enthält nur einen Platzhalter. Echtes bestehendes Datenbankpasswort eintragen." >&2
    return 2
  fi
  [[ -n "$(env_value "$primary" 2>/dev/null || true)" ]] || set_env_value "$primary" "$a"
  [[ -n "$(env_value "$legacy" 2>/dev/null || true)" ]] || set_env_value "$legacy" "$a"
}
repair_env() {
  local key value host port tz
  # IMPORTANT: DB values must match an already initialized PostgreSQL bind mount.
  reconcile_alias_pair POSTGRES_PASSWORD FACTORY_DB_PASSWORD || return 2
  reconcile_alias_pair POSTGRES_DB FACTORY_DB_NAME n8n || return 2
  reconcile_alias_pair POSTGRES_USER FACTORY_DB_USER n8n || return 2

  [[ -n "$(env_value FACTORY_GITHUB_AUTOMERGE_ENABLED 2>/dev/null || true)" ]] || set_env_value FACTORY_GITHUB_AUTOMERGE_ENABLED false

  # Preserve settings from older factory deployments if present.
  if [[ -z "$(env_value FACTORY_HTTP_PORT 2>/dev/null || true)" ]]; then
    port=$(env_value FACTORY_PORT 2>/dev/null || printf '5678')
    set_env_value FACTORY_HTTP_PORT "$port"
  fi
  if [[ -z "$(env_value N8N_HOST 2>/dev/null || true)" ]]; then
    host=$(env_value FACTORY_HOST 2>/dev/null || printf 'localhost')
    set_env_value N8N_HOST "$host"
  fi
  tz=$(env_value FACTORY_TIMEZONE 2>/dev/null || printf 'Europe/Berlin')
  [[ -n "$(env_value GENERIC_TIMEZONE 2>/dev/null || true)" ]] || set_env_value GENERIC_TIMEZONE "$tz"
  [[ -n "$(env_value TZ 2>/dev/null || true)" ]] || set_env_value TZ "$tz"
  host=$(env_value N8N_HOST)
  port=$(env_value FACTORY_HTTP_PORT)
  [[ -n "$(env_value N8N_EDITOR_BASE_URL 2>/dev/null || true)" ]] || set_env_value N8N_EDITOR_BASE_URL "http://$host:$port/"
  [[ -n "$(env_value N8N_WEBHOOK_URL 2>/dev/null || true)" ]] || set_env_value N8N_WEBHOOK_URL "http://$host:$port/"

  # n8n requires the SAME encryption key to decrypt saved credentials.
  value=$(env_value N8N_ENCRYPTION_KEY 2>/dev/null || true)
  if [[ -z "$value" || "$value" == REPLACE_* || "$value" == CHANGE_ME* ]]; then
    echo 'N8N_ENCRYPTION_KEY fehlt oder ist ein Platzhalter. Bitte alten Wert aus Backup wiederherstellen.' >&2
    return 2
  fi
  value=$(env_value FACTORY_AGENT_API_TOKEN 2>/dev/null || true)
  if [[ -z "$value" || "$value" == REPLACE_* || "$value" == CHANGE_ME* ]]; then
    set_env_value FACTORY_AGENT_API_TOKEN "$(new_hex_secret)"
    echo 'Neuer interner Agent-Manager-Token erstellt (bestehende n8n Credentials ggf. anpassen).'
  fi

  # Add any missing non-secret v5 settings from the current complete template.
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[A-Z][A-Z0-9_]*= ]] || continue
    key=${line%%=*}; value=${line#*=}
    if [[ -z "$(env_value "$key" 2>/dev/null || true)" ]]; then
      case "$key" in
        POSTGRES_PASSWORD|FACTORY_DB_PASSWORD|N8N_ENCRYPTION_KEY|FACTORY_AGENT_API_TOKEN)
          echo "Sicherheitsvariable $key fehlt. Bitte vor dem Start prüfen." >&2; return 2 ;;
        *) set_env_value "$key" "$value" ;;
      esac
    fi
  done < .env.example
  if [[ $env_repair_count -gt 0 ]]; then
    echo ".env repariert: $env_repair_count Werte ergänzt/angepasst. Bestehende Geheimnisse wurden beibehalten."
  else
    echo '.env vollständig; Datenbank-Passwort-Aliasse stimmen überein.'
  fi
}

factory_compose() {
  # Ignore an accidental empty or stale shell export: .env takes precedence.
  local db_pass
  db_pass=$(env_value POSTGRES_PASSWORD 2>/dev/null || true)
  if [[ -n "$db_pass" ]]; then
    db_pass=$(env_unquote "$db_pass")
    export POSTGRES_PASSWORD="$db_pass" FACTORY_DB_PASSWORD="$db_pass"
  fi
  docker compose --env-file .env -f compose.yaml "$@"
}

# Docker Compose accepts relative bind paths, but the Docker Engine API used
# by agent-manager needs ABSOLUTE host paths. Resolve .env paths once in Bash;
# exported values take precedence over values in Compose's --env-file.
resolve_bind_path() {
  local variable=$1 path
  path=$(env_value "$variable") || { echo "Missing .env setting: $variable" >&2; exit 2; }
  [[ -n "$path" && "$path" != / ]] || { echo "Unsafe path: $variable" >&2; exit 2; }
  if [[ "$path" != /* ]]; then path="$PWD/$path"; fi
  mkdir -p -- "$path"
  path=$(cd -- "$path" && pwd -P)
  [[ "$path" != / ]] || { echo "Root path cannot be a bind mount" >&2; exit 2; }
  export "$variable=$path"
}
prepare_bind_paths() {
  local variable
  for variable in FACTORY_POSTGRES_DATA_DIR FACTORY_N8N_DATA_DIR FACTORY_N8N_WORKFLOWS_DIR FACTORY_N8N_SYNC_DATA_DIR FACTORY_AUTH_DATA_DIR FACTORY_WORKSPACE_DATA_DIR; do
    resolve_bind_path "$variable"
  done
  chmod 700 -- "$FACTORY_AUTH_DATA_DIR" || true
  chmod 750 -- "$FACTORY_WORKSPACE_DATA_DIR" || true
}
# The manager must create per-job directories inside this bind mount. Docker
# root can be user-namespace mapped to the host login user, whereas running
# this script with sudo would otherwise leave a root-owned 0700 directory.
# Honor optional .env overrides for unusual Docker/userns configurations.
prepare_workspace_permissions() {
  local owner_uid owner_gid existing
  owner_uid=$(env_value FACTORY_WORKSPACE_OWNER_UID 2>/dev/null || true)
  owner_gid=$(env_value FACTORY_WORKSPACE_OWNER_GID 2>/dev/null || true)
  if [[ -z "$owner_uid" || "$owner_uid" == auto ]]; then
    if [[ $EUID -eq 0 && "${SUDO_UID:-}" =~ ^[0-9]+$ ]]; then
      owner_uid=$SUDO_UID
    else
      owner_uid=$(id -u)
    fi
  fi
  if [[ -z "$owner_gid" || "$owner_gid" == auto ]]; then
    if [[ $EUID -eq 0 && "${SUDO_GID:-}" =~ ^[0-9]+$ ]]; then
      owner_gid=$SUDO_GID
    else
      owner_gid=$(id -g)
    fi
  fi
  if [[ ! "$owner_uid" =~ ^[0-9]+$ || ! "$owner_gid" =~ ^[0-9]+$ ]]; then
    echo 'FACTORY_WORKSPACE_OWNER_UID/GID must be integers or auto.' >&2
    return 2
  fi
  existing=$(stat -c '%u:%g' -- "$FACTORY_WORKSPACE_DATA_DIR") || return
  if [[ "$existing" != "$owner_uid:$owner_gid" ]]; then
    if ! chown "$owner_uid:$owner_gid" -- "$FACTORY_WORKSPACE_DATA_DIR"; then
      echo 'Cannot set workspace ownership. Retry with sudo ./factory.sh repair-workspaces' >&2
      return 1
    fi
  fi
  chmod 0750 -- "$FACTORY_WORKSPACE_DATA_DIR" || return
  echo "Workspace directory: $FACTORY_WORKSPACE_DATA_DIR (owner=$owner_uid:$owner_gid, mode=750)"
}

# Run from within the actual manager container: a host-side write check alone
# cannot detect Docker user namespace or bind-mount permission mismatches.
check_manager_workspace() {
  factory_compose exec -T agent-manager python3 - <<'PYCODE'
from pathlib import Path
import os
import socket
import uuid
root = Path('/opt/factory-workspaces')
print('Manager uid/gid:', os.geteuid(), os.getegid(), flush=True)
print('Workspace mount:', str(root), flush=True)
probe = root / ('factory-permission-check-' + uuid.uuid4().hex[:12])
created = False
try:
    probe.mkdir(mode=0o700)
    created = True
    probe.chmod(0o700)
    if os.geteuid() == 0:
        os.chown(probe, 1000, 1000)
    print('Workspace mkdir/chmod/chown: OK', flush=True)
finally:
    if created:
        probe.rmdir()
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.settimeout(5)
    s.connect('/var/run/docker.sock')
    print('Docker socket: OK', flush=True)
finally:
    s.close()
PYCODE
}
prepare_n8n_data() {
  # Both n8n main and workflow-sync run as UID 1000, each with its own bind
  # directory. Do not run importer as root or give it the Docker socket.
  local image dir
  image=$(env_value N8N_IMAGE)
  for dir in "$FACTORY_N8N_DATA_DIR" "$FACTORY_N8N_SYNC_DATA_DIR"; do
    docker run --rm --network none --user 0:0 \
      --mount "type=bind,source=$dir,target=/factory-data" \
      --entrypoint /bin/sh "$image" -ec \
      'chown -R 1000:1000 /factory-data && chmod 700 /factory-data'
  done
}

worker_filter="label=factory.owner=$factory_name"
providers=(codex claude gemini)
profiles=(default requirements architect developer tester reviewer security)

valid_provider() {
  local x
  for x in "${providers[@]}"; do [[ "$1" == "$x" ]] && return 0; done
  echo "Provider nicht erlaubt: $1 (codex | claude | gemini)" >&2
  return 2
}
valid_profile() {
  local x
  for x in "${profiles[@]}"; do [[ "$1" == "$x" ]] && return 0; done
  echo "Login-Profil nicht erlaubt: $1" >&2
  return 2
}
parse_provider_profile() {
  local first=${1:-default}
  if [[ "$first" == codex || "$first" == claude || "$first" == gemini ]]; then
    auth_provider=$first
    auth_profile=${2:-default}
  else
    # Backward compatible with v3: ./factory.sh auth-login default
    auth_provider=codex
    auth_profile=$first
  fi
  valid_provider "$auth_provider"
  valid_profile "$auth_profile"
}
worker_image() {
  printf '%s-%s:%s' "$(env_value FACTORY_AGENT_IMAGE_PREFIX)" "$1" "$(env_value FACTORY_AGENT_IMAGE_TAG)"
}
auth_path() { printf '%s/%s/%s' "$FACTORY_AUTH_DATA_DIR" "$1" "$2"; }
profile_active() {
  local provider=$1 profile=$2
  if [[ "$provider" == codex ]] && [[ -n "$(docker ps -q --filter "$worker_filter" --filter "label=factory.codex_profile=$profile" --filter 'label=factory.managed=true')" ]]; then
    return 0
  fi
  [[ -n "$(docker ps -q --filter "$worker_filter" --filter 'label=factory.managed=true' --filter "label=factory.provider=$provider" --filter "label=factory.profile=$profile")" ]]
}
ensure_image() {
  local provider=$1
  if ! docker image inspect "$(worker_image "$provider")" >/dev/null 2>&1; then
    echo "Baue fehlendes Worker-Image: $provider"
    factory_compose --profile agent-images build "image-$provider"
  fi
}
init_profile() {
  local provider=$1 profile=$2 vol
  valid_provider "$provider"; valid_profile "$profile"
  ensure_image "$provider"
  vol=$(auth_path "$provider" "$profile")
  mkdir -p -- "$vol"
  docker run --rm --network none --user 0:0 \
    --mount "type=bind,source=$vol,target=/auth" \
    --entrypoint /bin/sh "$(worker_image "$provider")" -ec \
      'mkdir -p /auth/.cache /auth/.claude /auth/.gemini; chown -R 1000:1000 /auth; chmod 0700 /auth'
  if [[ "$provider" == codex ]]; then
    docker run --rm --network none --user 1000:1000 \
      --mount "type=bind,source=$vol,target=/auth" \
      --entrypoint /bin/sh "$(worker_image "$provider")" -ec \
        'umask 077; test -f /auth/config.toml || printf '\''cli_auth_credentials_store = "file"\n'\'' > /auth/config.toml'
  fi
}
auth_docker() {
  local provider=$1 profile=$2; shift 2
  docker run --rm --network "$(env_value FACTORY_EGRESS_NETWORK)" --user 1000:1000 \
    --read-only --tmpfs /tmp:rw,nosuid,noexec,size=96m \
    --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 128 --memory 1536m --cpus 1 \
    --mount "type=bind,source=$(auth_path "$provider" "$profile"),target=/auth" \
    --env HOME=/auth --env CODEX_HOME=/auth --env CLAUDE_CONFIG_DIR=/auth/.claude \
    --env XDG_CACHE_HOME=/auth/.cache --env DISABLE_AUTOUPDATER=1 \
    --entrypoint "$provider" "$(worker_image "$provider")" "$@"
}
# Gemini executable matches provider. "claude" and "codex" do too.
auth_status() {
  local provider=$1 profile=$2
  if [[ ! -d "$(auth_path "$provider" "$profile")" ]]; then
    echo "Kein $provider-Loginprofil '$profile' vorhanden." >&2; return 1
  fi
  ensure_image "$provider"
  case "$provider" in
    codex) auth_docker "$provider" "$profile" login status ;;
    claude) auth_docker "$provider" "$profile" auth status ;;
    gemini)
      docker run --rm --network none --user 1000:1000 \
        --mount "type=bind,source=$(auth_path "$provider" "$profile"),target=/auth,readonly" \
        --entrypoint /bin/sh "$(worker_image "$provider")" -ec \
        'if test -s /auth/.gemini/oauth_creds.json; then echo "Gemini OAuth-Credentials vorhanden (Gültigkeit wird beim ersten Job geprüft)."; else echo "Gemini OAuth-Anmeldung fehlt." >&2; exit 1; fi'
      ;;
  esac
}
stop_workers() {
  local cid
  while IFS= read -r cid; do [[ -z "$cid" ]] || docker stop "$cid" >/dev/null || true; done \
    < <(docker ps -q --filter "$worker_filter" --filter 'label=factory.managed=true')
}
remove_workers() {
  local cid
  while IFS= read -r cid; do [[ -z "$cid" ]] || docker rm -fv "$cid" >/dev/null || true; done \
    < <(docker ps -aq --filter "$worker_filter" --filter 'label=factory.managed=true')
}
build_workers() {
  local p=${1:-all}
  if [[ "$p" == all ]]; then
    factory_compose --profile agent-images build image-codex image-claude image-gemini
  else
    valid_provider "$p"
    factory_compose --profile agent-images build "image-$p"
  fi
}
update_worker() {
  local p=${1:-}
  valid_provider "$p"
  echo "Aktualisiere nur $p (Version aus .env; andere Worker bleiben unverändert)."
  factory_compose --profile agent-images build --pull --no-cache "image-$p"
  echo "Fertig: Neue Jobs nutzen das neue $p-Image. Laufende Jobs bleiben unverändert."
}

# Factory reset operates ONLY on the five DATA bind mounts. Workflow JSON,
# project source, secrets in .env, and Docker images are deliberately retained.
# Extra allowed roots must be explicitly opted into in .env before resetting
# data kept outside ./data (colon-separated directories).
reset_absolute_path() {
  local raw=$1
  [[ -n "$raw" && "$raw" != '~'* && "$raw" != '$'* ]] || {
    echo "Unsafe/unsupported data path: $raw" >&2
    return 2
  }
  if [[ "$raw" != /* ]]; then raw="$PWD/$raw"; fi
  realpath -m -- "$raw"
}
reset_is_child() {
  local path=$1 root=$2
  [[ "$path" == "$root/"* ]]
}
reset_data() {
  local dry_run=0 yes=0 confirm='' value raw root root_path path path_source
  local workflow_path i answer name project_root remaining_workers
  local -a roots=() targets=() labels=() raw_roots=()
  local -a reset_keys=(
    FACTORY_POSTGRES_DATA_DIR
    FACTORY_N8N_DATA_DIR
    FACTORY_N8N_SYNC_DATA_DIR
    FACTORY_AUTH_DATA_DIR
    FACTORY_WORKSPACE_DATA_DIR
  )

  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=1; shift ;;
      --yes) yes=1; shift ;;
      --confirm)
        [[ $# -ge 2 ]] || { echo 'Missing value for --confirm' >&2; return 2; }
        confirm=$2; shift 2 ;;
      *) echo "Unknown reset option: $1" >&2; return 2 ;;
    esac
  done
  if [[ "$dry_run" -eq 1 && ( "$yes" -eq 1 || -n "$confirm" ) ]]; then
    echo '--dry-run cannot be combined with a confirmation.' >&2; return 2
  fi
  if [[ "$yes" -eq 1 && "$confirm" != RESET-FACTORY ]]; then
    echo 'For unattended reset use: reset-data --yes --confirm RESET-FACTORY' >&2; return 2
  fi
  if [[ "$yes" -eq 0 && -n "$confirm" ]]; then
    echo '--confirm requires --yes.' >&2; return 2
  fi
  command -v realpath >/dev/null || { echo 'realpath (coreutils) is required.' >&2; return 2; }
  project_root=$(realpath -m -- "$PWD")

  raw=$(env_value FACTORY_RESET_ALLOWED_ROOTS 2>/dev/null || printf './data')
  [[ -n "$raw" ]] || { echo 'FACTORY_RESET_ALLOWED_ROOTS must not be empty.' >&2; return 2; }
  IFS=':' read -r -a raw_roots <<< "$(env_unquote "$raw")"
  for root in "${raw_roots[@]}"; do
    [[ -n "$root" ]] || { echo 'Empty allowed reset root.' >&2; return 2; }
    root_path=$(reset_absolute_path "$root") || return 2
    # Never authorize a broad system or project root as the deletion boundary.
    case "$root_path" in
      /|/home|/root|/var|/tmp|/mnt|/media|/opt|/srv|/etc|/usr|/run)
        echo "Too broad reset root forbidden: $root_path" >&2; return 2 ;;
    esac
    if [[ "$root_path" == "$project_root" || "$root_path" == "$(dirname -- "$project_root")" || "$root_path" == "${HOME:-/nonexistent}" || "$project_root" == "$root_path/"* ]]; then
      echo "Project/home/parent directory forbidden as reset root: $root_path" >&2
      return 2
    fi
    roots+=("$root_path")
  done

  for name in "${reset_keys[@]}"; do
    value=$(env_value "$name" 2>/dev/null || true)
    [[ -n "$value" ]] || { echo "Missing data path in .env: $name" >&2; return 2; }
    raw=$(env_unquote "$value")
    path_source=$raw
    if [[ "$path_source" != /* ]]; then path_source="$PWD/$path_source"; fi
    if [[ -L "$path_source" ]]; then
      echo "Refusing symlink as reset target: $name ($path_source)" >&2; return 2
    fi
    path=$(reset_absolute_path "$raw") || return 2
    if [[ "$path" == "$project_root" || "$path" == "${HOME:-/nonexistent}" || "$project_root" == "$path/"* ]]; then
      echo "Refusing project/home/ancestor as reset target: $path" >&2; return 2
    fi
    local authorized=0
    for root in "${roots[@]}"; do
      if reset_is_child "$path" "$root"; then authorized=1; break; fi
    done
    if [[ "$authorized" -eq 0 ]]; then
      echo "Reset target outside FACTORY_RESET_ALLOWED_ROOTS: $name = $path" >&2
      echo 'For external data directories, set FACTORY_RESET_ALLOWED_ROOTS=./data:/external/factory-data' >&2
      return 2
    fi
    if [[ -e "$path" && ! -d "$path" ]]; then
      echo "Not a directory: $name = $path" >&2; return 2
    fi
    for i in "${targets[@]}"; do
      if [[ "$path" == "$i" || "$path" == "$i/"* || "$i" == "$path/"* ]]; then
        echo "Overlapping reset data paths are forbidden: $path and $i" >&2
        return 2
      fi
    done
    targets+=("$path")
    labels+=("$name")
  done

  value=$(env_value FACTORY_N8N_WORKFLOWS_DIR 2>/dev/null || printf './n8n-workflows')
  workflow_path=$(reset_absolute_path "$(env_unquote "$value")") || return 2
  for path in "${targets[@]}"; do
    if [[ "$workflow_path" == "$path" || "$workflow_path" == "$path/"* ]]; then
      echo "Workflow source directory must not be inside a reset target: $workflow_path" >&2
      return 2
    fi
  done

  echo "Factory '$factory_name' - reset of bind-mounted DATA only"
  for ((i=0; i<${#targets[@]}; i++)); do
    path=${targets[$i]}
    if [[ -d "$path" ]]; then
      printf '  %-32s %s\n' "${labels[$i]}" "$path"
    else
      printf '  %-32s %s (not present)\n' "${labels[$i]}" "$path"
    fi
  done
  echo 'Preserved: .env, compose.yaml, factory.sh, n8n-workflows/*.json, Docker images.'
  echo 'Lost: n8n users/settings/credentials and database, agent logins, workspaces, sync state.'
  if [[ "$dry_run" -eq 1 ]]; then
    echo 'DRY RUN: no containers stopped and no files removed.'
    return 0
  fi
  if [[ "$yes" -eq 0 ]]; then
    if [[ ! -t 0 ]]; then
      echo 'Interactive confirmation required. For scripts: --yes --confirm RESET-FACTORY' >&2
      return 2
    fi
    read -r -p 'To delete these data permanently, type RESET-FACTORY: ' answer
    if [[ "$answer" != RESET-FACTORY ]]; then
      echo 'Reset cancelled. No changes made.'
      return 1
    fi
  fi

  # Fail closed: if Compose configuration or shutdown fails, DELETE NOTHING.
  factory_compose config --quiet || { echo 'Compose validation failed; no files removed.' >&2; return 1; }
  echo 'Stopping Factory services and managed agent containers ...'
  stop_workers
  remove_workers
  remaining_workers=$(docker ps -aq --filter "$worker_filter" --filter 'label=factory.managed=true') || {
    echo 'Cannot verify worker cleanup; no data was removed.' >&2; return 1;
  }
  if [[ -n "$remaining_workers" ]]; then
    echo 'Managed worker containers remain; no data was removed.' >&2
    return 1
  fi
  factory_compose down --remove-orphans || { echo 'Compose shutdown failed; data was not deleted.' >&2; return 1; }
  # Keep each mount directory itself. Remove contents (including dotfiles),
  # allowing Docker to mount the same host paths on the next start.
  for path in "${targets[@]}"; do
    if [[ -d "$path" ]]; then
      find "$path" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + || {
        echo "Reset incomplete: could not clear $path" >&2
        return 1
      }
      echo "Cleared: $path"
    fi
  done
  echo 'Factory data reset completed. Restart with: sudo ./factory.sh start'
  echo 'On first start, set up n8n again; workflow-sync reimports JSON workflows.'
}

# GitHub Registry commands run INSIDE its private container: never expose port 8090.
github_registry_request() {
  local action=${1:-health}
  case "$action" in
    health) path='/health'; method='GET' ;;
    scan) path='/scan'; method='POST' ;;
    boards/ensure) path='/boards/ensure'; method='POST' ;;
    events) path='/events?status=pending&limit=100'; method='GET' ;;
    *) echo "Unknown GitHub registry action: $action" >&2; return 2 ;;
  esac
  factory_compose exec -T -e FACTORY_REQ_PATH="$path" -e FACTORY_REQ_METHOD="$method" github-registry python -c '
import os, json
from urllib.request import Request, urlopen
from urllib.error import HTTPError
url = "http://127.0.0.1:" + os.environ.get("FACTORY_GITHUB_REGISTRY_PORT", "8090") + os.environ["FACTORY_REQ_PATH"]
req = Request(url, data=b"" if os.environ["FACTORY_REQ_METHOD"] == "POST" else None, method=os.environ["FACTORY_REQ_METHOD"])
try:
    with urlopen(req, timeout=120) as response:
        print(json.dumps(json.load(response), indent=2, ensure_ascii=False))
except HTTPError as err:
    print(f"GitHub Registry HTTP {err.code}: {err.read(2000).decode()}")
    raise SystemExit(1)
'
}

# Quota monitor calls are local to the agent-manager container. No host port.
factory_limits_request() {
  local action=${1:-status}
  local path method
  case "$action" in
    status) path=/limits; method=GET ;;
    refresh) path=/limits/refresh; method=POST ;;
    *) echo 'Usage: ./factory.sh limits-status | limits-refresh' >&2; return 2 ;;
  esac
  factory_compose exec -T -e FACTORY_LIMIT_PATH="$path" -e FACTORY_LIMIT_METHOD="$method" agent-manager python3 -c '
import os,json
from urllib.request import Request,urlopen
from urllib.error import HTTPError
url="http://127.0.0.1:"+os.environ.get("FACTORY_AGENT_MANAGER_PORT","8080")+os.environ["FACTORY_LIMIT_PATH"]
method=os.environ["FACTORY_LIMIT_METHOD"]
req=Request(url, method=method, data=b"{}" if method=="POST" else None, headers={"Authorization":"Bearer "+os.environ["FACTORY_AGENT_API_TOKEN"],"Content-Type":"application/json"})
try:
    with urlopen(req, timeout=30) as response:
        print(json.dumps(json.load(response), indent=2, ensure_ascii=False))
except HTTPError as error:
    print("Quota monitor HTTP %s" % error.code)
    raise SystemExit(1)
'
}

# Stop must work even if a legacy compose.yaml/.env combination is broken.
# No compose interpolation or bind path creation is needed for stop/help.
case "${1:-help}" in
  stop|help|-h|--help|repair-env|upgrade-env|reset|reset-data) ;;
  *) repair_env; prepare_bind_paths ;;
esac

case "${1:-help}" in
  repair-env|upgrade-env) repair_env ;;
  reset|reset-data) reset_data "$@" ;;
  config|check) factory_compose --profile agent-images config --quiet; echo 'Compose-Konfiguration OK.' ;;
  limits-status) factory_limits_request status ;;
  limits-refresh) factory_limits_request refresh ;;
  github-health) github_registry_request health ;;
  github-bootstrap) github_registry_request bootstrap ;;
  github-scan) github_registry_request scan ;;
  github-boards-ensure) github_registry_request boards/ensure ;;
  github-events) github_registry_request events ;;
  github-logs) factory_compose logs --tail=100 -f github-registry ;;
  start|up)
    prepare_workspace_permissions
    factory_compose --profile agent-images config --quiet
    prepare_n8n_data
    for provider in "${providers[@]}"; do ensure_image "$provider"; done
    factory_compose up -d --build
    echo "Factory bereit: $(env_value N8N_EDITOR_BASE_URL)"
    echo 'Worker: codex, claude, gemini. Anmelden: ./factory.sh auth-login PROVIDER default'
    echo 'Workflow-Auto-Import laeuft im Service workflow-sync (Logs: ./factory.sh workflows-sync-status).'
    echo 'GitHub Registry: ./factory.sh github-health (Repo und Token in .env eintragen).'
    ;;
  build-workers|build-agents) build_workers "${2:-all}" ;;
  update-worker) update_worker "${2:-}" ;;
  auth-login)
    parse_provider_profile "${2:-default}" "${3:-default}"
    profile_active "$auth_provider" "$auth_profile" && { echo 'Profil wird gerade von einem Worker verwendet.' >&2; exit 1; }
    init_profile "$auth_provider" "$auth_profile"
    echo "Starte $auth_provider-Kontoanmeldung fuer Profil $auth_profile"
    case "$auth_provider" in
      codex)
        docker run --rm -it --network "$(env_value FACTORY_EGRESS_NETWORK)" --user 1000:1000 \
          --read-only --tmpfs /tmp:rw,nosuid,noexec,size=96m \
          --cap-drop ALL --security-opt no-new-privileges \
          --mount "type=bind,source=$(auth_path codex "$auth_profile"),target=/auth" \
          --env CODEX_HOME=/auth --env HOME=/auth --env XDG_CACHE_HOME=/auth/.cache \
          --entrypoint codex "$(worker_image codex)" login --device-auth
        ;;
      claude)
        docker run --rm -it --network "$(env_value FACTORY_EGRESS_NETWORK)" --user 1000:1000 \
          --read-only --tmpfs /tmp:rw,nosuid,noexec,size=96m \
          --cap-drop ALL --security-opt no-new-privileges \
          --mount "type=bind,source=$(auth_path claude "$auth_profile"),target=/auth" \
          --env CLAUDE_CONFIG_DIR=/auth/.claude --env HOME=/auth \
          --env XDG_CACHE_HOME=/auth/.cache --env DISABLE_AUTOUPDATER=1 \
          --entrypoint claude "$(worker_image claude)" auth login
        auth_docker claude "$auth_profile" auth status >/dev/null
        docker run --rm --network none --user 1000:1000 \
          --mount "type=bind,source=$(auth_path claude "$auth_profile"),target=/auth" \
          --entrypoint /bin/sh "$(worker_image claude)" -ec 'umask 077; touch /auth/.factory-login-ready'
        ;;
      gemini)
        echo 'Falls die Browser-/localhost-Weiterleitung im Container scheitert:'
        echo '  ./factory.sh auth-import gemini default "$HOME/.gemini"'
        docker run --rm -it --network "$(env_value FACTORY_EGRESS_NETWORK)" --user 1000:1000 \
          --read-only --tmpfs /tmp:rw,nosuid,noexec,size=96m \
          --cap-drop ALL --security-opt no-new-privileges \
          --mount "type=bind,source=$(auth_path gemini "$auth_profile"),target=/auth" \
          --env HOME=/auth --env XDG_CACHE_HOME=/auth/.cache \
          --env GEMINI_TELEMETRY_ENABLED=false \
          --entrypoint gemini "$(worker_image gemini)"
        ;;
    esac
    auth_status "$auth_provider" "$auth_profile"
    ;;
  auth-import)
    parse_provider_profile "${2:-default}" "${3:-default}"
    source_file=${4:-}
    if [[ "$auth_provider" == codex && "${2:-}" != codex ]]; then source_file=${3:-}; fi
    [[ -n "$source_file" ]] || { echo 'Verwendung: auth-import gemini default "$HOME/.gemini" ODER auth-import codex default DATEI' >&2; exit 2; }
    profile_active "$auth_provider" "$auth_profile" && { echo 'Profil ist aktiv.' >&2; exit 1; }
    init_profile "$auth_provider" "$auth_profile"
    if [[ "$auth_provider" == codex ]]; then
      [[ -f "$source_file" ]] || { echo 'Codex auth.json fehlt.' >&2; exit 2; }
      docker run --rm -i --user 1000:1000 \
        --mount "type=bind,source=$(auth_path codex "$auth_profile"),target=/auth" \
        --env CODEX_HOME=/auth --entrypoint /bin/sh "$(worker_image codex)" -ec '
          umask 077; cat > /auth/auth.json.import
          if ! jq -e '\''type == "object" and .auth_mode == "chatgpt" and (.tokens.access_token | type == "string")'\'' /auth/auth.json.import >/dev/null 2>&1; then
            rm -f /auth/auth.json.import; exit 2
          fi
          mv -f /auth/auth.json.import /auth/auth.json
        ' < "$source_file"
    elif [[ "$auth_provider" == gemini ]]; then
      [[ -d "$source_file" && -f "$source_file/oauth_creds.json" ]] || { echo 'Verzeichnis mit oauth_creds.json fehlt.' >&2; exit 2; }
      source_file=$(cd -- "$source_file" && pwd -P)
      docker run --rm --user 0:0 --network none \
        --mount "type=bind,source=$source_file,target=/source,readonly" \
        --mount "type=bind,source=$(auth_path gemini "$auth_profile"),target=/auth" \
        --entrypoint /bin/sh "$(worker_image gemini)" -ec '
          set -e; umask 077; mkdir -p /auth/.gemini
          cp /source/oauth_creds.json /auth/.gemini/oauth_creds.json
          if test -f /source/settings.json; then cp /source/settings.json /auth/.gemini/settings.json; fi
          if test -f /source/google_accounts.json; then cp /source/google_accounts.json /auth/.gemini/google_accounts.json; fi
          chown -R 1000:1000 /auth
          chmod 600 /auth/.gemini/oauth_creds.json
        '
    else
      echo 'Fuer Claude bitte interaktiven Login mit ./factory.sh auth-login claude default verwenden.' >&2
      exit 2
    fi
    auth_status "$auth_provider" "$auth_profile"
    ;;
  auth-status)
    parse_provider_profile "${2:-default}" "${3:-default}"
    auth_status "$auth_provider" "$auth_profile"
    ;;
  auth-logout)
    parse_provider_profile "${2:-default}" "${3:-default}"
    profile_active "$auth_provider" "$auth_profile" && { echo 'Profil ist aktiv.' >&2; exit 1; }
    case "$auth_provider" in
      codex) auth_docker codex "$auth_profile" logout ;;
      claude)
        auth_docker claude "$auth_profile" auth logout
        docker run --rm --network none --user 1000:1000 \
          --mount "type=bind,source=$(auth_path claude "$auth_profile"),target=/auth" \
          --entrypoint /bin/sh "$(worker_image claude)" -ec 'rm -f /auth/.factory-login-ready'
        ;;
      gemini)
        docker run --rm --network none --user 1000:1000 \
          --mount "type=bind,source=$(auth_path gemini "$auth_profile"),target=/auth" \
          --entrypoint /bin/sh "$(worker_image gemini)" -ec 'rm -f /auth/.gemini/oauth_creds.json'
        ;;
    esac
    ;;
  smoke)
    prepare_workspace_permissions
    provider=${2:-codex}
    if [[ "$provider" == all ]]; then
      for provider in "${providers[@]}"; do factory_compose exec -T agent-manager python3 /app/smoke.py "$provider"; done
    else
      valid_provider "$provider"
      factory_compose exec -T agent-manager python3 /app/smoke.py "$provider"
    fi
    ;;
  agents|workers)
    docker ps -a --filter "$worker_filter" --filter 'label=factory.managed=true' \
      --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
    ;;
  worker-versions)
    for provider in "${providers[@]}"; do
      ensure_image "$provider"
      docker run --rm --network none --entrypoint "$provider" "$(worker_image "$provider")" --version
    done
    ;;
  stop)
    stop_workers
    mapfile -t compose_ids < <(docker ps -q --filter "label=com.docker.compose.project=$factory_name")
    if [[ ${#compose_ids[@]} -gt 0 ]]; then
      docker stop "${compose_ids[@]}" >/dev/null
      echo "Factory '$factory_name' gestoppt (${#compose_ids[@]} Services)."
    else
      echo "Factory '$factory_name' hat keine laufenden Services."
    fi
    ;;
  restart) stop_workers; prepare_workspace_permissions; prepare_n8n_data; factory_compose up -d --force-recreate ;;
  repair-workspaces|fix-workspaces)
    prepare_workspace_permissions
    echo 'Permissions updated. Verify: sudo ./factory.sh doctor'
    ;;
  workflows-check)
    # Verify the workflow bind mount from INSIDE the n8n container.
    # No credentials or model API logins are required.
    factory_compose exec -T n8n node -e '
      const fs = require("node:fs");
      const path = "/home/node/.n8n-files/workflows";
      try {
        const items = fs.readdirSync(path, {withFileTypes: true});
        const files = items.filter(i => i.isFile() && i.name.endsWith(".json"));
        if (!files.length) throw new Error("Keine Workflow-JSON-Dateien gefunden");
        for (const file of files) {
          JSON.parse(fs.readFileSync(`${path}/${file.name}`, "utf8"));
          process.stdout.write(`OK ${file.name}\n`);
        }
        process.stdout.write(`Workflow-Bind-Mount OK (${files.length} JSON-Dateien)\n`);
      } catch (e) {
        console.error(`Workflow-Verzeichnis nicht lesbar: ${e.message}`);
        process.exit(1);
      }
    '
    ;;
  workflows-sync-status)
    echo 'Automatischer Workflow-Importer (letzte Meldungen):'
    factory_compose ps workflow-sync
    factory_compose logs --tail=80 workflow-sync
    ;;
  workflows-import)
    # A bind mount exposes JSON on disk only. This explicit operation registers
    # the five Factory workflows in the n8n DB, making them visible in the UI.
    # Stable workflow IDs in the bundled JSON prevent duplicate imports.
    echo 'Pruefe Workflow-Dateien im n8n-Container ...'
    factory_compose exec -T -u node n8n node -e '
      const fs = require("node:fs");
      const base = "/home/node/.n8n-files/workflows";
      const files = fs.readdirSync(base).filter(name => name.endsWith(".json"));
      if (!files.length) { console.error("Keine Workflow-JSON-Dateien gefunden."); process.exit(2); }
      const ids = new Set();
      for (const file of files) {
        const data = JSON.parse(fs.readFileSync(`${base}/${file}`, "utf8"));
        if (!data.id || typeof data.id !== "string" || !data.name || !Array.isArray(data.nodes) || !data.connections) {
          console.error(`Ungueltiger Workflow: ${file} (id/name/nodes/connections)`);
          process.exit(2);
        }
        if (ids.has(data.id)) { console.error(`Doppelte Workflow-ID: ${data.id}`); process.exit(2); }
        ids.add(data.id);
      }
      console.log(`${files.length} gueltige Workflow-Dateien gefunden.`);
    '
    import_project=$(env_value FACTORY_N8N_IMPORT_PROJECT_ID 2>/dev/null || printf 'owner')
    if [[ -z "$import_project" || "$import_project" == owner ]]; then
      echo 'Importiere ins persoenliche Projekt des n8n-Instanzbesitzers.'
      import_args=()
    else
      [[ "$import_project" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo 'Ungueltige FACTORY_N8N_IMPORT_PROJECT_ID.' >&2; exit 2; }
      echo "Importiere in n8n-Projekt: $import_project"
      import_args=("--projectId=$import_project")
    fi
    echo 'Hinweis: Erneuter Import ueberschreibt Workflows mit derselben ID (auch Aenderungen aus der UI).'
    if ! factory_compose exec -T -u node n8n n8n import:workflow --separate \
      --input=/home/node/.n8n-files/workflows "${import_args[@]}"; then
      echo 'Import fehlgeschlagen. Bitte Fehlermeldung, Admin-Setup und Projekt-ID pruefen.' >&2
      echo 'Details: sudo docker compose logs --tail=60 n8n' >&2
      exit 1
    fi
    echo 'Import abgeschlossen. In n8n: Overview -> Workflows, Seite aktualisieren.'
    echo 'HTTP Request Nodes benoetigen weiterhin die Header-Auth-Credential fuer den Agent-Manager.'
    ;;
  doctor)
    if check_manager_workspace; then
      echo 'Agent manager workspace and Docker socket: OK'
    else
      echo 'Manager cannot prepare worker directories or reach Docker.' >&2
      echo 'Try: sudo ./factory.sh repair-workspaces && sudo ./factory.sh doctor' >&2
      echo 'If the error persists, inspect: sudo docker compose logs --tail=50 agent-manager' >&2
      exit 1
    fi
    ;;
  down) stop_workers; remove_workers; factory_compose down; echo 'Bind-Mount-Daten und Loginverzeichnisse bleiben erhalten.' ;;
  status|ps) factory_compose ps; echo 'Dynamische Worker:'; docker ps -a --filter "$worker_filter" --filter 'label=factory.managed=true' --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' ;;
  logs) shift; factory_compose logs -f --tail=100 "$@" ;;
  help|-h|--help)
    cat <<'HELP'
Factory v5.7 - Reset fuer Bind-Mount-Daten

  ./factory.sh repair-env                       .env-Aliasse reparieren; Geheimnisse bewahren
  ./factory.sh config                            Compose-Konfiguration pruefen
  ./factory.sh reset-data --dry-run              Datenpfade und Resetumfang anzeigen (ohne Aenderungen)
  ./factory.sh reset-data                        Alle Daten nach interaktiver Bestaetigung loeschen
  ./factory.sh reset-data --yes --confirm RESET-FACTORY  Explizit bestaetigter automatischer Reset
  ./factory.sh start                             Bind-Mounts vorbereiten und Factory starten
  ./factory.sh update-worker claude              NUR Claude Worker aktualisieren
  ./factory.sh update-worker gemini              NUR Gemini Worker aktualisieren
  ./factory.sh update-worker codex               NUR Codex Worker aktualisieren
  ./factory.sh build-workers [provider|all]      Worker-Images bauen
  ./factory.sh auth-login PROVIDER [profil]      Abonnement-/Google-Login (kein Modell-API-Key)
  ./factory.sh auth-import gemini default PFAD   Bereits vorhandene Gemini OAuth-Anmeldung
  ./factory.sh auth-import codex default DATEI   Bereits vorhandene Codex ChatGPT-Anmeldung
  ./factory.sh limits-refresh                  Login-Quoten per Status-Container abfragen
  ./factory.sh limits-status                   Quoten / Reset-Zeiten anzeigen
  ./factory.sh auth-status PROVIDER [profil]     Authentifizierungsstatus
  ./factory.sh auth-logout PROVIDER [profil]     Login beenden
  ./factory.sh smoke [provider|all]              Docker-Spawn ohne Modellaufruf testen
  ./factory.sh repair-workspaces                 Workspace-Bind-Mount-Rechte reparieren
  ./factory.sh doctor                            Schreibrechte und Docker-Zugriff pruefen
  ./factory.sh workflows-check                   Workflow-Bind-Mount im n8n-Container pruefen
  ./factory.sh workflows-import                  Manueller Force-Import (optional)
  ./factory.sh workflows-sync-status             Auto-Importer-Status und Logs anzeigen
  ./factory.sh github-health                    GitHub Registry Dienst pruefen
  ./factory.sh github-bootstrap                 Leere private Registry initialisieren (keine Auto-Erkennung)
  ./factory.sh github-scan                      GitHub Repos sofort scannen (ohne n8n Publish)
  ./factory.sh github-boards-ensure             Fehlende Projects-v2-Boards/Kategorien provisionieren
  ./factory.sh github-events                    Pending Issue-Ereignisse anzeigen
  ./factory.sh github-logs                      GitHub Registry Dienst-Logs
  ./factory.sh agents                            Dynamische Agent-Container anzeigen
  ./factory.sh worker-versions                   Installierte CLI-Versionen
  ./factory.sh status|logs|stop|restart|down    Factory-Verwaltung

provider: codex | claude | gemini
profil: default | requirements | architect | planner | developer | tester | reviewer | security
Alte v3-Syntax auth-login default / auth-status default bleibt fuer Codex erhalten.
Nur Docker, Compose und Bash auf dem Host notwendig, alle Variablen in .env.
HELP
    ;;
  *) echo "Unbekannter Befehl: $1" >&2; exit 2 ;;
esac
