#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TEMP=$(mktemp -d)
trap 'rm -rf -- "$TEMP"' EXIT
mkdir -p "$TEMP/mockbin" "$TEMP/work"
cat > "$TEMP/mockbin/docker" <<'MOCK'
#!/usr/bin/env bash
# Stop must never invoke docker compose: the source .env may be broken.
if [[ "${1:-}" == compose ]]; then
  echo 'Unexpected compose interpolation' >&2
  exit 53
fi
case "${1:-}" in
  ps) exit 0 ;;
  stop) exit 0 ;;
  *) exit 0 ;;
esac
MOCK
chmod +x "$TEMP/mockbin/docker"
cp "$ROOT/factory.sh" "$ROOT/compose.yaml" "$ROOT/.env.example" "$ROOT/.env" "$TEMP/work/"
export PATH="$TEMP/mockbin:$PATH"
fail() { echo "FAIL: $*" >&2; exit 1; }
get_value() { sed -n "s/^$1=//p" "$TEMP/work/.env" | tail -n 1; }

# Case 1: Modern v5 .env has POSTGRES_PASSWORD but missing legacy alias.
sed -i '/^FACTORY_DB_PASSWORD=/d' "$TEMP/work/.env"
old_password=$(get_value POSTGRES_PASSWORD)
(cd "$TEMP/work" && ./factory.sh repair-env >/dev/null)
[[ "$(get_value FACTORY_DB_PASSWORD)" == "$old_password" ]] || fail 'Legacy DB alias not added'
[[ -n "$(ls "$TEMP/work"/.env.backup.* 2>/dev/null)" ]] || fail 'No backup written'

# Case 2: Old .env has only FACTORY_DB_PASSWORD; new key must match.
sed -i '/^POSTGRES_PASSWORD=/d' "$TEMP/work/.env"
(cd "$TEMP/work" && ./factory.sh repair-env >/dev/null)
[[ "$(get_value POSTGRES_PASSWORD)" == "$old_password" ]] || fail 'Current DB alias not added'

# Case 3: Conflicting passwords must be rejected and not silently changed.
sed -i 's/^FACTORY_DB_PASSWORD=.*/FACTORY_DB_PASSWORD=conflicting-secret/' "$TEMP/work/.env"
if (cd "$TEMP/work" && ./factory.sh repair-env >/dev/null 2>&1); then
  fail 'Conflicting passwords were silently accepted'
fi
[[ "$(get_value POSTGRES_PASSWORD)" == "$old_password" ]] || fail 'Original secret was overwritten'

# Case 4: Stopping must work with completely broken/missing credentials.
printf 'FACTORY_NAME=factory\n' > "$TEMP/work/.env"
(cd "$TEMP/work" && ./factory.sh stop >/dev/null) || fail 'Stop depends on full .env or docker compose'

# Case 5: If both password names are missing, repair refuses inventing a new one.
if (cd "$TEMP/work" && ./factory.sh repair-env >/dev/null 2>&1); then
  fail 'A new password was incorrectly invented for an existing database'
fi

echo 'PASS: env alias repair, legacy compatibility, conflict detection, no-data-loss and stop fallback'
