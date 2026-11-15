#!/usr/bin/env bash
# Daily encrypted backup of ~/.guild and the bridge DB, for a Mac where they are the only copy.
#
#   scripts/backup.sh               write ~/.guild/backups/<UTC date>.tar.age and keep the newest 14; the same into a
#                                   second folder (e.g. one iCloud Drive syncs) if one is set: GUILD_BACKUP_COPY_DIR,
#                                   else the first line of ~/.guild/backup-copy-dir.txt
#   scripts/backup.sh restore-test  decrypt the newest backup with the keychain identity, check it, delete the plaintext
#   scripts/backup.sh install       run the backup daily at 03:30 through a launchd agent; `uninstall` removes it
#
# Encrypted to the age recipients in ~/.guild/backup-recipient.txt; the identity lives in the keychain:
#   security add-generic-password -s guild-backup -a "$USER" -w "$(cat key.txt)"
# Restore by hand: age -d -i key.txt <file>.tar.age | tar -x   (gives .guild/ and bridge.db)
set -Eeuo pipefail
umask 077

guild=$HOME/.guild
backups=$guild/backups
recipients=$guild/backup-recipient.txt
state=${BRIDGE_STATE_DIR:-$HOME/.local/state/bridge}
db=$state/bridge.db
keep=14
label=dev.guild.backup
plist=$HOME/Library/LaunchAgents/$label.plist

work=$(mktemp -d "${TMPDIR:-/tmp}/guild-backup.XXXXXX")
partial=
cleanup() {
  rm -rf "$work"
  [ -z "$partial" ] || rm -f "$partial"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cmd=${1:-backup}
step=start
fail() {
  echo "${0##*/} $cmd: fail at $step (exit $1)" >&2
  exit 1
}
trap 'fail $?' ERR
die() {
  echo "${0##*/} $cmd: fail: $*" >&2
  exit 1
}

backup_names() {
  grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}\.tar\.age$' || true
}

list_backups() {
  [ -d "$1" ] || return 0
  (cd "$1" && printf '%s\n' *.tar.age) | backup_names
}

prune() {
  list_backups "$1" | sort -r | tail -n +$((keep + 1)) | while read -r old; do rm -f "$1/$old"; done
}

read_copy_dir() {
  local file=$guild/backup-copy-dir.txt dir=${GUILD_BACKUP_COPY_DIR:-}
  [ -n "$dir" ] || [ ! -f "$file" ] || dir=$(grep -Ev '^[[:space:]]*(#|$)' "$file" | head -n 1 || true)
  echo "${dir/#\~/$HOME}"
}

run_backup() {
  step="check inputs"
  [ -s "$recipients" ] || die "put an age public key in $recipients"
  [ -f "$db" ] || die "no bridge DB at $db"
  mkdir -p "$backups"

  step="copy the bridge DB"
  sqlite3 "$db" ".backup \"$work/bridge.db\""

  step="tar and encrypt"
  local name out
  name=$(date -u +%Y-%m-%d).tar.age
  out=$backups/$name
  partial=$out.part
  tar -C "$HOME" --exclude .guild/backups -cf - .guild -C "$work" bridge.db | age -R "$recipients" -o "$partial"
  mv -f "$partial" "$out"
  partial=

  step="prune $backups"
  prune "$backups"
  echo "backup: wrote $out ($(du -h "$out" | cut -f1 | tr -d ' '))"

  local copy_dir
  copy_dir=$(read_copy_dir)
  if [ -z "$copy_dir" ]; then
    echo "backup: no copy folder configured (GUILD_BACKUP_COPY_DIR or $guild/backup-copy-dir.txt); local copy only"
    return
  fi
  step="copy to $copy_dir"
  mkdir -p "$copy_dir"
  partial=$copy_dir/$name.part
  cp "$out" "$partial"
  mv -f "$partial" "$copy_dir/$name"
  partial=
  step="prune $copy_dir"
  prune "$copy_dir"
  echo "backup: copied to $copy_dir/$name"
}

run_restore_test() {
  step="find the newest backup"
  local latest
  latest=$(list_backups "$backups" | sort | tail -n 1)
  [ -n "$latest" ] || die "no backup in $backups"

  step="read the identity"
  local identity=$work/identity
  # Tests have no keychain entry; nothing else should set this.
  if [ -n "${GUILD_BACKUP_TEST_IDENTITY:-}" ]; then
    printf '%s\n' "$GUILD_BACKUP_TEST_IDENTITY" >"$identity"
  else
    security find-generic-password -s guild-backup -a "$USER" -w >"$identity"
  fi

  step="decrypt $latest"
  age -d -i "$identity" -o "$work/backup.tar" "$backups/$latest"
  rm -f "$identity"

  step="list $latest"
  tar -tvf "$work/backup.tar"
  tar -tf "$work/backup.tar" | grep -x '.guild/backup-recipient.txt' >/dev/null || die "$latest has no .guild/backup-recipient.txt"

  step="check the DB copy"
  tar -xf "$work/backup.tar" -C "$work" bridge.db
  local integrity
  integrity=$(sqlite3 "$work/bridge.db" 'PRAGMA integrity_check')
  [ "$integrity" = ok ] || die "integrity_check of bridge.db says: $integrity"
  echo "restore-test: ok ($latest)"
}

run_install() {
  step="write $plist"
  local script path dir
  script=$(cd "$(dirname "$0")" && pwd)/${0##*/}
  path=/usr/bin:/bin:/usr/sbin:/sbin
  # launchd starts agents with the bare system PATH, which lacks Homebrew's age.
  command -v age >/dev/null || die "age is not on PATH"
  dir=$(dirname "$(command -v age)")
  case ":$path:" in *":$dir:"*) ;; *) path=$dir:$path ;; esac
  mkdir -p "$(dirname "$plist")" "$backups"
  cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$script</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$path</string>
    <key>BRIDGE_STATE_DIR</key><string>$state</string>${GUILD_BACKUP_COPY_DIR:+
    <key>GUILD_BACKUP_COPY_DIR</key><string>$GUILD_BACKUP_COPY_DIR</string>}
  </dict>
  <key>StandardOutPath</key><string>$backups/backup.log</string>
  <key>StandardErrorPath</key><string>$backups/backup.log</string>
</dict>
</plist>
EOF
  chmod 644 "$plist"
  step="load $label"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "install: $label runs $script daily at 03:30; log in $backups/backup.log"
}

run_uninstall() {
  step="unload $label"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$plist"
  echo "uninstall: removed $label"
}

case "$cmd" in
  backup) run_backup ;;
  restore-test) run_restore_test ;;
  install) run_install ;;
  uninstall) run_uninstall ;;
  *)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac
