#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Ivar Hogstad
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See LICENSE file in the project root or <https://www.gnu.org/licenses/>.

"""
===============================================================================
OPERATOR GUIDE - mdns_dns_sync
===============================================================================

PURPOSE
-------
This is a long-running daemon that listens for multicast DNS (mDNS / RFC 6762)
announcements on a single network interface and synchronises the announced
hostnames and addresses into an authoritative BIND zone via RFC 2136 dynamic
updates (nsupdate + TSIG).

By default the agent publishes AAAA records and v6 PTRs, matching the original
ipv6_dns_sync behaviour. Set `publish_v4: true` in the config to additionally
publish A records and v4 PTRs from the same observed mDNS announcements.

It is intended as an alternative to deploying the per-host ipv6_dns_sync script
on every machine: instead, one agent runs per VLAN/subnet in an LXC container
that has a NIC on every VLAN, and each agent instance translates .local
announcements into updates against a configured zone.

DESIGN NOTES
------------
- mDNS is link-local (TTL=255, never routed), so each agent only sees hosts on
  the link it is bound to. Per-VLAN scoping is implicit in interface binding.
- The agent uses python-zeroconf's RecordUpdateListener to observe raw A/AAAA
  record updates - not ServiceBrowser, which only sees service registrations
  (_http._tcp etc), and would miss hosts that announce a name without a service.
- Per-host A and AAAA state is maintained in memory, tracked separately by
  family. On each update we apply the "delete all <record-type>, then re-add
  current" rule from ipv6_dns_sync because dangling A or AAAA records cause
  TCP connection-timeout delays in clients. The two families are reconciled
  independently of each other.
- Forward and reverse zones are also tracked per-family: configure
  `reverse_zones` for v6 reverses (ip6.arpa) and `reverse_zones_v4` for v4
  reverses (in-addr.arpa).
- IPv6 link-local (fe80::/10) and IPv4 link-local (169.254.0.0/16, APIPA) are
  always filtered out before being written to DNS.

ALLOWLIST / SAFETY
------------------
Unlike ipv6_dns_sync, where each host updates only its own records using its
own TSIG key, this agent updates records on behalf of arbitrary machines that
announce themselves on the link. To limit blast radius:

  - The TSIG key used should be scoped in BIND (update-policy / grant) so it
    can only update names within the configured forward zone and the matching
    reverse zone(s).
  - The config file supports an optional `allowed_hosts` allowlist (list of
    short hostnames). When present, only announcements from those names are
    acted on; everything else is logged and ignored.
  - When absent, all announcements on the link are accepted. This is fine on
    a trusted management VLAN, less fine on a guest/IoT VLAN.

CONFIGURATION
-------------
Configuration is a JSON file passed via --config. Example:

{
  "interface": "192.0.2.10",
  "server": "192.0.2.53",
  "keyfile": "/etc/mdns_dns_sync/mdns-vlan10.key",
  "domain": "example.net",
  "ttl": 120,

  "publish_v4": true,

  "reverse_zones": [
    "1111:2222:3333:10::/64"
  ],

  "reverse_zones_v4": [
    "192.0.2.0/24"
  ],

  "include_prefixes": [
    "1111:2222:3333:10:"
  ],
  "exclude_prefixes": [],

  "include_prefixes_v4": [
    "192.0.2."
  ],
  "exclude_prefixes_v4": [],

  "allowed_hosts": null,

  "host_timeout": 3600,
  "state_file": "/var/cache/mdns_dns_sync/state.json"
}

Field semantics:
  interface       IPv4 or IPv6 address of the local interface to bind to.
                  This is how zeroconf scopes its multicast group membership
                  to a single link. Required.
  server          DNS server (BIND) to send nsupdate to. Required.
  keyfile         BIND-format TSIG keyfile (passed to nsupdate -k). Required.
  domain          Forward zone to update. ".local" in mDNS names is replaced
                  with this domain. Required.
  ttl             TTL for A/AAAA/PTR records. Default 120.
  publish_v4      If true, also publish A records and v4 PTRs (when the
                  matching reverse zone is configured). Default false, which
                  preserves the v6-only behaviour of ipv6_dns_sync. Note that
                  include_prefixes/exclude_prefixes only filter IPv6 addresses;
                  IPv4 addresses are always published if publish_v4 is on and
                  they're not link-local or loopback.
  reverse_zones   List of IPv6 CIDRs that the agent is authoritative for.
                  PTRs (ip6.arpa) are only written for addresses inside one of
                  these. The reverse zone name is derived from the CIDR.
  reverse_zones_v4  List of IPv4 CIDRs. v4 PTRs (in-addr.arpa) are only
                    written for addresses inside one of these. Only relevant
                    when publish_v4 is true.
  include_prefixes  Optional. If present, only IPv6 addresses starting with
                    one of these strings are published. Use this to scope
                    each per-VLAN agent to its own prefix as defense against
                    cross-VLAN multicast leakage (e.g. Windows hosts that
                    autoconfigure on every prefix they see).
  exclude_prefixes  Optional. IPv6 addresses starting with these strings are
                    skipped.
  include_prefixes_v4  Optional. Same as include_prefixes but for IPv4
                       (dotted-decimal string-prefix match). e.g.
                       ["192.168.123."] confines a VLAN 1 agent to its own
                       v4 subnet.
  exclude_prefixes_v4  Optional. IPv4 counterpart of exclude_prefixes.
  allowed_hosts   Optional list of short hostnames (without .local). If
                  present, announcements from other hosts are ignored.
                  null/missing = accept everything on the link.
  host_timeout    Seconds. If a host hasn't announced anything for this long,
                  its A/AAAA/PTR records are removed. Catches sleeping laptops
                  that don't send mDNS "goodbye" packets. Default 3600.
  state_file      Path to a JSON file where last-published state per host is
                  persisted, so we can compute diffs across restarts and
                  remove orphaned records on shutdown. Required.

COMMON INVOCATIONS
------------------
Run in the foreground (systemd-friendly):
    mdns_dns_sync.py --config /etc/mdns_dns_sync/vlan10.json

Preview mode - print what would be sent to nsupdate, don't actually update:
    mdns_dns_sync.py --config /etc/mdns_dns_sync/vlan10.json --preview

Verbose - log every observed announcement and decision:
    mdns_dns_sync.py --config /etc/mdns_dns_sync/vlan10.json --verbose

Cleanup mode - remove all records for hosts in state_file and exit:
    mdns_dns_sync.py --config /etc/mdns_dns_sync/vlan10.json --shutdown

OPERATIONAL NOTES
-----------------
- Preview mode prints the nsupdate script that *would* be sent for each
  observed change and does not invoke nsupdate or update state. Use this
  during initial deployment to verify coverage before going live.
- The daemon logs to stderr; under systemd it goes to journald.
- mDNS sees announcements only after they happen, so a host that has just
  changed addresses may take a few seconds (Avahi default reannounce delay)
  to be reflected in DNS.

FAILURE MODES
-------------
- Missing keyfile -> hard failure at startup
- nsupdate returning non-zero -> logged, state NOT updated for that host
  (so the next announcement triggers another attempt)
- Address neither in include_prefixes nor in any reverse_zone -> AAAA still
  published if the host is allowlisted; PTR is skipped (logged at verbose).

===============================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address, IPv4Address, IPv4Network, IPv6Address, IPv6Network
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# python-zeroconf
# Debian package: python3-zeroconf
from zeroconf import (
    DNSAddress,
    DNSRecord,
    IPVersion,
    RecordUpdate,
    Zeroconf,
)
from zeroconf import _dns as _zc_dns  # for _TYPE_A / _TYPE_AAAA constants
from zeroconf._updates import RecordUpdateListener


# ------------------------ constants ------------------------ #

# RFC 1035 record-type values, exposed by zeroconf but pinned here in case
# the internal module path changes between library versions.
_TYPE_A = 1
_TYPE_AAAA = 28


# ------------------------ logging ------------------------ #

log = logging.getLogger("mdns_dns_sync")


def setup_logging(verbose: bool) -> None:
    """
    Configure stderr logging with a timestamp prefix.

    Under systemd this output goes to journald, which adds its own timestamp,
    but the inline timestamp is still handy when running the daemon by hand
    or piping to a file.
    """
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)
    log.addHandler(handler)
    log.setLevel(level)


# ------------------------ config ------------------------ #


@dataclass
class Config:
    """
    Parsed configuration, with sensible defaults filled in.
    """
    interface: str
    server: str
    keyfile: str
    domain: str
    ttl: int = 120
    publish_v4: bool = False
    reverse_zones: List[IPv6Network] = field(default_factory=list)
    reverse_zones_v4: List[IPv4Network] = field(default_factory=list)
    include_prefixes: List[str] = field(default_factory=list)
    exclude_prefixes: List[str] = field(default_factory=list)
    include_prefixes_v4: List[str] = field(default_factory=list)
    exclude_prefixes_v4: List[str] = field(default_factory=list)
    allowed_hosts: Optional[Set[str]] = None
    host_timeout: int = 3600
    state_file: str = "/var/cache/mdns_dns_sync/state.json"


def load_config(path: str) -> Config:
    """
    Load JSON config from disk and return a typed Config.

    Decision logic:
    - Required fields fail loudly with a clear message.
    - reverse_zones may be omitted (then no PTRs are written, AAAA only).
    - allowed_hosts is normalised to a set of lowercase short names.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    required = ["interface", "server", "keyfile", "domain"]
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise ValueError(f"config missing required fields: {', '.join(missing)}")

    rev_zones: List[IPv6Network] = []
    for entry in raw.get("reverse_zones", []) or []:
        # Accept either a plain CIDR string or an object with a 'cidr' key,
        # for forward compatibility with the ipv6_dns_sync config format.
        if isinstance(entry, dict):
            cidr = entry.get("cidr")
        else:
            cidr = entry
        if not cidr:
            continue
        rev_zones.append(IPv6Network(cidr, strict=False))

    rev_zones_v4: List[IPv4Network] = []
    for entry in raw.get("reverse_zones_v4", []) or []:
        if isinstance(entry, dict):
            cidr = entry.get("cidr")
        else:
            cidr = entry
        if not cidr:
            continue
        rev_zones_v4.append(IPv4Network(cidr, strict=False))

    allowed = raw.get("allowed_hosts")
    if allowed is not None:
        allowed = {h.strip().lower() for h in allowed if h and h.strip()}

    return Config(
        interface=str(raw["interface"]),
        server=str(raw["server"]),
        keyfile=str(Path(raw["keyfile"]).expanduser()),
        domain=str(raw["domain"]).rstrip("."),
        ttl=int(raw.get("ttl", 120)),
        publish_v4=bool(raw.get("publish_v4", False)),
        reverse_zones=rev_zones,
        reverse_zones_v4=rev_zones_v4,
        include_prefixes=list(raw.get("include_prefixes", []) or []),
        exclude_prefixes=list(raw.get("exclude_prefixes", []) or []),
        include_prefixes_v4=list(raw.get("include_prefixes_v4", []) or []),
        exclude_prefixes_v4=list(raw.get("exclude_prefixes_v4", []) or []),
        allowed_hosts=allowed,
        host_timeout=int(raw.get("host_timeout", 3600)),
        state_file=str(Path(raw.get("state_file", "/var/cache/mdns_dns_sync/state.json")).expanduser()),
    )


