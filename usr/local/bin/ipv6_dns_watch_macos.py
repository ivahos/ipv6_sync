#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Ivar Hogstad
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See LICENSE file in the project root or <https://www.gnu.org/licenses/>.

"""
ipv6_dns_watch_macos.py — macOS IPv6 change watcher

Listens to SystemConfiguration dynamic store notifications for IPv6
changes and runs ipv6_dns_sync.py whenever addresses change.

On shutdown, launchd sends SIGTERM to all daemons. We catch this signal
and run the sync script with --shutdown to remove all AAAA and PTR records
before the process exits, preventing stale DNS records.

Requires:
  - /usr/local/bin/ipv6_dns_sync.py
"""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from SystemConfiguration import (
    SCDynamicStoreCreate,
    SCDynamicStoreSetNotificationKeys,
    SCDynamicStoreCreateRunLoopSource,
)
from CoreFoundation import (
    CFRunLoopGetCurrent,
    CFRunLoopAddSource,
    CFRunLoopStop,
    kCFRunLoopDefaultMode,
    CFRunLoopRun,
)

def log(msg):
    """Write a timestamped message to stderr."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] [ipv6_dns_watch] {msg}\n")
    sys.stderr.flush()


SYNC_SCRIPT = "/usr/local/bin/ipv6_dns_sync.py"
CONFIG_PATH = "https://a02.au/ipv6_sync_config/config.json"

# Simple debounce (avoid running the sync too often if multiple events fire quickly)
LAST_RUN = 0
MIN_INTERVAL = 0  # seconds

# Shutdown flag — set to True when SIGTERM is received so the callback
# ignores further IPv6 change events during network teardown.
SHUTTING_DOWN = False


def run_sync():
    global LAST_RUN
    # Ignore address change events during shutdown — interfaces going down
    # as the network tears down would otherwise trigger re-registration
    # after the cleanup has already run.
    if SHUTTING_DOWN:
        return

    now = time.time()
    if now - LAST_RUN < MIN_INTERVAL:
        return
    LAST_RUN = now

    cmd = [SYNC_SCRIPT, "--config-url", CONFIG_PATH]
    # You can add "-v" here if you want more verbose logging:
    # cmd.append("-v")

    try:
        subprocess.run(cmd, check=False)
    except Exception as e:
        log(f"sync failed: {e}")


def run_shutdown():
    """
    Run the sync script with --shutdown to remove all DNS records.
    Called when SIGTERM is received (i.e. on system shutdown or service stop).

    launchd sends SIGTERM to all daemons during shutdown. By catching it here
    and running the cleanup before exiting, we ensure DNS is left in a clean
    state — no dangling AAAA records that would cause connectivity delays
    when the machine comes back up or another host tries to reach this one.

    During shutdown, DNS resolution may already be down even though the network
    is still up at the IP level. We therefore pass --config-url but the sync
    script will automatically fall back to the cached config if the URL cannot
    be resolved.
    """
    log("SIGTERM received — running shutdown cleanup")
    try:
        subprocess.run(
            [SYNC_SCRIPT, "--config-url", CONFIG_PATH, "--shutdown"],
            check=False,
            timeout=25,  # launchd's default ExitTimeout is 5s but we set 30s in plist
        )
    except Exception as e:
        log(f"shutdown cleanup failed: {e}")
    log("shutdown cleanup complete")


def handle_sigterm(signum, frame):
    """
    SIGTERM handler — run DNS cleanup and exit.
    Sets SHUTTING_DOWN flag first so any further IPv6 change events from
    network interfaces going down are ignored and don't re-register records
    after the cleanup has run.
    """
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    run_shutdown()

    # Verify network is still up after cleanup by pinging the DNS server.
    # This helps diagnose whether nsupdate could actually reach the server
    # during shutdown, or whether the network was already gone.
    try:
        import re as _re
        # Extract server IP from cached config if available
        _server = None
        _cache = os.path.expanduser("~/.cache/ipv6_dns_sync/cached_config.json")
        if os.path.exists(_cache):
            import json as _json
            with open(_cache) as _f:
                _server = _json.load(_f).get("server")
        if _server:
            _result = subprocess.run(
                ["ping", "-c", "1", "-t", "3", _server],
                capture_output=True, text=True
            )
            if _result.returncode == 0:
                log(f"network check: DNS server {_server} is reachable after cleanup ✓")
            else:
                log(f"network check: DNS server {_server} is NOT reachable after cleanup ✗")
    except Exception as e:
        log(f"network check failed: {e}")

    # Stop the CFRunLoop so CFRunLoopRun() returns and main() can exit cleanly
    CFRunLoopStop(CFRunLoopGetCurrent())
    # Exit immediately after cleanup — don't process any more events
    sys.exit(0)


def callback(store, changed_keys, info):
    # changed_keys is a CFArray of keys that changed
    try:
        log(f"IPv6 change: {list(changed_keys)}")
    except Exception:
        pass
    run_sync()


def main():
    # Register SIGTERM handler so we clean up DNS records on shutdown
    signal.signal(signal.SIGTERM, handle_sigterm)

    # Create a dynamic store with our callback
    store = SCDynamicStoreCreate(
        None,
        "ipv6-dns-sync-watch",
        callback,
        None,
    )

    # We care about IPv6 state on all interfaces + global IPv6 state
    keys = None  # no exact keys
    patterns = [
        "State:/Network/Interface/.*/IPv6",  # any interface's IPv6 state
        "State:/Network/Global/IPv6",        # global IPv6 changes
    ]
    SCDynamicStoreSetNotificationKeys(store, keys, patterns)

    # Attach to the CFRunLoop so the callback is invoked on changes
    rl = CFRunLoopGetCurrent()
    src = SCDynamicStoreCreateRunLoopSource(None, store, 0)
    CFRunLoopAddSource(rl, src, kCFRunLoopDefaultMode)

    # Do an initial sync on startup, so DNS matches current IPs
    run_sync()

    # Block here and handle events forever.
    # CFRunLoopRun() returns when CFRunLoopStop() is called (i.e. on SIGTERM).
    CFRunLoopRun()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        run_shutdown()
    except Exception as e:
        log(f"fatal error: {e}")
        sys.exit(1)
