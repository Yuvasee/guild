# bridge's rewake waiter, run by Claude Code's asyncRewake hooks: sh rewake.sh <bridge-hook> <event>.
# Between turns it watches the tab's hint file; when a send sets it to 0 it runs `<bridge-hook> rewake`, whose exit 2
# with the mail on stderr becomes a new turn. One waiter per Claude process, the newest; it leaves when that process
# does, when a newer one takes over, or when Claude Code no longer reads it.
# While waiting it runs nothing but `sleep`: no Python stays resident.
hook=$1
event=$2
pid=$CLAUDE_PID
case $pid in '' | *[!0-9]*) exit 0 ;; esac
state=${BRIDGE_STATE_DIR:-$HOME/.local/state/bridge}
dir=$state/rewake
busy=$dir/$pid.busy
lock=$dir/$pid.waiter
hint=$state/hints/$pid
# Tests poll faster; a typer reads a beat older than rewake_marks.BEAT_FRESH_S as no waiter.
poll=${BRIDGE_REWAKE_POLL_S:-1}
# Claude Code shows everything a waking hook wrote to stderr as the turn's text, failed attempts' included: the mail
# alone goes there, on fd 3. The waiter's own misses (no hint yet, a lock just gone) are expected and dropped.
exec 3>&2 2>/dev/null

case $event in
SessionStart)
  payload=$(cat)
  # Auto-compaction starts a session in the middle of a turn, which stays busy.
  case $payload in
  *'"source":"compact"'* | *'"source": "compact"'*) ;;
  *) rm -f "$busy" ;;
  esac
  ;;
Stop)
  cat >/dev/null
  rm -f "$busy"
  ;;
*) exit 0 ;;
esac

# A print-mode or SDK Claude waits for its async hooks before it exits, so a waiter there would hold it open.
# Claude Code writes its record while the SessionStart hooks already run, after up to 5 s of its own setup: a single
# read could find none and leave the tab without a waiter until its first turn ended.
record=${BRIDGE_CLAUDE_SESSIONS_DIR:-$HOME/.claude/sessions}/$pid.json
tries=${BRIDGE_REWAKE_RECORD_TRIES:-200}
while :; do
  case $(cat "$record") in
  *'"entrypoint":"cli"'* | *'"entrypoint": "cli"'*) break ;;
  *'"entrypoint"'*) exit 0 ;;
  esac
  [ "$tries" -gt 0 ] && kill -0 "$pid" || exit 0
  tries=$((tries - 1))
  sleep 0.1
done

leave() {
  owner=
  read -r owner <"$lock/pid"
  [ "$owner" = $$ ] && rm -rf "$lock"
}
trap 'leave; exit 0' TERM HUP INT

mkdir -p "$dir" || exit 0
if ! mkdir "$lock"; then
  # The newest waiter takes over, a live one too: /restart runs a new Claude Code in the same process, which no longer
  # reads an older waiter, and that one would keep the lock and turn every later waiter away. The older one leaves at
  # its next round. Two taking it over both write their pid; after a settle only the last stays.
  echo $$ >"$lock/pid" || exit 0
  sleep 1
  owner=
  read -r owner <"$lock/pid"
  [ "$owner" = $$ ] || exit 0
fi
echo $$ >"$lock/pid" || exit 0

while kill -0 "$pid"; do
  owner=
  read -r owner <"$lock/pid"
  [ "$owner" = $$ ] || exit 0
  # Its age tells a typer this waiter still listens. Apart from the pid, which only a takeover writes, so a newer
  # waiter's pid is never written over.
  : >"$lock/beat"
  if [ ! -e "$busy" ]; then
    due=
    read -r due <"$hint"
    if [ "$due" = 0 ]; then
      # Due, as 0 is, for a tool hook's check; a send after this sets 0 again and is seen next round.
      echo 1 >"$hint"
      BRIDGE_REWAKE_FD=3 "$hook" rewake 2>>"$state/errors.log"
      code=$?
      if [ $code = 2 ]; then
        # The turn this starts fires no UserPromptSubmit.
        : >"$busy"
        leave
        exit 2
      fi
      if [ $code = 3 ]; then
        # Claude Code no longer reads this waiter: a typer then finds none and types at once.
        leave
        exit 0
      fi
    fi
  fi
  sleep "$poll"
done
leave
rm -f "$busy"
exit 0