# ------------------------ helpers ------------------------ #


def ipv6_to_ptr(addr: str) -> str:
    """
    Convert an IPv6 address into its ip6.arpa PTR owner name (RFC 3596).

    Matches the implementation in ipv6_dns_sync for consistency.
    """
    ip = ip_address(addr)
    if not isinstance(ip, IPv6Address):
        raise ValueError("ipv6_to_ptr only supports IPv6")
    full_hex = f"{int(ip):032x}"
    return ".".join(reversed(full_hex)) + ".ip6.arpa."


def ipv4_to_ptr(addr: str) -> str:
    """
    Convert an IPv4 address into its in-addr.arpa PTR owner name.

    e.g. 192.0.2.5 -> 5.2.0.192.in-addr.arpa.
    """
    ip = ip_address(addr)
    if not isinstance(ip, IPv4Address):
        raise ValueError("ipv4_to_ptr only supports IPv4")
    return ".".join(reversed(str(ip).split("."))) + ".in-addr.arpa."


def reverse_zone_name(net: IPv6Network) -> str:
    """
    Derive the ip6.arpa zone name from an IPv6 network.

    The prefix must be a multiple of 4 bits (nibble-aligned); BIND's reverse
    delegations almost always are. If it isn't we still produce the closest
    nibble-aligned zone, matching how delegations are typically set up.
    """
    if not isinstance(net.network_address, IPv6Address):
        raise ValueError("reverse_zone_name only supports IPv6")
    nibbles = (net.prefixlen + 3) // 4
    full_hex = f"{int(net.network_address):032x}"
    head = full_hex[:nibbles]
    return ".".join(reversed(head)) + ".ip6.arpa."


