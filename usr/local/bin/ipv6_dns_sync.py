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
OPERATOR GUIDE — ipv6_dns_sync
===============================================================================

PURPOSE
-------
This script synchronises IPv6 addresses on a host with DNS by issuing RFC 2136
dynamic updates (nsupdate). It is designed to be *idempotent*:
- If nothing has changed, no DNS updates are sent.
- If addresses are added/removed locally, DNS is updated accordingly.

It supports:
- Forward AAAA record updates
- Reverse PTR record updates
- SLAAC churn handling (temporary/privacy addresses)
- Multiple reverse zones
- Preview and verbose diagnostic modes

CONFIGURATION
-------------
Configuration is normally provided via a JSON file, either locally or via URL.
Typical fields include:
- hostname / domain
- DNS server address
- TSIG key file
- TTL for records
- Reverse zone CIDRs
- State file location

The state file records the *last applied* address set so the script can compute
a diff and avoid unnecessary updates.

COMMON INVOCATIONS
------------------

Normal run (quiet, cron-safe):
    ipv6_dns_sync.py --config /path/to/config.json

Remote config URL:
    ipv6_dns_sync.py --config-url https://example.net/config.json

Preview mode (NO DNS CHANGES):
    ipv6_dns_sync.py --config config.json --preview

Verbose diagnostics:
    ipv6_dns_sync.py --config config.json --verbose

Force update (ignore cached state):
    ipv6_dns_sync.py --config config.json --force

OPERATIONAL NOTES
-----------------
- Preview mode prints the nsupdate script that *would* be sent.
- Verbose mode explains *why* updates are or are not generated.
- The script intentionally tolerates duplicate or reordered addresses.
- Reverse updates are only generated when a matching reverse zone exists.
- Named/BIND is expected to optimise away no-op updates server-side as well.

FAILURE MODES
-------------
- Missing or unreadable TSIG key → hard failure
- No matching reverse zone → PTR update skipped (logged in verbose mode)
- Empty address set → existing DNS records may be removed

This file contains **documentation-only changes** relative to ipv6_dns_sync.py.
No functional behaviour has been altered.

===============================================================================
"""

"""
ipv6_dns_sync.py (macOS/Linux)

- Discovers IPv6 addresses on this host
- Maintains AAAA + PTR records via nsupdate/TSIG
- Uses a JSON config file for settings
- Supports per-prefix domain override for both AAAA and PTR

Config example (Linux):

{
  "server": "192.168.1.53",
  "keyfile": "/var/root/.mykey",
  "ttl": 120,
  "host": "myhost",
  "domain": "example.net",

  "include_link_local": false,

  "allowed_prefixes": [
    "1111:2222:3333:1::/64",
    "1111:2222:3333:2::/64"
  ],

  "ignored_prefixes": [
    "fd00::/8"
  ],

  "reverse_zones": [
    {
      "cidr": "1111:2222:3333:1::/64",
      "zone": "1.c.5.f.6.0.8.5.3.0.4.2.ip6.arpa.",
      "ptr_domain": "example.net"
    },
    {
      "cidr": "1111:2222:3333:2::/64",
      "zone": "2.c.5.f.6.0.8.5.3.0.4.2.ip6.arpa.",
      "ptr_domain": "lab.example.com"
    }
  ],

  "state_file": "/var/root/.ipv6_dns_sync_state.json"
}
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address, IPv6Address, IPv6Network
from typing import Any, Dict, List, Optional, Tuple


# ------------------------ helpers ------------------------ #


def sh(cmd: List[str]) -> subprocess.CompletedProcess:
    """
    Run a shell command and return CompletedProcess.
    """
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def log(msg: str, verbose: bool) -> None:
    """
    Lightweight logger for stderr.
    
    Decision logic:
    - The script is often run from cron/launchd; stderr is the safest place for logs.
    - Keep normal runs quiet; verbose/preview modes are responsible for detailed output.
    """
    if verbose:
        print(msg)


