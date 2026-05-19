# mdns_dns_sync

A long-running daemon that listens for multicast DNS (mDNS / RFC 6762)
announcements on a single network interface and synchronises the announced
hostnames and addresses into an authoritative BIND zone via RFC 2136
dynamic updates (`nsupdate` + TSIG).

By default the agent publishes AAAA records and IPv6 PTRs, matching the
original `ipv6_dns_sync` behaviour. Set `publish_v4: true` in the config
(along with `reverse_zones_v4`) to additionally publish A records and IPv4
PTRs from the same mDNS announcements.

This is an alternative deployment model to `ipv6_dns_sync`. Instead of
running a sync script on every host, you run one agent per VLAN/subnet on
a multi-homed LXC container. Each agent:

- Listens on exactly one interface (one VLAN's broadcast domain)
- Hears all mDNS announcements on that link (Avahi on Linux, Bonjour on
  macOS, mDNS Responder on Windows)
- Strips `.local` from announced hostnames and replaces it with a
  configured forward zone (e.g. `wireguard.au` for the wireguard VLAN's
  agent)
- Sends TSIG-signed dynamic updates to BIND with the right AAAA + PTR
  records

mDNS is link-local by design (TTL=255, never routed), so each agent only
ever sees hosts on the link it's bound to. Per-VLAN scoping is implicit.

## Trade-offs vs ipv6_dns_sync

| | `ipv6_dns_sync` (per-host) | `mdns_dns_sync` (per-VLAN agent) |
|---|---|---|
| Deployment | Push script + systemd unit to every host | One container with N config files |
| Trust model | Each host updates only its own records with its own TSIG key | Each agent updates on behalf of any host on its link |
| Coverage | Whatever Ansible reaches | Whatever speaks mDNS on the link |
| Hosts that don't run the script | Not synced | Synced automatically if they speak mDNS |
| Hosts that don't speak mDNS | Synced | Not synced |

You can run both at once — they don't conflict, and `ipv6_dns_sync`'s
"delete all AAAA before re-adding current ones" rule applies identically
to `mdns_dns_sync`.

## Trust model and BIND policy

Because one agent's TSIG key can update records for any host that
announces on the link, you should scope each key in BIND with
`update-policy` so it can only touch names within the matching forward
zone and the matching reverse zone. Example for the wireguard VLAN agent:

```
key "mdns-vlan10" {
    algorithm hmac-sha256;
    secret "....";
};

zone "wireguard.au" {
    type master;
    file "..";
    update-policy {
        grant mdns-vlan10 zonesub AAAA;
    };
};

zone "0.1.0.0.8.b.d.0.1.0.0.2.ip6.arpa" {
    type master;
    file "..";
    update-policy {
        grant mdns-vlan10 zonesub PTR;
    };
};
```

A compromised agent then can only mess with the zones it was supposed to
manage anyway.

For VLANs that may carry untrusted devices (guest, IoT), set
`allowed_hosts` in the agent config to an allowlist of short hostnames.
Without that, any device announcing on the link can claim any name in the
forward zone.

## Deployment

Intended target: a Debian 13 LXC container on Proxmox with a NIC on every
VLAN you want covered. From inside the container the NICs appear as
`eth0`, `eth1`, ... — give each a stable IP and run one systemd instance
of the agent per interface.

Install dependencies:

```
apt install python3-zeroconf bind9-dnsutils
```

(The agent shells out to `nsupdate` from `bind9-dnsutils`, the same way
`ipv6_dns_sync` does.)

Put the script and systemd unit in place:

```
install -m 0755 usr/local/bin/mdns_dns_sync.py /usr/local/bin/
install -m 0644 etc/systemd/system/mdns-dns-sync@.service /etc/systemd/system/
systemctl daemon-reload
```

Create one config per VLAN under `/etc/mdns_dns_sync/`:

```
/etc/mdns_dns_sync/vlan10.json
/etc/mdns_dns_sync/vlan20.json
...
```

Put the matching TSIG keyfile for each agent in the same directory (mode
`0600`, owned by root). Then enable an instance per config:

```
systemctl enable --now mdns-dns-sync@vlan10
systemctl enable --now mdns-dns-sync@vlan20
```

The `@vlan10` instance loads `/etc/mdns_dns_sync/vlan10.json`. Logs go to
journald per-instance:

```
journalctl -u mdns-dns-sync@vlan10 -f
```

## Config schema

See `docs/config.example.json` and the docstring at the top of the script
for the full schema. Required fields: `interface`, `server`, `keyfile`,
`domain`. Everything else has sensible defaults.

## Shadow-mode rollout

Run with `--preview` for the first few weeks. The agent will log the
nsupdate scripts it *would* send but won't actually touch DNS. Compare
that output against what `ipv6_dns_sync` produces on the same hosts
during the same window — if coverage and addresses match, cut over by
removing `--preview` (drop the flag from the systemd unit's ExecStart or
use an override).

## Cleanup on retirement

When retiring an agent (decommissioning a VLAN, replacing the container,
etc.) run it once with `--shutdown` to remove all records it has
published, so the zone doesn't accumulate stale entries:

```
mdns_dns_sync.py --config /etc/mdns_dns_sync/vlan10.json --shutdown
```

The systemd unit also runs this on `systemctl stop`.