def reverse_zone_name_v4(net: IPv4Network) -> str:
    """
    Derive the in-addr.arpa zone name from an IPv4 network.

    Only octet-aligned prefixes (/8, /16, /24, /32) map cleanly to a single
    classful reverse zone. For non-octet-aligned prefixes (RFC 2317 style)
    BIND uses subdomain delegations whose names vary by site convention, so
    we default to the containing /24's reverse zone, which works for the
    vast majority of home/lab setups.
    """
    if not isinstance(net.network_address, IPv4Address):
        raise ValueError("reverse_zone_name_v4 only supports IPv4")
    octets = str(net.network_address).split(".")
    if net.prefixlen >= 24:
        return ".".join(reversed(octets[:3])) + ".in-addr.arpa."
    elif net.prefixlen >= 16:
        return ".".join(reversed(octets[:2])) + ".in-addr.arpa."
    elif net.prefixlen >= 8:
        return octets[0] + ".in-addr.arpa."
    else:
        # Below /8 doesn't realistically occur on a LAN; fall back to /8.
        return octets[0] + ".in-addr.arpa."


def find_reverse_zone(addr: str, zones: List[IPv6Network]) -> Optional[IPv6Network]:
    """
    Return the most specific configured v6 reverse zone containing this
    address, or None if no zone matches. Longest-prefix match.
    """
    try:
        ip = ip_address(addr)
    except ValueError:
        return None
    if not isinstance(ip, IPv6Address):
        return None
    best: Optional[Tuple[int, IPv6Network]] = None
    for net in zones:
        if ip in net:
            if best is None or net.prefixlen > best[0]:
                best = (net.prefixlen, net)
    return best[1] if best else None


def find_reverse_zone_v4(addr: str, zones: List[IPv4Network]) -> Optional[IPv4Network]:
    """
    Return the most specific configured v4 reverse zone containing this
    address, or None if no zone matches. Longest-prefix match.
    """
    try:
        ip = ip_address(addr)
    except ValueError:
        return None
    if not isinstance(ip, IPv4Address):
        return None
    best: Optional[Tuple[int, IPv4Network]] = None
    for net in zones:
        if ip in net:
            if best is None or net.prefixlen > best[0]:
                best = (net.prefixlen, net)
    return best[1] if best else None


