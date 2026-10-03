#!/usr/bin/env bash
# Prepare bd and a project database for this session, then print `bd prime`.
#
#   bash scripts/setup.sh [project]
#   bash scripts/setup.sh --upgrade [version]   move every project in the
#                                               bucket to another bd release
#
# Each project database is one git bundle object in an S3-compatible bucket:
# s3://<bucket>/<prefix><project>.bundle. The bd release to use is recorded in
# the bucket (<prefix>bd/VERSION) and its archive is cached next to it.
set -euo pipefail

BD_VERSION="1.3.1"   # recorded for a bucket that has no bd version yet
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${BEADS_S3_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/beads-s3/config.json}"
WORK_ROOT="${BEADS_S3_HOME:-$HOME/beads}"
FAKE_HOST="https://s3bundle.invalid/"

say()  { echo "[beads-s3] $*" >&2; }
fail() { echo "[beads-s3] error: $*" >&2; exit 1; }

quiet() {  # run a command, show its output only if it fails
  local out
  if ! out="$("$@" 2>&1)"; then
    echo "$out" >&2
    return 1
  fi
}

# --- settings ---------------------------------------------------------------

if [ -f "$SKILL_DIR/auth/credentials.json" ]; then
  mkdir -p "$(dirname "$CONFIG")"
  install -m 600 "$SKILL_DIR/auth/credentials.json" "$CONFIG"
fi
[ -f "$CONFIG" ] || fail "no bucket settings. Put credentials.json in $SKILL_DIR/auth/ (see credentials.example.json) or create $CONFIG."

command -v git >/dev/null || fail "git is required"
command -v python3 >/dev/null || fail "python3 is required"

read_setting() {
  python3 - "$CONFIG" "$1" "${2:-}" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
print(cfg.get(sys.argv[2]) or sys.argv[3])
PY
}
BUCKET="$(read_setting bucket)"
PREFIX="$(read_setting prefix beads/)"
[ -n "$BUCKET" ] || fail "\"bucket\" is missing in $CONFIG"
case "$PREFIX" in ""|*/) ;; *) PREFIX="$PREFIX/" ;; esac

# --- tools ------------------------------------------------------------------

if ! python3 -c "import boto3" 2>/dev/null; then
  say "installing boto3"
  python3 -m pip install --quiet boto3 2>/dev/null \
    || python3 -m pip install --quiet --break-system-packages boto3 2>/dev/null \
    || python3 -m pip install --quiet --user boto3 \
    || fail "could not install boto3"
fi

BDCACHE=(python3 "$SKILL_DIR/scripts/bdcache.py")
"${BDCACHE[@]}" ensure "$BD_VERSION" >/dev/null || fail "could not install bd"
command -v bd >/dev/null || fail "bd was installed outside PATH"

helper_dir="$(dirname "$(command -v bd)")"
install -m 755 "$SKILL_DIR/scripts/git-remote-s3bundle" "$helper_dir/git-remote-s3bundle"

# --- git --------------------------------------------------------------------

git config --global --get-all url.s3bundle://.insteadOf 2>/dev/null | grep -qx "$FAKE_HOST" \
  || git config --global --add url.s3bundle://.insteadOf "$FAKE_HOST"
git config --global user.name >/dev/null \
  || git config --global user.name "$(read_setting git_user_name beads)"
git config --global user.email >/dev/null \
  || git config --global user.email "$(read_setting git_user_email beads@localhost)"
git config --global beads.role >/dev/null \
  || git config --global beads.role maintainer

remote_url() { echo "${FAKE_HOST}${BUCKET}/${PREFIX}$1.bundle"; }

# --- upgrade ----------------------------------------------------------------

if [ "${1:-}" = "--upgrade" ]; then
  NEW="${2:-$("${BDCACHE[@]}" latest)}"
  OLD="$(bd version | sed -n 's/.*version \([^ ]*\).*/\1/p')"
  [ "$NEW" != "$OLD" ] || { say "already on bd $NEW"; exit 0; }
  "${BDCACHE[@]}" install "$NEW" >/dev/null || fail "could not install bd $NEW"
  for project in $("${BDCACHE[@]}" projects); do
    tmp="$(mktemp -d)"
    mkdir -p "$tmp/$project/.beads"
    chmod 700 "$tmp/$project/.beads"
    printf 'sync:\n  remote: "git+%s"\n' "$(remote_url "$project")" > "$tmp/$project/.beads/config.yaml"
    (
      cd "$tmp/$project"
      quiet bd bootstrap --yes || fail "bootstrap of $project failed with bd $NEW"
      quiet bd migrate --force || fail "migration of $project failed"
      quiet bd dolt push || fail "push of $project failed"
    )
    rm -rf "$tmp" "${WORK_ROOT:?}/$project"
    say "$project migrated to bd $NEW"
  done
  "${BDCACHE[@]}" set-version "$NEW"
  say "bucket now uses bd $NEW (was $OLD)"
  exit 0
fi

# --- project database -------------------------------------------------------

PROJECT="${1:-$(read_setting default_project beads)}"
[[ "$PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || fail "invalid project name: $PROJECT"
GIT_URL="$(remote_url "$PROJECT")"
WS="$WORK_ROOT/$PROJECT"

if [ ! -d "$WS/.beads" ]; then
  mkdir -p "$WS"
  cd "$WS"
  if git ls-remote "$GIT_URL" refs/dolt/data | grep -q .; then
    say "restoring $PROJECT from s3://$BUCKET/${PREFIX}$PROJECT.bundle"
    mkdir -p .beads
    chmod 700 .beads
    printf 'sync:\n  remote: "git+%s"\n' "$GIT_URL" > .beads/config.yaml
    quiet bd bootstrap --yes || { rm -rf .beads; fail "bootstrap failed"; }
  else
    say "creating $PROJECT at s3://$BUCKET/${PREFIX}$PROJECT.bundle"
    quiet git-remote-s3bundle --seed "s3bundle://${BUCKET}/${PREFIX}${PROJECT}.bundle"
    quiet bd init --quiet --stealth --prefix "$PROJECT" || { rm -rf .beads; fail "bd init failed"; }
    quiet bd dolt remote add origin "git+$GIT_URL"
    quiet bd dolt push || fail "first push failed"
  fi
  quiet bd config set no-git-ops true
  quiet bd config set dolt.auto-push true
  quiet bd config set dolt.auto-push-interval 1ms
fi

cd "$WS"
echo "Workspace: $WS (run bd commands from this directory)"
echo
bd prime
