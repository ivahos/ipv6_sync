#!/Users/ivar/.venv/ipv6-dns-sync/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Ivar Hogstad
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See LICENSE file in the project root or <https://www.gnu.org/licenses/>.

"""
ipv6_dns_watch.py — macOS IPv6 change watcher

Listens to SystemConfiguration dynamic store notifications for IPv6
changes and runs ipv6_dns_sync.py whenever addresses change.

Requires:
  - /usr/local/bin/ipv6_dns_sync.py
  - /usr/local/etc/ipv6_dns_sync.json
"""

import os
import subprocess
import sys
import time

from SystemConfiguration import (
    SCDynamicStoreCreate,
    SCDynamicStoreSetNotificationKeys,
    SCDynamicStoreCreateRunLoopSource,
)
from CoreFoundation import (
    CFRunLoopGetCurrent,
    CFRunLoopAddSource,
    CFRunLoopRun,
    kCFRunLoopDefaultMode,
)

SYNC_SCRIPT = "/usr/local/bin/ipv6_dns_sync.py"
CONFIG_PATH = "https://a02.au/ipv6_sync_config/config.json"

# Simple debounce (avoid running the sync too often if multiple events fire quickly)
LAST_RUN = 0
MIN_INTERVAL = 0  # seconds


def run_sync():
    global LAST_RUN
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
        sys.stderr.write(f"[ipv6_dns_watch] sync failed: {e}\n")


def callback(store, changed_keys, info):
    # changed_keys is a CFArray of keys that changed
    try:
        sys.stderr.write(f"[ipv6_dns_watch] IPv6 change: {list(changed_keys)}\n")
    except Exception:
        pass
    run_sync()


def main():
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

    # Block here and handle events forever
    CFRunLoopRun()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        sys.stderr.write(f"[ipv6_dns_watch] fatal error: {e}\n")
        sys.exit(1)