def addr_passes_filters(addr: str, cfg: Config) -> bool:
    """
    Apply per-family eligibility checks:

    - IPv6: drop link-local / loopback / unspecified; apply
      include_prefixes / exclude_prefixes (string-prefix match on the
      canonical form).
    - IPv4: drop link-local (169.254/16) / loopback / unspecified; apply
      include_prefixes_v4 / exclude_prefixes_v4 (string-prefix match on
      the dotted-decimal form). Also requires publish_v4 to be true.

    Mirrors filter_addresses() in ipv6_dns_sync for the v6 path.
    """
    try:
        ip = ip_address(addr)
    except ValueError:
        return False
    if ip.is_link_local or ip.is_loopback or ip.is_unspecified:
        return False
    if isinstance(ip, IPv6Address):
        if cfg.include_prefixes and not any(addr.startswith(p) for p in cfg.include_prefixes):
            return False
        if cfg.exclude_prefixes and any(addr.startswith(p) for p in cfg.exclude_prefixes):
            return False
        return True
    if isinstance(ip, IPv4Address):
        # Only publish v4 if explicitly enabled in the config.
        if not cfg.publish_v4:
            return False
        if cfg.include_prefixes_v4 and not any(addr.startswith(p) for p in cfg.include_prefixes_v4):
            return False
        if cfg.exclude_prefixes_v4 and any(addr.startswith(p) for p in cfg.exclude_prefixes_v4):
            return False
        return True
    return False


def mdns_name_to_shortname(name: str) -> Optional[str]:
    """
    Extract the short hostname from an mDNS owner name.

    mDNS hostnames look like 'pi500plus.local.' - we want 'pi500plus'.
    Returns None for names that don't end in .local. (or .local).
    Names with more than one label before .local are also rejected (those
    would be service-instance names like 'My Printer._ipp._tcp.local.').
    """
    n = name.rstrip(".").lower()
    if not n.endswith(".local"):
        return None
    head = n[: -len(".local")]
    if not head or "." in head:
        return None
    return head


# ------------------------ state ------------------------ #


@dataclass
class HostState:
    """
    Per-host runtime state, tracked per address family.

    `published_v4` and `published_v6` are the sets of addresses currently in
    DNS (i.e. what we last successfully nsupdate'd into the zone). Tracking
    them separately means the v4 and v6 reconciliations can succeed or fail
    independently without confusing each other's "what's published?" state.

    `last_seen` tracks when we last received any mDNS announcement for this
    host (of either family), used to expire stale entries.
    """
    published_v4: Set[str] = field(default_factory=set)
    published_v6: Set[str] = field(default_factory=set)
    last_seen: float = 0.0

    def published_for(self, family: str) -> Set[str]:
        """family is 'v4' or 'v6'."""
        return self.published_v4 if family == "v4" else self.published_v6

    def set_published_for(self, family: str, value: Set[str]) -> None:
        if family == "v4":
            self.published_v4 = value
        else:
            self.published_v6 = value


class State:
    """
    Thread-safe per-host state with JSON persistence.
    """

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.hosts: Dict[str, HostState] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("could not read state file %s: %s; starting empty", self.path, e)
            return
        for host, entry in raw.get("hosts", {}).items():
            # Backwards compat: older state files had a single "published"
            # field containing only IPv6 addresses. Migrate those into
            # published_v6 and start published_v4 empty.
            v4 = set(entry.get("published_v4", []))
            v6 = set(entry.get("published_v6", []))
            if not v4 and not v6 and "published" in entry:
                for a in entry["published"]:
                    try:
                        ip = ip_address(a)
                    except ValueError:
                        continue
                    if isinstance(ip, IPv4Address):
                        v4.add(a)
                    elif isinstance(ip, IPv6Address):
                        v6.add(a)
            self.hosts[host] = HostState(
                published_v4=v4,
                published_v6=v6,
                last_seen=float(entry.get("last_seen", 0.0)),
            )

    def save(self) -> None:
        """Atomic write: temp file + rename, matching ipv6_dns_sync style."""
        out = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "hosts": {
                h: {
                    "published_v4": sorted(s.published_v4),
                    "published_v6": sorted(s.published_v6),
                    "last_seen": s.last_seen,
                }
                for h, s in self.hosts.items()
            },
        }
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=str(Path(self.path).parent),
            delete=False,
            encoding="utf-8",
        ) as tf:
            json.dump(out, tf, indent=2, sort_keys=True)
            tf.flush()
            os.fsync(tf.fileno())
            tmp = tf.name
        os.replace(tmp, self.path)


# ------------------------ nsupdate ------------------------ #


