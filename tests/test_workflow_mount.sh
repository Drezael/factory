#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TEMP=$(mktemp -d)
trap 'rm -rf -- "$TEMP"' EXIT
mkdir -p "$TEMP/mockbin" "$TEMP/factory"
cp "$ROOT/factory.sh" "$ROOT/compose.yaml" "$ROOT/.env.example" "$ROOT/.env" "$TEMP/factory/"
cat > "$TEMP/mockbin/docker" <<'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" != compose ]]; then exit 0; fi
# The host path is exported as an absolute path for Compose (as for other mounts).
printf '%s\n' "${FACTORY_N8N_WORKFLOWS_DIR:-}" > "$MOCK_WORKFLOW_PATH_OUTPUT"
exit 0
MOCK
chmod +x "$TEMP/mockbin/docker"
export PATH="$TEMP/mockbin:$PATH" MOCK_WORKFLOW_PATH_OUTPUT="$TEMP/path-output"
cd "$TEMP/factory"
sed -i 's|^FACTORY_N8N_WORKFLOWS_DIR=.*|FACTORY_N8N_WORKFLOWS_DIR=./custom/workflow-json|' .env
./factory.sh config >/dev/null
expected="$TEMP/factory/custom/workflow-json"
actual=$(cat "$MOCK_WORKFLOW_PATH_OUTPUT")
[[ "$actual" == "$expected" ]] || { echo "FAIL: wrong workflow path '$actual' != '$expected'" >&2; exit 1; }
[[ -d "$expected" ]] || { echo 'FAIL: workflow directory not created' >&2; exit 1; }
[[ -f "$ROOT/n8n-workflows/01-factory-systemstatus.json" ]] || { echo 'FAIL: v5.3 workflow files missing' >&2; exit 1; }
[[ $(grep -c 'FACTORY_N8N_WORKFLOWS_DIR' "$ROOT/compose.yaml") -ge 1 ]] || exit 1
[[ $(grep -c 'read_only: true' "$ROOT/compose.yaml") -ge 2 ]] || exit 1
# Verify upgrades from older installs transparently add the new nonsecret setting.
sed -i '/^FACTORY_N8N_WORKFLOWS_DIR=/d' .env
./factory.sh repair-env >/dev/null
grep -q '^FACTORY_N8N_WORKFLOWS_DIR=./n8n-workflows$' .env || { echo 'FAIL: env upgrade omitted workflow path' >&2; exit 1; }
echo 'PASS: custom workflow paths, missing env repair, directory creation, read-only bind'