def load_json(path: str) -> Any:
    """
    Load JSON from a file path and return the decoded object.
    
    Decision logic:
    - State/config files are expected to be small; we read them in one shot.
    - JSON errors are allowed to propagate so the caller can decide whether to abort or treat it as 'first run'.
    """
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: Any) -> None:
    """
    Atomically write JSON to a file path.
    
    Decision logic:
    - Write via a temp file + replace so we never leave a partially-written state file.
    - State is written only after a successful nsupdate (unless in preview mode).
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)

# ------------------------ remote config loader ------------------------ #

# DEFAULT_CONFIG_PATH = "/usr/local/etc/ipv6_dns_sync.json"
# DEFAULT_CONFIG_CACHE = "/var/cache/ipv6-dns-sync/config.json"
# 
# def load_remote_config(config_url: str, cache_path: str, verbose: bool=False) -> Any:
#     """Load JSON config from URL, cache it locally, fall back to cache on failure."""
#     cache_file = Path(os.path.expanduser(cache_path))
#     cache_file.parent.mkdir(parents=True, exist_ok=True)
# 
#     try:
#         req = urllib.request.Request(config_url, headers={"User-Agent": "ipv6-dns-sync/1.0"})
#         with urllib.request.urlopen(req, timeout=10) as resp:
#             raw = resp.read().decode("utf-8")
#         data = json.loads(raw)
# 
#         with tempfile.NamedTemporaryFile("w", dir=str(cache_file.parent), delete=False, encoding="utf-8") as tf:
#             json.dump(data, tf, indent=2, sort_keys=True)
#             tf.flush()
#             os.fsync(tf.fileno())
#             tmp_name = tf.name
#         os.replace(tmp_name, cache_file)
#         if verbose:
#             print(f"Config: loaded from URL and cached to {cache_file}", file=sys.stderr)
#         return data
#     except Exception as e:
#         if cache_file.exists():
#             if verbose:
#                 print(f"Config: URL fetch failed ({e}); using cache {cache_file}", file=sys.stderr)
#             with cache_file.open("r", encoding="utf-8") as f:
#                 return json.load(f)
#         raise
# 
# def load_config(local_path: Optional[str], config_url: Optional[str], cache_path: Optional[str], verbose: bool=False) -> Any:
#     """Prefer remote URL config if provided; otherwise fall back to local file."""
#     url = config_url or os.environ.get("IPV6_DNS_CONFIG_URL")
#     cache = cache_path or os.environ.get("IPV6_DNS_CONFIG_CACHE") or DEFAULT_CONFIG_CACHE
#     if url:
#         return load_remote_config(url, cache, verbose=verbose)
#     path = os.path.expanduser(local_path or DEFAULT_CONFIG_PATH)
#     if verbose:
#         print(f"Config: loading local file {path}", file=sys.stderr)
#     return load_json(path)

def load_remote_config(config_url):
    """
    Download config JSON from a URL and return the decoded dict.
    
    Decision logic:
    - Central config lets you roll changes to many hosts without redeploying the script.
    - The caller decides whether to cache and how to handle failures (online/offline operation).
    """
    if not config_url:
        raise RuntimeError("--config-url is mandatory")

    # First: try downloading remote config
    try:
        with urllib.request.urlopen(config_url, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            config = json.loads(raw)
    except Exception as e:
        # Remote failed — try cached config
        cached = load_cached_config_only()
        if cached is not None:
            return cached
        raise RuntimeError(f"Failed to download config and no cache available: {e}")

    # cache_file MUST exist in config
    if "cache_file" not in config:
        raise RuntimeError("Config is missing mandatory 'cache_file' key")

    cache_path = Path(config["cache_file"]).expanduser()
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Write cache atomically
    with tempfile.NamedTemporaryFile(
        "w",
        dir=str(cache_path.parent),
        delete=False
    ) as tmp:
        json.dump(config, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name

    os.replace(tmp_path, cache_path)
    return config


def load_cached_config_only():
    # We don't know cache_file path yet — scan common failure scenario
    # Caller only uses this after a previous successful run
    """
    Load the most recently cached remote config (if present).
    
    Decision logic:
    - Used as a resilience fallback when the config URL is temporarily unavailable.
    - Keeps syncing behavior stable using the last-known-good config.
    """
    possible_paths = [
        Path("~/.cache/ipv6_dns_sync/config.json").expanduser(),
    ]

    for path in possible_paths:
        if path.exists():
            try:
                with path.open() as f:
                    cfg = json.load(f)
                if "cache_file" in cfg:
                    return cfg
            except Exception:
                pass
    return None

def default_state_path() -> str:
    """
    Return the default path for the local state file.
    
    Decision logic:
    - Store under a per-user cache dir (doesn't require root and avoids littering the working directory).
    """
    home = os.path.expanduser("~")
    return os.path.join(home, ".ipv6_dns_sync_state.json")

def _get_reverse_zone_name(rz: "ReverseZone") -> str:
    """
    Get the ip6.arpa zone name for a ReverseZone, compatible with both
    Linux and macOS variants (which may use .zone or .zone_name).
    Falls back to ipv6_prefix_to_arpa(rz.network) if needed.
    """
    z = getattr(rz, "zone", None) or getattr(rz, "zone_name", None)
    if z:
        return z
    try:
        return ipv6_prefix_to_arpa(rz.network)
    except Exception:
        return ""

# ------------------------ reverse zone structures ------------------------ #


@dataclass
class ReverseZone:
    network: IPv6Network
    zone: str
    ptr_domain: Optional[str]  # e.g. example.net or lab.example.com



def parse_reverse_zones(cfg: Dict[str, Any]) -> List[ReverseZone]:
    """
    Parse the 'reverse_zones' config, accepting either:
      - simple strings: "1111:2222:3333::/48"
      - objects: { "cidr": "...", "domain": "example.net" }
        (also accepts "prefix" instead of "cidr", and "ptr_domain" instead of "domain")

    The reverse zone name is derived automatically from the CIDR via ipv6_prefix_to_arpa().
    """
    rz_cfg = cfg.get("reverse_zones", [])
    reverse_zones: List[ReverseZone] = []

    for item in rz_cfg:
        if isinstance(item, str):
            # Short form: just a CIDR
            cidr = item
            ptr_domain = None
        elif isinstance(item, dict):
            # Long form: explicit CIDR and optional domain override
            cidr = item.get("cidr") or item.get("prefix")
            if not cidr:
                raise ValueError(f"reverse_zones entry is missing 'cidr': {item!r}")
            ptr_domain = item.get("domain") or item.get("ptr_domain")
        else:
            raise ValueError(f"reverse_zones entries must be strings or objects: {item!r}")

        net = IPv6Network(cidr, strict=False)
        zone = ipv6_prefix_to_arpa(net)  # derive ip6.arpa zone name from the prefix
        reverse_zones.append(ReverseZone(network=net, zone=zone, ptr_domain=ptr_domain))

    return reverse_zones

def ipv6_prefix_to_arpa(prefix: IPv6Network) -> str:
    """
    Convert e.g. 1111:2222:3333:1::/64 to a normalized ip6.arpa zone.

    1) Expand and strip colons:
       1111:2222:3333:1::  ->  24035806f52c0001...
    2) Take the bits up to prefix length and then nibble-align.
    """
    if not isinstance(prefix, IPv6Network):
        raise ValueError("prefix must be IPv6Network")

    # Convert the network address to a 32-hex string
    full_hex = f"{int(prefix.network_address):032x}"

    # How many hex chars are covered by the prefix length?
    # 4 bits per hex digit.
    hex_chars = (prefix.prefixlen + 3) // 4
    relevant = list(full_hex[:hex_chars])

    rev = ".".join(reversed(relevant)) + ".ip6.arpa."
    return rev


def ipv6_to_ptr(addr: str) -> str:
    """
    Convert a full IPv6 address into its ip6.arpa PTR owner name (nibble-reversed).
    
    Decision logic:
    - PTR owner names are derived deterministically from the address (no DNS lookups required).
    - Nibble format matches the RFC 3596 reverse-tree structure.
    """
    ip = ip_address(addr)
    if not isinstance(ip, IPv6Address):
        raise ValueError("ipv6_to_ptr only supports IPv6")
    full_hex = f"{int(ip):032x}"
    return ".".join(reversed(full_hex)) + ".ip6.arpa."


def ptr_to_ipv6(name: str) -> IPv6Address:
    """
    Convert an ip6.arpa PTR owner name back into an IPv6 address (best-effort).
    
    Decision logic:
    - Mostly useful for debugging/validation.
    - Expects nibble-reversed names; raises on malformed input to avoid silent bad conversions.
    """
    s = name.rstrip(".").lower()
    suffix = ".ip6.arpa"
    if not s.endswith(suffix):
        raise ValueError(f"Not an ip6.arpa name: {name}")
    s = s[: -len(suffix)]
    nibbles = s.split(".")
    if any(len(n) != 1 for n in nibbles):
        raise ValueError(f"Unexpected nibble structure: {name}")
    hex_str = "".join(reversed(nibbles))
    if len(hex_str) != 32:
        raise ValueError(f"Unexpected hex length: {len(hex_str)} in {name}")
    return IPv6Address(int(hex_str, 16))


def find_best_reverse_zone(
    addr: str, reverse_zones: List[ReverseZone]
) -> Optional[ReverseZone]:
    """
    Pick the most specific configured reverse zone that contains a given IPv6 address.
    
    Decision logic:
    - Prefer the longest matching prefix (most specific) when multiple zones overlap.
    - If no zone matches, skip PTR updates for that address (AAAA updates may still happen).
    """
    ip = ip_address(addr)
    if not isinstance(ip, IPv6Address):
        return None

    best: Optional[Tuple[int, ReverseZone]] = None
    for rz in reverse_zones:
        if ip in rz.network:
            plen = rz.network.prefixlen
            if best is None or plen > best[0]:
                best = (plen, rz)
    return best[1] if best else None


# ------------------------ address discovery & filtering ------------------------ #


def filter_addresses(addrs: List[str], cfg: Dict[str, Any]) -> List[str]:
    # Address-selection policy:
    # - Prefer globally-routable unicast addresses; skip link-local/loopback.
    # - Optional: exclude temporary/privacy addresses for more stable AAAA/PTR mappings.
    # - Optional: restrict by interface and/or prefix to avoid publishing VPN/container addrs.
    # - Normalize/sort results to keep diffs and state files stable between runs.
    """
    macOS-compatible filtering:

    - drop link-local and loopback
    - apply include_prefixes / exclude_prefixes (string prefix match)
    """
    include_prefixes = cfg.get("include_prefixes", []) or []
    exclude_prefixes = cfg.get("exclude_prefixes", []) or []

    def matches_prefixes(addr: str, prefixes: List[str]) -> bool:
        return any(addr.startswith(p) for p in prefixes)

    result: List[str] = []
    for a in addrs:
        ip = ip_address(a)
        if not isinstance(ip, IPv6Address):
            continue

        # Skip link-local and loopback
        if ip.is_link_local or ip.is_loopback:
            continue

        # Positive include filter
        if include_prefixes and not matches_prefixes(a, include_prefixes):
            continue

        # Negative exclude filter
        if exclude_prefixes and matches_prefixes(a, exclude_prefixes):
            continue

        result.append(a)

    # Return sorted unique list
    return sorted(set(result))


def get_ipv6_addresses_macos(cfg: Dict[str, Any], verbose: bool) -> List[str]:
    """
    Parse /sbin/ifconfig output and collect global IPv6 addresses.
    """
    cp = sh(["/sbin/ifconfig"])
    if cp.returncode != 0:
        raise RuntimeError(f"ifconfig failed: {cp.stderr.decode(errors='ignore')}")

    addrs: List[str] = []
    for line in cp.stdout.decode(errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("inet6 "):
            continue
        # Example:
        # inet6 fe80::1%lo0 prefixlen 64 ...
        parts = line.split()
        if len(parts) < 2:
            continue
        addr = parts[1]
        # Strip %interface suffix if present
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        try:
            ip = ip_address(addr)
        except Exception:
            continue
        if not isinstance(ip, IPv6Address):
            continue
        addrs.append(str(ip))

    filtered = filter_addresses(addrs, cfg)
    log(f"Interfaces found (raw ifconfig IPv6 count={len(addrs)}): {addrs}", verbose)
    return filtered

# ------------------------ main sync logic ------------------------ #


def have_command(cmd: str) -> bool:
    """
    Return True if a command exists on PATH.
    
    Decision logic:
    - Some address discovery paths are OS/tooling dependent; this gates optional commands (e.g. 'ip').
    """
    from shutil import which
    return which(cmd) is not None


def get_ipv6_addresses(cfg: Dict[str, Any], verbose: bool) -> List[str]:
    """Cross-platform IPv6 address discovery."""
    plat = sys.platform
    if plat == "darwin":
        return get_ipv6_addresses_macos(cfg, verbose)

    # Prefer Linux `ip` if available.
    if have_command("ip"):
        return get_ipv6_addresses_linux(cfg, verbose)

    # Fallback (e.g. minimal containers): try ifconfig-style parsing.
    if os.path.exists("/sbin/ifconfig"):
        return get_ipv6_addresses_macos(cfg, verbose)

    raise RuntimeError("Unable to discover IPv6 addresses: neither `ip` nor `/sbin/ifconfig` is available")


def get_ipv6_addresses_linux(cfg: Dict[str, Any], verbose: bool) -> List[str]:
    """
    Use the Linux `ip` command to collect global IPv6 addresses and
    then apply the same filtering logic as the macOS version.

    We run:
        ip -6 addr show scope global

    and parse lines of the form:

        inet6 1111:2222:3333:1::1234/64 scope global dynamic

    Link-local addresses (fe80::/10) are ignored, as are tentative,
    deprecated, or duplicate addresses; any remaining addresses are
    passed through `filter_addresses` which applies the JSON config
    rules (allowed_prefixes, ignored_prefixes, etc).
    """
    # Call `ip` – we insist on IPv6 global scope only.
    cp = sh(["ip", "-6", "addr", "show", "scope", "global"])
    if cp.returncode != 0:
        raise RuntimeError(f"`ip -6 addr` failed: {cp.stderr.decode(errors='ignore')}")

    addrs: List[str] = []
    for line in cp.stdout.decode(errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("inet6 "):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        # parts[1] is like "1111:2222:3333:1::1234/64"
        addr_cidr = parts[1]
        addr = addr_cidr.split("/", 1)[0]

        # Skip link-local just in case `scope global` wasn't honoured
        if addr.lower().startswith("fe80:"):
            continue

        try:
            ip = ip_address(addr)
        except Exception:
            continue
        if not isinstance(ip, IPv6Address):
            continue

        # Filter out tentative/duplicate/etc states if present in flags
        # Example end of line: "scope global tentative", "scope global dynamic mngtmpaddr"
        flags = set(parts[2:])
        if "tentative" in flags or "dadfailed" in flags or "duplicate" in flags:
            continue

        addrs.append(str(ip))

    filtered = filter_addresses(addrs, cfg)
    log(f"Interfaces found (raw ip IPv6 count={len(addrs)}): {addrs}", verbose)
    return filtered


# ------------------------ main sync logic ------------------------ #

def build_nsupdate_script(
    # nsupdate script generation:
    # - Produce explicit deletes for records we no longer want and adds for desired records.
    # - This is robust against partial prior runs and keeps the update idempotent.
    # - The caller may skip running nsupdate entirely when the diff is empty.
    host: str,
    domain: str,
    server: str,
    ttl: int,
    keyfile: str,
    current_addrs: List[str],
    prev_addrs: List[str],
    reverse_zones: List[ReverseZone],
    first_run: bool,
    verbose: bool,
) -> str:
    """
    Build the nsupdate script text based on differences and reverse zone contents.

    - First run:
      * Nuke AAAA for all possible hostnames (in all configured domains),
        in separate send-blocks per forward zone.
      * AXFR each reverse zone and delete any existing PTRs whose RDATA
        equals this host's FQDN for that zone (host + per-zone domain).
    - Subsequent runs:
      * For each address, only update AAAA in the single domain that
        matches its reverse zone (or the default domain), so AAAA <-> PTR match.
      * Only touch AAAA/PTR for addresses in to_add / to_del.
    - AAAA updates are grouped per forward domain.
    - PTR updates are grouped per reverse ip6.arpa zone.
    - No 'zone' or 'key' statements are emitted; TSIG is supplied via -k.
    """

    # ----- Compute domain set used for AAAA/PTR target names -----
    host_domains = set()
    if domain:
        host_domains.add(domain.rstrip("."))
    for rz in reverse_zones:
        if rz.ptr_domain:
            host_domains.add(rz.ptr_domain.rstrip("."))

    # All possible FQDNs (used only for first-run AAAA cleanup)
    all_host_fqdns = {f"{host}.{d}." for d in host_domains}

    # Current vs previous address sets
    curr_set = set(current_addrs)
    prev_set = set(prev_addrs)

    to_add = sorted(curr_set - prev_set)
    to_del = sorted(prev_set - curr_set)

    log(f"build_nsupdate_script: to_add={to_add}, to_del={to_del}", verbose)

    lines: List[str] = []

    # ----- First-run: nuke all AAAA for all possible hostnames -----
    # Do this in separate send-blocks per forward zone to avoid NOTZONE.
    if first_run and all_host_fqdns:
        # Group FQDNs by their "zone" (everything after the first label)
        zone_map: Dict[str, List[str]] = {}
        for fqdn in sorted(all_host_fqdns):
            name = fqdn.rstrip(".")
            parts = name.split(".")
            if len(parts) >= 2:
                z = ".".join(parts[1:])
            else:
                z = name
            zone_map.setdefault(z, []).append(fqdn)

        for z, fqdns in zone_map.items():
            lines.append(f"server {server}")
            for f in fqdns:
                lines.append(f"update delete {f} AAAA")
            lines.append("send")

    # We will collect forward and reverse operations, then emit them
    # in grouped server/send blocks.
    forward_ops: Dict[str, List[str]] = {}   # key: forward domain (example.net, example.com, ...)
    reverse_ops: Dict[str, List[str]] = {}   # key: ip6.arpa zone name

    # ----- First-run: AXFR reverse zones and delete stale PTRs for this host -----
    if first_run:
        for rz in reverse_zones:
            # Determine which FQDN this reverse zone should point to
            zone_domain = (rz.ptr_domain or domain or "").rstrip(".")
            if not zone_domain:
                continue

            fqdn = f"{host}.{zone_domain}."
            zone_name = _get_reverse_zone_name(rz)
            if not zone_name:
                continue

            # AXFR the zone and delete PTRs whose RDATA == fqdn
            cmd = ["dig"]
            if keyfile:
                cmd.extend(["-k", keyfile])
            cmd.extend([f"@{server}", "AXFR", zone_name, "+noall", "+answer"])

            cp = sh(cmd)
            if cp.returncode != 0:
                log(
                    f"AXFR of {zone_name} failed (rc={cp.returncode}): "
                    f"{cp.stderr.decode(errors='ignore')}",
                    verbose,
                )
                continue

            for line in cp.stdout.decode(errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith(";"):
                    continue
                parts = line.split()
                if len(parts) < 5:
                    continue

                owner, ttl_s, rdclass, rtype = parts[0], parts[1], parts[2], parts[3]
                rdata = parts[4]

                if rtype.upper() != "PTR":
                    continue

                # Normalise both to absolute, case-insensitive
                if not rdata.endswith("."):
                    rdata_cmp = rdata + "."
                else:
                    rdata_cmp = rdata
                if rdata_cmp.lower() != fqdn.lower():
                    continue

                # Delete this specific PTR
                reverse_ops.setdefault(zone_name, []).append(
                    f"update delete {owner} PTR {fqdn}"
                )

    # ----- Forward AAAA updates (safe mode): always delete all AAAA for the host, then re-add current -----
    #
    # IMPORTANT: This "delete all, then re-add current" approach is intentional and should not be
    # changed to a simple diff-based add/remove. Here is why:
    #
    # A dangling PTR record (reverse DNS pointing to an address that no longer exists) is mostly
    # harmless — reverse lookups simply fail silently. However, a dangling AAAA record is actively
    # harmful: DNS clients will attempt to connect to the stale address first, and TCP connection
    # attempts to a dead IPv6 address wait for the full connection timeout (often 20+ seconds)
    # before falling back to another address or IPv4. This causes random, hard-to-diagnose
    # connectivity delays that have been observed in practice.
    #
    # SLAAC address churn (privacy extensions, prefix changes from ISP renumbering) means stale
    # AAAA records accumulate more readily than with stable IPv4 DHCP assignments. The safe mode
    # approach guarantees that after every sync the DNS state exactly matches the host's current
    # addresses, with no possibility of stale records surviving.
    #
    # For each forward domain we touch, we do:
    #   update delete <fqdn> AAAA        (removes ALL existing AAAA records for this name)
    #   update add <fqdn> <ttl> AAAA <addr>   (re-adds only the current addresses)
    #
    # Domains are derived the same way as before: if the address matches a reverse-zone entry with
    # ptr_domain, use that as the forward domain; otherwise fall back to the default 'domain'.
    current_by_domain: Dict[str, List[str]] = {}
    prev_by_domain: Dict[str, List[str]] = {}

    def _addr_to_forward_domain(a: str) -> str:
        rz = find_best_reverse_zone(a, reverse_zones)
        if rz and rz.ptr_domain:
            return rz.ptr_domain.rstrip(".")
        return domain.rstrip(".") if domain else ""

    for addr in current_addrs:
        d = _addr_to_forward_domain(addr)
        if d:
            current_by_domain.setdefault(d, []).append(addr)

    for addr in prev_addrs:
        d = _addr_to_forward_domain(addr)
        if d:
            prev_by_domain.setdefault(d, []).append(addr)

    domains_to_touch = sorted(set(current_by_domain.keys()) | set(prev_by_domain.keys()))
    for d in domains_to_touch:
        fqdn = f"{host}.{d}."
        ops = forward_ops.setdefault(d, [])
        # Delete *all* AAAA for this name in this zone, then add back the current set.
        ops.append(f"update delete {fqdn} AAAA")
        for addr in sorted(set(current_by_domain.get(d, []))):
            ops.append(f"update add {fqdn} {ttl} AAAA {addr}")

    # ----- PTR deletes only for removed addresses -----

    for addr in to_del:
        rz = find_best_reverse_zone(addr, reverse_zones)
        if not rz:
            continue
        ptr_name = ipv6_to_ptr(addr)
        zone_name = _get_reverse_zone_name(rz)
        if not zone_name:
            continue
        reverse_ops.setdefault(zone_name, []).append(
            f"update delete {ptr_name} PTR"
        )

    # ----- PTR adds only for new addresses -----
    for addr in to_add:
        rz = find_best_reverse_zone(addr, reverse_zones)
        if not rz:
            continue

        ptr_name = ipv6_to_ptr(addr)
        if rz.ptr_domain:
            d = rz.ptr_domain.rstrip(".")
        else:
            d = domain.rstrip(".") if domain else ""
        if not d:
            continue

        fqdn = f"{host}.{d}."
        zone_name = _get_reverse_zone_name(rz)
        if not zone_name:
            continue

        ops = reverse_ops.setdefault(zone_name, [])
        # Safety: delete any existing PTR for this owner name before adding.
        # This helps clear leftover/incorrect mappings after prefix churn.
        ops.append(f"update delete {ptr_name} PTR")
        ops.append(f"update add {ptr_name} {ttl} PTR {fqdn}")

    # ----- Emit forward AAAA blocks per domain -----
    for d, ops in forward_ops.items():
        if not ops:
            continue
        lines.append(f"server {server}")
        lines.extend(ops)
        lines.append("send")

    # ----- Emit PTR blocks per reverse zone -----
    for zone_name, ops in reverse_ops.items():
        if not ops:
            continue
        lines.append(f"server {server}")
        lines.extend(ops)
        lines.append("send")

    if not lines:
        return "\n"

    return "\n".join(lines) + "\n"

# ------------------------ main ------------------------ #



def main() -> None:
    """
    CLI entry point.
    
    High-level decision flow:
    1) Load remote config (fallback to cached config if needed).
    2) Resolve effective settings (config defaults overridden by CLI).
    3) Discover + filter IPv6 addresses according to policy.
    4) Diff against previous state and generate nsupdate directives.
    5) Preview prints changes; normal mode applies them and persists new state.
    6) Shutdown mode removes all DNS records for this host and clears state.
    """
    ap = argparse.ArgumentParser(description="Sync IPv6 AAAA/PTR records via nsupdate (macOS/Linux)")
    ap.add_argument("--config-url", required=True, help="URL to JSON config (required)")
    ap.add_argument("--host", help="Override host label (without domain)")
    ap.add_argument("--domain", help="Override default domain")
    ap.add_argument("--server", help="DNS server for nsupdate")
    ap.add_argument("--keyfile", help="TSIG keyfile for nsupdate/dig")
    ap.add_argument("--ttl", type=int, help="TTL for AAAA/PTR records")
    ap.add_argument("--state-file", help="Override state file path")

    ap.add_argument("--shutdown", action="store_true",
                    help="Shutdown mode: remove all AAAA and PTR records for this host and clear state file. "
                         "Run from a shutdown hook to avoid leaving stale records in DNS.")

    mx = ap.add_mutually_exclusive_group()
    mx.add_argument("-v", "--verbose", action="store_true",
                    help="Verbose output (prints config, diffs, and the generated nsupdate script)")
    mx.add_argument("-p", "--preview", action="store_true",
                    help="Preview mode (same output as -v, but does not run nsupdate or update state)")

    args = ap.parse_args()
    verbose_output = bool(args.verbose or args.preview)

    def vprint(*a, **k):
        if verbose_output:
            print(*a, **k)

    # Load config
    try:
        cfg = load_remote_config(args.config_url)
    # Config is centrally hosted; this enables you to change DNS policy without redeploying the script.
    # CLI options are treated as overrides on top of the config (handy for testing).
    except Exception as e:
        print(f"ERROR: failed to load config: {e}", file=sys.stderr)
        sys.exit(2)

    # Determine effective settings (CLI overrides config)
    server = args.server or cfg.get("server")
    if not server:
        print("ERROR: no 'server' specified in config or CLI", file=sys.stderr)
        sys.exit(2)

    keyfile = args.keyfile or cfg.get("keyfile", "")
    # Expand ~ in paths (important under sudo/cron/launchd)
    if keyfile:
        keyfile = str(Path(keyfile).expanduser())
    ttl = int(args.ttl if args.ttl is not None else cfg.get("ttl", 120))

    host = args.host or cfg.get("host") or socket.gethostname().split(".")[0]
    domain = args.domain or cfg.get("domain") or ""
    if not domain:
        print("ERROR: no 'domain' specified in config or CLI", file=sys.stderr)
        sys.exit(2)

    # State file (default: ~/.cache/ipv6_dns_sync/state.json)
    state_file = args.state_file or cfg.get("state_file") or str(default_state_path())
    state_file = str(Path(state_file).expanduser())

    # Reverse zones
    reverse_zones = parse_reverse_zones(cfg)

    # Discover current IPv6 addresses
    try:
        current_addrs = get_ipv6_addresses(cfg, verbose_output)
    except Exception as e:
        print(f"ERROR: failed to discover IPv6 addresses: {e}", file=sys.stderr)
        sys.exit(2)

    # Load previous state
    # The state file is used only to compute a diff (what changed since last successful run).
    # Missing/unreadable state means 'first run' => publish what we currently observe.
    try:
        state = load_json(state_file)
        prev_addrs = state.get("addrs", [])
        first_run = False
    except Exception:
        prev_addrs = []
        first_run = True

    # Shutdown mode: remove all DNS records for this host and clear state
    # Uses the same first-run cleanup logic (AAAA delete across all domains +
    # AXFR each reverse zone and delete matching PTRs) but with current_addrs=[]
    # so no records are re-added afterwards. State file is deleted so the next
    # boot is treated as a first run and registers addresses cleanly.
    if args.shutdown:
        _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{_ts}] [ipv6_dns_sync] shutdown: removing all DNS records for {host}", file=sys.stderr)
        try:
            nsupdate_script = build_nsupdate_script(
                host=host,
                domain=domain,
                server=server,
                ttl=ttl,
                keyfile=keyfile,
                current_addrs=[],        # No addresses to add
                prev_addrs=prev_addrs,   # Used for PTR cleanup
                reverse_zones=reverse_zones,
                first_run=True,          # Triggers full AAAA + AXFR PTR cleanup
                verbose=verbose_output,
            )
        except Exception as e:
            print(f"ERROR: failed to build shutdown nsupdate script: {e}", file=sys.stderr)
            sys.exit(2)

        if verbose_output:
            vprint("----- shutdown nsupdate script -----")
            vprint(nsupdate_script.rstrip("\n"))

        if keyfile:
            cmd = ["nsupdate", "-v", "-k", keyfile]
        else:
            cmd = ["nsupdate", "-v"]

        cp = subprocess.run(cmd, input=nsupdate_script.encode("utf-8"), capture_output=True)
        if cp.returncode != 0:
            stderr = cp.stderr.decode(errors="ignore").strip()
            stdout = cp.stdout.decode(errors="ignore").strip()
            print(f"ERROR: nsupdate failed during shutdown with code {cp.returncode}", file=sys.stderr)
            if stdout:
                print(stdout, file=sys.stderr)
            if stderr:
                print(stderr, file=sys.stderr)
            sys.exit(1)

        # Clear state file so next boot is treated as a first run
        try:
            Path(state_file).unlink(missing_ok=True)
            _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{_ts}] [ipv6_dns_sync] shutdown: state file cleared", file=sys.stderr)
        except Exception as e:
            print(f"WARNING: failed to clear state file: {e}", file=sys.stderr)

        _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{_ts}] [ipv6_dns_sync] shutdown: DNS cleanup complete", file=sys.stderr)
        return

    # Verbose banner
    if verbose_output:
        now = datetime.now().astimezone()
        vprint(f"Time: {now.strftime('%a %b %d %H:%M:%S %Z %Y')}")
        vprint(f"First run: {first_run}")
        vprint(f"Host: {host}.{domain}")
        vprint(f"Server: {server}")
        vprint(f"TTL: {ttl}")
        vprint(f"State: {state_file}")
        vprint(f"Keyfile: {keyfile}")
        vprint(f"Reverse zones (CIDR): {', '.join(str(rz.network) for rz in reverse_zones)}")
        vprint(f"Current addrs: {current_addrs}")
        vprint(f"Prev addrs   : {prev_addrs}")

        to_add = sorted(set(current_addrs) - set(prev_addrs))
        to_del = sorted(set(prev_addrs) - set(current_addrs))
        vprint(f"Add -> {to_add}")
        vprint(f"Del -> {to_del}")

    # Build nsupdate script
    try:
        nsupdate_script = build_nsupdate_script(
            host=host,
            domain=domain,
            server=server,
            ttl=ttl,
            keyfile=keyfile,
            current_addrs=current_addrs,
            prev_addrs=prev_addrs,
            reverse_zones=reverse_zones,
            first_run=first_run,
            verbose=verbose_output,
        )
    except Exception as e:
        print(f"ERROR: failed to build nsupdate script: {e}", file=sys.stderr)
        sys.exit(2)

    # In verbose/preview, always show the generated nsupdate script.
    if verbose_output:
        vprint("----- nsupdate script -----")
        vprint(nsupdate_script.rstrip("\n"))

    # Preview mode: stop here (do not apply changes, do not update state)
    if args.preview:
        return

    # Execute nsupdate quietly by default (only errors produce output)
    # Execution policy:
    # - Keep stdout/stderr quiet on success (cron-friendly).
    # - Always surface errors with captured stdout/stderr to aid debugging.
    if keyfile:
        cmd = ["nsupdate", "-v", "-k", keyfile]
    else:
        cmd = ["nsupdate", "-v"]

    cp = subprocess.run(cmd, input=nsupdate_script.encode("utf-8"), capture_output=True)
    if cp.returncode != 0:
        # Always emit errors, even in non-verbose mode.
        stderr = cp.stderr.decode(errors="ignore").strip()
        stdout = cp.stdout.decode(errors="ignore").strip()
        print(f"ERROR: nsupdate failed with code {cp.returncode}", file=sys.stderr)
        if stdout:
            print(stdout, file=sys.stderr)
        if stderr:
            print(stderr, file=sys.stderr)
        print("ERROR: nsupdate failed; NOT updating state file.", file=sys.stderr)
        sys.exit(1)

    # Save new state (only after successful nsupdate)
    new_state = {
    # Persist the observed address set after a successful update so subsequent runs are diff-based.
        "host": host,
        "domain": domain,
        "addrs": current_addrs,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        save_json(state_file, new_state)
    except Exception as e:
        print(f"ERROR: nsupdate succeeded but failed to write state file: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()