def build_nsupdate_script(
    host: str,
    cfg: Config,
    current_v6: Set[str],
    prev_v6: Set[str],
    current_v4: Optional[Set[str]] = None,
    prev_v4: Optional[Set[str]] = None,
) -> str:
    """
    Build an nsupdate script that brings DNS into the state described by
    current_v6 (and current_v4, when publish_v4 is enabled) for this host.

    The strategy is "delete all <type>, then re-add current" - the same approach
    used by ipv6_dns_sync. A dangling A or AAAA record causes ~20s TCP
    connection-timeout delays in clients, so we must never leave stale forward
    records in DNS, even at the cost of an unnecessary delete on a record that
    didn't change.

    The two families are handled independently:
      - AAAA: delete-all-then-readd, plus per-address PTR diff against the
        configured v6 reverse zones.
      - A: delete-all-then-readd (only if publish_v4 is true), plus per-address
        PTR diff against the configured v4 reverse zones.

    Each family's forward delete/add happens in its own nsupdate send-block.
    Reverse blocks are grouped per zone.

    Returns the nsupdate script text. If there's nothing to do, returns "".
    """
    fqdn = f"{host}.{cfg.domain}."
    lines: List[str] = []

    # Default the v4 args so callers in v6-only mode don't have to pass them.
    if current_v4 is None:
        current_v4 = set()
    if prev_v4 is None:
        prev_v4 = set()

    def _forward_block(rtype: str, current: Iterable[str], prev: Set[str]) -> None:
        """
        Emit one server/update/send block for the given record type, applying
        the delete-all-then-readd rule. Only emits if there's a change or this
        is the host's first publish.
        """
        fwd = sorted(a for a in current if addr_passes_filters(a, cfg))
        changed = set(fwd) != prev or (not prev and fwd)
        if not changed:
            return
        lines.append(f"server {cfg.server}")
        lines.append(f"update delete {fqdn} {rtype}")
        for a in fwd:
            lines.append(f"update add {fqdn} {cfg.ttl} {rtype} {a}")
        lines.append("send")

    def _reverse_ops(
        family: str,
        current: Iterable[str],
        prev: Set[str],
        ops_by_zone: Dict[str, List[str]],
    ) -> None:
        """
        Compute per-address PTR adds/removes for one family and accumulate
        them into ops_by_zone (keyed by reverse zone name).
        """
        passing = {a for a in current if addr_passes_filters(a, cfg)}
        to_add = passing - prev
        to_del = prev - passing

        if family == "v6":
            zones = cfg.reverse_zones
            find = lambda a: find_reverse_zone(a, zones)
            zone_name = reverse_zone_name
            to_ptr = ipv6_to_ptr
        else:
            zones = cfg.reverse_zones_v4
            find = lambda a: find_reverse_zone_v4(a, zones)
            zone_name = reverse_zone_name_v4
            to_ptr = ipv4_to_ptr

        for a in sorted(to_del):
            rz = find(a)
            if rz is None:
                continue
            zn = zone_name(rz)
            ops_by_zone.setdefault(zn, []).append(
                f"update delete {to_ptr(a)} PTR"
            )

        for a in sorted(to_add):
            rz = find(a)
            if rz is None:
                log.debug("no %s reverse zone for %s, skipping PTR", family, a)
                continue
            zn = zone_name(rz)
            # Safety: delete any existing PTR for this owner name before
            # adding. Clears stale mappings from a previous run that died
            # between add and delete on the same name.
            ops_by_zone.setdefault(zn, []).append(f"update delete {to_ptr(a)} PTR")
            ops_by_zone.setdefault(zn, []).append(
                f"update add {to_ptr(a)} {cfg.ttl} PTR {fqdn}"
            )

    # ---- Forward AAAA ----
    _forward_block("AAAA", current_v6, prev_v6)

    # ---- Forward A (only if v4 publishing is enabled) ----
    if cfg.publish_v4:
        _forward_block("A", current_v4, prev_v4)

    # ---- Reverse: collect all PTR ops, then emit grouped by zone ----
    rev_ops: Dict[str, List[str]] = {}
    _reverse_ops("v6", current_v6, prev_v6, rev_ops)
    if cfg.publish_v4:
        _reverse_ops("v4", current_v4, prev_v4, rev_ops)

    for _zn, ops in rev_ops.items():
        if not ops:
            continue
        lines.append(f"server {cfg.server}")
        lines.extend(ops)
        lines.append("send")

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def run_nsupdate(script: str, cfg: Config, preview: bool) -> bool:
    """
    Send a script to nsupdate. Returns True on success (or in preview mode).

    In preview mode we just log the script and return True; nothing touches
    DNS or state.
    """
    if not script:
        return True

    if preview:
        log.info("PREVIEW: would send the following nsupdate script:\n%s", script.rstrip("\n"))
        return True

    cmd = ["nsupdate", "-v", "-k", cfg.keyfile]
    cp = subprocess.run(cmd, input=script.encode("utf-8"), capture_output=True)
    if cp.returncode != 0:
        log.error(
            "nsupdate failed rc=%d stdout=%r stderr=%r",
            cp.returncode,
            cp.stdout.decode(errors="ignore").strip(),
            cp.stderr.decode(errors="ignore").strip(),
        )
        return False
    log.debug("nsupdate ok")
    return True


# ------------------------ mDNS listener ------------------------ #


