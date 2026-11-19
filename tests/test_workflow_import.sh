#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/factory" "$TMP/mock"
cp "$ROOT/factory.sh" "$ROOT/compose.yaml" "$ROOT/.env.example" "$ROOT/.env" "$TMP/factory/"
# Need workflow files only on the host; docker is mocked so no daemon required.
mkdir -p "$TMP/factory/n8n-workflows"
cp "$ROOT/n8n-workflows/"*.json "$TMP/factory/n8n-workflows/"
cat > "$TMP/mock/docker" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_LOG"
exit 0
MOCK
chmod +x "$TMP/mock/docker"
export PATH="$TMP/mock:$PATH" MOCK_LOG="$TMP/commands.log"
cd "$TMP/factory"
./factory.sh workflows-import > "$TMP/out.log"
grep -qF 'import:workflow --separate --input=/home/node/.n8n-files/workflows' "$MOCK_LOG"
! grep -q -- '--projectId=' "$MOCK_LOG"
grep -qF 'Import abgeschlossen.' "$TMP/out.log"
sed -i 's|^FACTORY_N8N_IMPORT_PROJECT_ID=.*|FACTORY_N8N_IMPORT_PROJECT_ID=abc123|' .env
: > "$MOCK_LOG"
./factory.sh workflows-import > "$TMP/out2.log"
grep -qF -- '--projectId=abc123' "$MOCK_LOG"
python3 - "$ROOT/n8n-workflows" <<'PY'
import json,sys
from pathlib import Path
files=list(Path(sys.argv[1]).glob('*.json'))
assert len(files)==12,files
workflows=[json.loads(p.read_text(encoding='utf8')) for p in files]
ids=[w['id'] for w in workflows]
assert len(ids)==len(set(ids))
assert all(len(i)==36 for i in ids)
assert all(w['active'] is False for w in workflows)
assert len({w['name'] for w in workflows})==len(workflows)
PY
echo 'PASS: import command, optional project, fixed IDs and inactive workflows'
