#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Ivar Hogstad
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See LICENSE file in the project root or <https://www.gnu.org/licenses/>.

# ipv6_dns_shutdown_hook.sh — macOS shutdown hook for IPv6 DNS cleanup
#
# This script is loaded by launchd at boot (RunAtLoad=true, KeepAlive=false).
# It stays alive via "tail -f /dev/null" and waits for SIGTERM, which launchd
# sends to all running daemons during system shutdown. When SIGTERM is received
# the trap fires and runs the sync script with --shutdown to remove all AAAA
# and PTR records from DNS before the machine goes down.
#
# The state file is also cleared here as a safety net, in case the sync
# script is killed by launchd before it can clear it itself. This ensures
# the next boot treats itself as a first run and re-registers all addresses.

SYNC_SCRIPT="/usr/local/bin/ipv6_dns_sync.py"
CONFIG_URL="https://a02.au/ipv6_sync_config/config.json"
VENV_PYTHON="/opt/ipv6-dns-sync/venv/bin/python3"
STATE_FILE="/var/root/.cache/ipv6_dns_sync/state.json"
LOG="/var/log/ipv6_dns_shutdown.err.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ipv6-dns-shutdown] $*" >> "$LOG"
}

shutdown_cleanup() {
    log "SIGTERM received — running DNS cleanup"
    "$VENV_PYTHON" "$SYNC_SCRIPT" --config-url "$CONFIG_URL" --shutdown >> "$LOG" 2>&1

    # Clear the state file as a safety net in case the sync script was
    # killed by launchd before it could do so itself. Without this, the
    # next boot would treat itself as a normal run rather than a first run,
    # and would not re-register addresses that were removed during shutdown.
    if [ -f "$STATE_FILE" ]; then
        rm -f "$STATE_FILE"
        log "state file cleared (safety net)"
    fi

    log "DNS cleanup complete"
    exit 0
}

trap shutdown_cleanup SIGTERM

log "shutdown hook ready"

# Keep the script running until SIGTERM is received
tail -f /dev/null &
wait $!