class MdnsRecordListener(RecordUpdateListener):
    """
    Receives every A/AAAA record update zeroconf sees on the bound interface.

    For each batch of updates we group by hostname, look up the host's
    currently-announced AAAA set, and call sync_host() to reconcile with DNS.

    Note: zeroconf maintains its own cache of records seen on the link with
    TTLs from the announcements. We always re-query the cache for the host's
    AAAA records rather than tracking adds/removes incrementally, because
    the cache already does TTL expiry for us.
    """

    def __init__(self, zc: Zeroconf, agent: "Agent"):
        self.zc = zc
        self.agent = agent

    def async_update_records(
        self,
        zc: Zeroconf,
        now: float,
        records: List[RecordUpdate],
    ) -> None:
        """
        Called by zeroconf for each batch of record updates (one batch per
        incoming packet or cache event). We collect the distinct names that
        had A/AAAA records change, then trigger a reconcile for each.

        This callback runs in the zeroconf event loop, so we must not block.
        We push reconcile work into the agent's queue and return.
        """
        names_changed: Set[str] = set()
        for ru in records:
            rec = ru.new
            if not isinstance(rec, DNSAddress):
                continue
            if rec.type not in (_TYPE_A, _TYPE_AAAA):
                continue
            names_changed.add(rec.name)

        for name in names_changed:
            short = mdns_name_to_shortname(name)
            if short is None:
                # not a host announcement, e.g. a service-instance name
                continue
            self.agent.enqueue_host(short, name)


# ------------------------ agent ------------------------ #


