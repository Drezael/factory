#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TEMP=$(mktemp -d)
trap 'rm -rf -- "$TEMP"' EXIT
mkdir -p "$TEMP/mockbin" "$TEMP/factory"
cp "$ROOT/factory.sh" "$ROOT/compose.yaml" "$ROOT/.env" "$ROOT/.env.example" "$TEMP/factory/"
cat > "$TEMP/mockbin/docker" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_LOG"
exit 0
MOCK
chmod +x "$TEMP/mockbin/docker"
export PATH="$TEMP/mockbin:$PATH" MOCK_LOG="$TEMP/docker.log"
cd "$TEMP/factory"
sed -i '/^FACTORY_N8N_SYNC_DATA_DIR=/d; /^FACTORY_N8N_AUTO_IMPORT=/d; /^FACTORY_N8N_SYNC_INTERVAL_SEC=/d; /^FACTORY_N8N_SYNC_IMPORT_TIMEOUT_SEC=/d' .env
./factory.sh repair-env >/dev/null
for key in FACTORY_N8N_SYNC_DATA_DIR FACTORY_N8N_AUTO_IMPORT FACTORY_N8N_SYNC_INTERVAL_SEC FACTORY_N8N_SYNC_IMPORT_TIMEOUT_SEC; do
  grep -q "^${key}=" .env || { echo "FAIL missing $key" >&2; exit 1; }
done
./factory.sh config >/dev/null
[[ -d "$TEMP/factory/data/n8n-workflow-sync" ]] || { echo 'FAIL: auto-sync state bind dir missing' >&2; exit 1; }
python3 - "$ROOT/compose.yaml" <<'PY'
import yaml,sys
config=yaml.safe_load(open(sys.argv[1]))
sync=config['services']['workflow-sync']
assert sync['networks']==['factory_internal']
assert 'ports' not in sync
assert not any('docker.sock' in str(m) for m in sync['volumes'])
assert sync['read_only'] is True
assert sync['environment']['FACTORY_N8N_AUTO_IMPORT']=='${FACTORY_N8N_AUTO_IMPORT}'
assert sync['depends_on']['postgres']['condition']=='service_healthy'
assert sync['depends_on']['n8n']['condition']=='service_started'
assert any('${FACTORY_N8N_SYNC_DATA_DIR' in str(m) for m in sync['volumes'])
assert any('${FACTORY_N8N_WORKFLOWS_DIR' in str(m) and m.get('read_only') for m in sync['volumes'])
PY
echo 'PASS: bind mounts, internal-only network, env repair, auto-sync service'
