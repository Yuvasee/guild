#!/usr/bin/env bash
# Deploys this checkout's HEAD so that every hook and `bridge` call runs either the old release or the new one whole:
# each release is a venv of its own under ~/.local/share/bridge/releases, and ~/.local/bin/{bridge,bridge-hook,guild}
# reach it through one symlink, ~/.local/share/bridge/current, swapped by rename. A running process keeps its release:
# its interpreter and site-packages are named by the release's own path, never by `current`.
#
#   scripts/deploy.sh            deploy HEAD (a clean tree only), stopping at the first failed step
#   scripts/deploy.sh rollback   point `current` back at the release before it, re-install its hooks, restart serve
set -Eeuo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
state=${BRIDGE_STATE_DIR:-$HOME/.local/state/bridge}
root=$HOME/.local/share/bridge
releases=$root/releases
current=$root/current
previous=$root/previous
bin=$HOME/.local/bin
keep=5
# Tests set a check that opens no agterm tab and starts no Claude.
check=${BRIDGE_DEPLOY_CHECK:-deploy-check}
export PATH="$bin:$PATH"
mkdir -p "$root"
# One deploy or rollback at a time; the kernel drops the lock when the script exits, however it ends.
[ -n "${BRIDGE_DEPLOY_LOCKED:-}" ] || BRIDGE_DEPLOY_LOCKED=1 exec lockf -t 0 "$root/deploy.lock" "$0" ${1+"$@"}

step=start
swapped=
fail() {
  echo "deploy: stopped at '$step' (exit $1)${2:+: $2}" >&2
  [ -z "$swapped" ] || echo "deploy: the new release is live; to go back: $repo/scripts/deploy.sh rollback" >&2
  exit 1
}
trap 'fail $?' ERR

point() {
  ln -sfn "$2" "$1.new.$$"
  # BSD mv -h renames over the link itself, atomically, instead of moving into the directory it points at; a GNU
  # mv earlier on PATH (Homebrew coreutils) has no -h.
  /bin/mv -fh "$1.new.$$" "$1"
}

link_entry_points() {
  local name
  for name in bridge bridge-hook guild; do
    [ "$(readlink "$bin/$name" 2>/dev/null)" = "$current/bin/$name" ] || point "$bin/$name" "$current/bin/$name"
  done
}

restart_and_hook() {
  step="bridge install-hooks"
  bridge install-hooks
  step="bridge restart-service"
  bridge restart-service
}

if [ "${1:-}" = rollback ]; then
  step="rollback: find the previous release"
  [ -L "$previous" ] && [ -L "$current" ] || fail 1 "no $previous or $current to swap"
  from=$(readlink "$current")
  to=$(readlink "$previous")
  [ -x "$to/bin/bridge" ] || fail 1 "$to has no bin/bridge"
  step="rollback: swap $current to $to (was $from)"
  point "$current" "$to"
  point "$previous" "$from"
  link_entry_points
  restart_and_hook
  echo "deploy: rolled back to $to (was $from); scripts/deploy.sh rollback again returns to it"
  exit 0
fi
[ $# -eq 0 ] || fail 2 "usage: scripts/deploy.sh [rollback]"

step="check the checkout"
dirty=$(git -C "$repo" status --porcelain)
[ -z "$dirty" ] || fail 1 "uncommitted changes in $repo; a release is a commit"
sha=$(git -C "$repo" rev-parse --short=12 HEAD)
release=$releases/$(date -u +%Y%m%dT%H%M%SZ)-$sha
venv=$release/bridge
logs=$state/deploys
mkdir -p "$logs" "$state/backups" "$releases"
step="find the installed bridge"
command -v bridge >/dev/null || fail 1 "no bridge on PATH; install it once with uv tool install (README)"

step="find the release to roll back to"
if [ -L "$current" ]; then
  was=$(readlink "$current")
else
  # The first deploy moves off uv's own tool venv; it stays where it is, as the release to roll back to.
  running=$(readlink -f "$bin/bridge")
  was=$(dirname "$(dirname "$running")")
fi
[ -f "$was/pyvenv.cfg" ] && [ -x "$was/bin/bridge" ] || fail 1 "$was is not a bridge venv"

step="deploy-check before"
bridge "$check" >"$logs/$sha-check-before.txt" 2>&1 || fail $? "see $logs/$sha-check-before.txt"

# After the check: an attempt it stops changes nothing, so the board hears only of a deploy that goes ahead.
step="board heads-up"
bridge board "Deploying bridge $sha: hooks and the dashboard restart within a minute." --topic bridge

step="back up bridge.db"
sqlite3 "$state/bridge.db" ".backup '$state/backups/bridge-pre-$sha.db'"

step="install $sha into $release"
# The bin links uv makes go into the release, so uv never touches ~/.local/bin.
# --reinstall rebuilds: without it uv reuses its cached wheel of the same version, which is the last build, not HEAD.
UV_TOOL_DIR=$release UV_TOOL_BIN_DIR=$release/links uv tool install --reinstall --compile-bytecode "$repo" \
  >"$logs/$sha-install.txt" 2>&1 || { rm -rf "$release"; fail 1 "see $logs/$sha-install.txt"; }
"$venv/bin/bridge" --help >/dev/null || { rm -rf "$release"; fail 1 "the new release does not start"; }
installed=$("$venv/bin/python" -c 'import bridge, os; print(os.path.dirname(bridge.__file__))')
diff -rq -x __pycache__ "$repo/src/bridge" "$installed" >"$logs/$sha-diff.txt" 2>&1 \
  || { rm -rf "$release"; fail 1 "the release is not this checkout's code: see $logs/$sha-diff.txt"; }

step="swap $current to $venv (was $was)"
point "$previous" "$was"
point "$current" "$venv"
swapped=1
link_entry_points
echo "deploy: $sha is live (was $was); to go back: $repo/scripts/deploy.sh rollback"

restart_and_hook

step="deploy-check after"
bridge "$check" >"$logs/$sha-check-after.txt" 2>&1 || fail $? "see $logs/$sha-check-after.txt"

step="prune old releases"
keep_now=$(dirname "$(readlink "$current")")
keep_prev=$(dirname "$(readlink "$previous")")
# A remote host tab or a night's dream can outlive several deploys and still read its release's files.
in_use=$(ps -axww -o command=)
find "$releases" -mindepth 1 -maxdepth 1 -type d | sort -r | tail -n +$((keep + 1)) | while read -r old; do
  case $in_use in *"$old/"*) continue ;; esac
  [ "$old" = "$keep_now" ] || [ "$old" = "$keep_prev" ] || rm -rf "$old"
done

echo "deploy: done; checks in $logs/$sha-check-{before,after}.txt, database copy in $state/backups/bridge-pre-$sha.db"
