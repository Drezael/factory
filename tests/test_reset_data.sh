#!/usr/bin/env bash
set -Eeuo pipefail
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/factory" "$TMP/bin"
cp "$SOURCE_DIR/factory.sh" "$SOURCE_DIR/compose.yaml" "$SOURCE_DIR/.env.example" "$TMP/factory/"
cp "$SOURCE_DIR/.env.example" "$TMP/factory/.env"
chmod 600 "$TMP/factory/.env"
cat > "$TMP/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "${FAKE_DOCKER_FAIL_CONFIG:-0}" == 1 && " $* " == *' config --quiet '* ]]; then exit 1; fi
if [[ "${FAKE_DOCKER_FAIL_DOWN:-0}" == 1 && " $* " == *' down --remove-orphans '* ]]; then exit 1; fi
exit 0
DOCKER
chmod +x "$TMP/bin/docker"
export PATH="$TMP/bin:$PATH" FAKE_DOCKER_LOG="$TMP/docker-log"
cd "$TMP/factory"
mkdir -p data/postgres data/n8n data/n8n-workflow-sync data/auth data/workspaces n8n-workflows
for d in postgres n8n n8n-workflow-sync auth workspaces; do
  mkdir -p "data/$d/subdir"
  printf 'test-%s' "$d" > "data/$d/visible"
  printf 'hidden' > "data/$d/.hidden"
  printf 'nested' > "data/$d/subdir/file"
done
printf 'must-be-preserved' > n8n-workflows/sample.json
printf 'must-be-preserved' > keep.txt
original_env=$(sha256sum .env | awk '{print $1}')
original_json=$(sha256sum n8n-workflows/sample.json | awk '{print $1}')

# Dry run lists paths but never mutates data, Compose, or Docker.
./factory.sh reset-data --dry-run > "$TMP/dry.txt"
grep -q 'DRY RUN' "$TMP/dry.txt"
test -f data/postgres/visible
test ! -s "$FAKE_DOCKER_LOG"

# No TTY = no deletion without double explicit confirmation.
if ./factory.sh reset-data > "$TMP/nontty.txt" 2>&1; then
  echo 'FAIL: noninteractive reset not blocked' >&2; exit 1
fi
test -f data/postgres/visible
if ./factory.sh reset-data --yes > "$TMP/bad-confirm.txt" 2>&1; then
  echo 'FAIL: missing confirmation not blocked' >&2; exit 1
fi
test -f data/postgres/visible

# Refuse a directory outside the allowed data root.
printf '\nFACTORY_WORKSPACE_DATA_DIR=./other-dir\n' >> .env
mkdir -p other-dir; touch other-dir/keep
if ./factory.sh reset-data --yes --confirm RESET-FACTORY > "$TMP/outside.txt" 2>&1; then
  echo 'FAIL: data path outside reset root was accepted' >&2; exit 1
fi
grep -q 'outside FACTORY_RESET_ALLOWED_ROOTS' "$TMP/outside.txt"
test -f other-dir/keep
sed -i '$d' .env

# Reject a symlink source and retain its target.
mv data/auth data/auth-real
ln -s auth-real data/auth
if ./factory.sh reset-data --yes --confirm RESET-FACTORY > "$TMP/symlink.txt" 2>&1; then
  echo 'FAIL: symlink data path was accepted' >&2; exit 1
fi
grep -q 'Refusing symlink' "$TMP/symlink.txt"
test -f data/auth-real/visible
rm data/auth; mv data/auth-real data/auth

# Reject overlapping data roots.
printf '\nFACTORY_AUTH_DATA_DIR=./data/n8n/auth\n' >> .env
if ./factory.sh reset-data --yes --confirm RESET-FACTORY > "$TMP/overlap.txt" 2>&1; then
  echo 'FAIL: overlapping data path was accepted' >&2; exit 1
fi
grep -q 'Overlapping reset data paths' "$TMP/overlap.txt"
sed -i '$d' .env

# Refuse if workflows would be deleted.
printf '\nFACTORY_N8N_WORKFLOWS_DIR=./data/n8n/workflows\n' >> .env
if ./factory.sh reset-data --yes --confirm RESET-FACTORY > "$TMP/workflow.txt" 2>&1; then
  echo 'FAIL: workflow path nested in reset data was accepted' >&2; exit 1
fi
grep -q 'Workflow source directory' "$TMP/workflow.txt"
sed -i '$d' .env

# A Docker/Compose failure must NOT delete anything.
export FAKE_DOCKER_FAIL_DOWN=1
if ./factory.sh reset-data --yes --confirm RESET-FACTORY > "$TMP/downfail.txt" 2>&1; then
  echo 'FAIL: failed Docker shutdown not blocked' >&2; exit 1
fi
test -f data/postgres/visible
unset FAKE_DOCKER_FAIL_DOWN
export FAKE_DOCKER_FAIL_CONFIG=1
if ./factory.sh reset-data --yes --confirm RESET-FACTORY > "$TMP/configfail.txt" 2>&1; then
  echo 'FAIL: failed Compose config not blocked' >&2; exit 1
fi
test -f data/postgres/visible
unset FAKE_DOCKER_FAIL_CONFIG

# Successful reset must remove dotfiles/subdirectories but keep directory mounts.
original_env=$(sha256sum .env | awk '{print $1}')
./factory.sh reset-data --yes --confirm RESET-FACTORY > "$TMP/success.txt"
grep -q 'reset completed' "$TMP/success.txt"
for d in postgres n8n n8n-workflow-sync auth workspaces; do
  test -d "data/$d"
  test -z "$(find "data/$d" -mindepth 1 -print -quit)"
done
[[ $(sha256sum .env | awk '{print $1}') == "$original_env" ]]
[[ $(sha256sum n8n-workflows/sample.json | awk '{print $1}') == "$original_json" ]]
test -f keep.txt
# For external bind mounts, require an explicit allowlisted root.
mkdir -p "$TMP/external/store/postgres"
printf external > "$TMP/external/store/postgres/record"
cat >> .env <<ENV
FACTORY_POSTGRES_DATA_DIR=$TMP/external/store/postgres
FACTORY_RESET_ALLOWED_ROOTS=./data:$TMP/external/store
ENV
./factory.sh reset-data --dry-run > "$TMP/external-preview.txt"
./factory.sh reset-data --yes --confirm RESET-FACTORY > "$TMP/external-run.txt"
test ! -f "$TMP/external/store/postgres/record"

echo 'PASS: factory reset dry-run, guards, Compose failures and destructive reset'