class Agent:
    """
    Top-level coordinator: owns the Zeroconf instance, the state, and the
    reconcile worker thread.

    Reconciles are serialised through a single worker thread to keep nsupdate
    invocations ordered and to avoid two threads racing on the same host's
    state. Throughput is irrelevant - we expect at most a few updates per
    second across the whole link, even on a busy network.
    """

    def __init__(self, cfg: Config, preview: bool):
        self.cfg = cfg
        self.preview = preview
        self.state = State(cfg.state_file)
        self.queue_lock = threading.Lock()
        self.pending: Dict[str, str] = {}  # short_hostname -> full mDNS name
        self.queue_event = threading.Event()
        self.stop_event = threading.Event()
        self.zc: Optional[Zeroconf] = None

    # ---- enqueue / worker ----

    def enqueue_host(self, short: str, full_name: str) -> None:
        """Called from the zeroconf event loop; thread-safe."""
        with self.queue_lock:
            self.pending[short] = full_name
        self.queue_event.set()

    def worker_loop(self) -> None:
        """
        Pull pending hosts from the queue and reconcile each.

        Also periodically scans for stale hosts (no announcements within
        host_timeout) and removes their records.
        """
        last_sweep = 0.0
        sweep_interval = max(60, self.cfg.host_timeout // 4)

        while not self.stop_event.is_set():
            self.queue_event.wait(timeout=sweep_interval)
            self.queue_event.clear()

            # Drain the pending set
            with self.queue_lock:
                batch = dict(self.pending)
                self.pending.clear()

            for short, full_name in batch.items():
                if self.stop_event.is_set():
                    break
                try:
                    self.reconcile_host(short, full_name)
                except Exception as e:
                    log.exception("reconcile failed for %s: %s", short, e)

            # Periodic stale-host sweep
            now = time.time()
            if now - last_sweep >= sweep_interval:
                last_sweep = now
                try:
                    self.sweep_stale(now)
                except Exception as e:
                    log.exception("sweep_stale failed: %s", e)

    # ---- reconcile a single host ----

    def reconcile_host(self, short: str, full_name: str) -> None:
        """
        Look up the host's currently-announced A and AAAA addresses (from the
        zeroconf cache), compute the per-family diff against last-published
        state, and run nsupdate if needed.

        Empty-cache protection (per family): if zeroconf's cache reports zero
        addresses for a family but we previously published some, treat that as
        a transient TTL gap rather than a real removal. We keep the previously
        published set in memory and let sweep_stale() do the cleanup if the
        host genuinely stays silent for host_timeout seconds.

        The reason: Avahi (and other mDNS responders) re-announce at intervals
        well shorter than the record TTL, but record updates arrive packet by
        packet. A host can briefly have, say, its A record expired in the
        cache while its AAAA is still live - that gap is normal and not a
        signal that the host has removed an address. Without this guard, the
        agent would tear down A records every few minutes for healthy hosts.
        """
        if self.cfg.allowed_hosts is not None and short not in self.cfg.allowed_hosts:
            log.debug("ignoring %s (not in allowed_hosts)", short)
            return

        announced_v6, announced_v4 = self.lookup_addrs(full_name)

        # We always look at AAAA; we only consider A if v4 publishing is on.
        if not self.cfg.publish_v4:
            announced_v4 = set()

        if not announced_v6 and not announced_v4:
            # Host has nothing usable in the cache right now. Could be a TTL
            # expiry across both families simultaneously, or the host only
            # announced something we filtered out. Don't tear down records;
            # sweep_stale() will handle a genuinely departed host.
            log.debug("%s: nothing to publish at this moment, skipping", short)
            self._touch(short)
            return

        with self.state.lock:
            hs = self.state.hosts.setdefault(short, HostState())
            prev_v6 = set(hs.published_v6)
            prev_v4 = set(hs.published_v4)
            hs.last_seen = time.time()

        # Per-family transient-empty protection: if this family had records
        # before and the cache currently shows zero, treat it as a TTL gap
        # and pretend nothing changed for this family. The other family can
        # still reconcile if it has real news to report.
        effective_v6 = announced_v6 if announced_v6 or not prev_v6 else prev_v6
        effective_v4 = announced_v4 if announced_v4 or not prev_v4 else prev_v4
        if effective_v6 is prev_v6 and prev_v6:
            log.debug("%s: v6 cache transiently empty, keeping prev=%s", short, sorted(prev_v6))
        if effective_v4 is prev_v4 and prev_v4:
            log.debug("%s: v4 cache transiently empty, keeping prev=%s", short, sorted(prev_v4))

        script = build_nsupdate_script(
            short, self.cfg,
            current_v6=effective_v6, prev_v6=prev_v6,
            current_v4=effective_v4, prev_v4=prev_v4,
        )
        if not script:
            log.debug("%s: no change", short)
            return

        # Log the diff per family. Only include v4 in the log if it's active
        # or has been published before, to keep the line short in v6-only mode.
        if self.cfg.publish_v4 or prev_v4 or announced_v4:
            log.info(
                "%s: v6 prev=%s announced=%s | v4 prev=%s announced=%s",
                short,
                sorted(prev_v6), sorted(effective_v6),
                sorted(prev_v4), sorted(effective_v4),
            )
        else:
            log.info(
                "%s: prev=%s announced=%s",
                short, sorted(prev_v6), sorted(effective_v6),
            )

        if run_nsupdate(script, self.cfg, self.preview):
            # Update in-memory state regardless of preview mode, so that
            # repeated announcements of the same address set from the same
            # host don't keep generating "would-send" events. Only persist
            # to disk when not in preview mode.
            with self.state.lock:
                hs = self.state.hosts.setdefault(short, HostState())
                hs.published_v6 = {
                    a for a in effective_v6 if addr_passes_filters(a, self.cfg)
                }
                if self.cfg.publish_v4:
                    hs.published_v4 = {
                        a for a in effective_v4 if addr_passes_filters(a, self.cfg)
                    }
                hs.last_seen = time.time()
            if not self.preview:
                self.state.save()

    def lookup_addrs(self, full_name: str) -> Tuple[Set[str], Set[str]]:
        """
        Return (v6_addresses, v4_addresses) currently in the zeroconf cache
        for this owner name. Each set is already filtered by addr_passes_filters.

        We rely on the zeroconf cache's own TTL tracking rather than building
        our own TTL state - the library already handles RFC 6762 expiry.
        """
        if self.zc is None:
            return set(), set()
        v6_addrs: Set[str] = set()
        v4_addrs: Set[str] = set()
        # The cache stores records by name lowercased and trailing-dot-normalised.
        cache_name = full_name.lower()
        if not cache_name.endswith("."):
            cache_name += "."

        # AAAA
        for rec in self.zc.cache.get_all_by_details(cache_name, _TYPE_AAAA, 1):
            if not isinstance(rec, DNSAddress):
                continue
            try:
                ip = ip_address(rec.address) if isinstance(rec.address, str) else IPv6Address(rec.address)
            except Exception:
                continue
            if not isinstance(ip, IPv6Address):
                continue
            s = str(ip)
            if addr_passes_filters(s, self.cfg):
                v6_addrs.add(s)

        # A - we always read these, but reconcile_host discards them if
        # publish_v4 is off. Reading is cheap; this keeps the data flow
        # symmetric and means turning publish_v4 on at runtime doesn't need
        # to wait for the next mDNS announcement to populate v4.
        for rec in self.zc.cache.get_all_by_details(cache_name, _TYPE_A, 1):
            if not isinstance(rec, DNSAddress):
                continue
            try:
                ip = ip_address(rec.address) if isinstance(rec.address, str) else IPv4Address(rec.address)
            except Exception:
                continue
            if not isinstance(ip, IPv4Address):
                continue
            s = str(ip)
            # Only call addr_passes_filters once we know publish_v4 is on -
            # otherwise it would reject the v4 address unconditionally and
            # we'd never even see it in the lookup result. We let v4 through
            # here and let reconcile_host's publish_v4 check decide whether
            # to use it.
            if self.cfg.publish_v4 and not addr_passes_filters(s, self.cfg):
                continue
            v4_addrs.add(s)

        return v6_addrs, v4_addrs

    def _touch(self, short: str) -> None:
        with self.state.lock:
            hs = self.state.hosts.setdefault(short, HostState())
            hs.last_seen = time.time()

    # ---- stale host sweep ----

    def sweep_stale(self, now: float) -> None:
        """
        Remove records for hosts that haven't been seen in host_timeout
        seconds. Catches sleeping laptops and devices that disappear without
        sending mDNS goodbye packets. Both v4 and v6 records are removed
        together since "host is gone" applies to both families.
        """
        stale: List[str] = []
        with self.state.lock:
            for short, hs in self.state.hosts.items():
                if not hs.published_v6 and not hs.published_v4:
                    continue
                if now - hs.last_seen > self.cfg.host_timeout:
                    stale.append(short)

        for short in stale:
            with self.state.lock:
                prev_v6 = set(self.state.hosts[short].published_v6)
                prev_v4 = set(self.state.hosts[short].published_v4)
                age = now - self.state.hosts[short].last_seen
            log.info("%s: timed out, removing records (last_seen %.0fs ago)",
                     short, age)
            script = build_nsupdate_script(
                short, self.cfg,
                current_v6=set(), prev_v6=prev_v6,
                current_v4=set(), prev_v4=prev_v4,
            )
            if script and run_nsupdate(script, self.cfg, self.preview):
                # Clear in-memory state regardless of preview mode so a
                # subsequent re-announcement (or the next sweep) doesn't keep
                # generating the same removal.
                with self.state.lock:
                    self.state.hosts[short].published_v6 = set()
                    self.state.hosts[short].published_v4 = set()
                if not self.preview:
                    self.state.save()

    # ---- lifecycle ----

    def run(self) -> int:
        """
        Start zeroconf, install the listener, run the worker loop until
        signalled. Returns process exit code.
        """
        # Resolve interface argument.
        # zeroconf accepts a list of interface IPs as strings, and joins the
        # mDNS multicast groups on those interfaces only.
        iface = self.cfg.interface
        try:
            iface_ip = ip_address(iface)
        except ValueError:
            log.error("config 'interface' must be an IP address, got %r", iface)
            return 2

        ip_version = IPVersion.V4Only if isinstance(iface_ip, IPv4Address) else IPVersion.V6Only

        log.info(
            "starting mdns_dns_sync: interface=%s server=%s domain=%s "
            "publish_v4=%s preview=%s",
            iface, self.cfg.server, self.cfg.domain,
            self.cfg.publish_v4, self.preview,
        )
        if self.cfg.allowed_hosts is not None:
            log.info("allowed_hosts: %s", sorted(self.cfg.allowed_hosts))
        else:
            log.info("allowed_hosts: (none configured - accepting all on link)")

        self.zc = Zeroconf(interfaces=[iface], ip_version=ip_version)
        listener = MdnsRecordListener(self.zc, self)
        # Passing question=None registers for ALL record updates, not just
        # those matching a specific question. This is what we want - we're
        # passively observing the link, not querying.
        self.zc.add_listener(listener, None)

        worker = threading.Thread(target=self.worker_loop, daemon=True, name="mdns-reconcile")
        worker.start()

        def handle_signal(signum, _frame):
            log.info("signal %s received, shutting down", signum)
            self.stop_event.set()
            self.queue_event.set()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(timeout=1.0)
        finally:
            try:
                self.zc.close()
            except Exception:
                pass
            worker.join(timeout=5)
            log.info("shutdown complete")
        return 0

    # ---- one-shot cleanup ----

    def shutdown_cleanup(self) -> int:
        """
        Remove all records (v4 and v6) for every host currently in state_file
        and clear state. Run this when retiring the agent for a VLAN so you
        don't leave stale records in the zone.
        """
        log.info("shutdown cleanup: removing all known host records")
        with self.state.lock:
            known = dict(self.state.hosts)
        for short, hs in known.items():
            if not hs.published_v6 and not hs.published_v4:
                continue
            script = build_nsupdate_script(
                short, self.cfg,
                current_v6=set(), prev_v6=set(hs.published_v6),
                current_v4=set(), prev_v4=set(hs.published_v4),
            )
            if script and run_nsupdate(script, self.cfg, self.preview):
                if not self.preview:
                    with self.state.lock:
                        self.state.hosts[short].published_v6 = set()
                        self.state.hosts[short].published_v4 = set()
        if not self.preview:
            self.state.save()
        return 0


# ------------------------ main ------------------------ #


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Listen for mDNS announcements on a single interface and "
                    "sync hostnames into a BIND zone via nsupdate+TSIG. "
                    "AAAA records are published by default; set publish_v4 in "
                    "the config to also publish A records.",
    )
    ap.add_argument(
        "--config", required=True,
        help="Path to JSON config file (see top of script for schema).",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose (DEBUG-level) logging. Combinable with --preview.",
    )
    ap.add_argument(
        "-p", "--preview", action="store_true",
        help="Preview mode: log nsupdate scripts but do not run them and do "
             "not persist state to disk. In-memory state is still tracked so "
             "you see realistic diffs across multiple announcements from the "
             "same host.",
    )
    ap.add_argument(
        "--shutdown", action="store_true",
        help="One-shot: remove records for every host in state_file and exit. "
             "Use when retiring this agent so stale records don't linger.",
    )
    args = ap.parse_args()

    setup_logging(verbose=args.verbose)

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"ERROR: failed to load config {args.config}: {e}", file=sys.stderr)
        return 2

    if not Path(cfg.keyfile).is_file():
        print(f"ERROR: TSIG keyfile not found: {cfg.keyfile}", file=sys.stderr)
        return 2

    agent = Agent(cfg, preview=args.preview)

    if args.shutdown:
        return agent.shutdown_cleanup()

    return agent.run()


if __name__ == "__main__":
    sys.exit(main())
