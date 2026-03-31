#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ivar Hogstad
# See LICENSE file in the project root for full licence text.

# ipv6_dns_watch_linux.sh
#
# Trailing-edge debounced IPv6 watcher for Linux.
# - watches: ip -ts monitor address
# - triggers only on: inet6 + scope global
# - ignores: tentative (DAD)
# - trailing-edge debounce: run once after changes have been quiet for N seconds
# - auto-restarts if ip monitor exits

set -u
PATH=/usr/sbin:/usr/bin:/sbin:/bin

SYNC_SCRIPT="${SYNC_SCRIPT:-/usr/local/bin/ipv6_dns_sync.py}"
CONFIG_URL="${CONFIG_URL:-https://a02.au/ipv6_sync_config/config.json}"

# Wait this many seconds after the *last* relevant event before running sync.
DEBOUNCE_SECONDS="${DEBOUNCE_SECONDS:-10}"

# DEBUG=1 logs matching events and debounce behavior.
DEBUG="${DEBUG:-0}"

log() { echo "[ipv6-dns-watch] $*" >&2; }

run_sync() {
  log "triggering sync"
  "$SYNC_SCRIPT" --config-url "$CONFIG_URL" -v
}

should_trigger() {
  local line="$1"

  # Only care about IPv6 global address events
  [[ "$line" != *" inet6 "* ]] && return 1
  [[ "$line" != *" scope global "* ]] && return 1

  # Ignore DAD tentative announcements
  [[ "$line" == *" tentative "* ]] && return 1

  return 0
}

log "starting (sync=$SYNC_SCRIPT, config=$CONFIG_URL, debounce=${DEBOUNCE_SECONDS}s, debug=$DEBUG)"

# Optional initial sync on startup
run_sync

while true; do
  log "starting ip monitor address"

  pending=0
  last_event=0

  # Use FD 3 for ip monitor so we can do timed reads.
  exec 3< <(/usr/sbin/ip -ts monitor address 2>&1)
  monitor_pid=$!

  while true; do
    # Timed read so we can evaluate the trailing-edge timer even when no events arrive
    if IFS= read -r -t 1 line <&3; then
      [[ -z "$line" ]] && continue

      if should_trigger "$line"; then
        pending=1
        last_event=$(date +%s)
        if [[ "$DEBUG" == "1" ]]; then
          log "event: $line"
          log "pending set; last_event=$last_event"
        fi
      else
        if [[ "$DEBUG" == "1" ]]; then
          : # keep quiet (or log ignored lines if you want)
        fi
      fi
    else
      # No line arrived within 1s timeout.
      :
    fi

    # If we have pending work and we've been quiet long enough, run sync once.
    if (( pending == 1 )); then
      now=$(date +%s)
      if (( now - last_event >= DEBOUNCE_SECONDS )); then
        pending=0
        if [[ "$DEBUG" == "1" ]]; then
          log "debounce window met (now=$now last_event=$last_event); running sync"
        fi
        run_sync
      fi
    fi

    # If ip monitor died, break and restart it.
    if ! kill -0 "$monitor_pid" 2>/dev/null; then
      if [[ "$DEBUG" == "1" ]]; then
        log "ip monitor process exited"
      fi
      break
    fi
  done

  # Cleanup FD 3
  exec 3<&-

  log "ip monitor exited; restarting in 1s"
  sleep 1
done
