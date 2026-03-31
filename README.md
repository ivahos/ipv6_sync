# ipv6_sync

Keeps DNS continuously in sync with the IPv6 addresses assigned to a host, so that every address on every host always resolves to the correct hostname. This makes network debugging significantly easier — rather than staring at raw IPv6 addresses, tools like `ping`, `traceroute`, `ssh` and log files will show meaningful hostnames.

As a side effect it also handles prefix changes (e.g. when your ISP renumbers), but the primary goal is simply to have clean, reliable forward and reverse DNS for all your hosts at all times.

Both `AAAA` and `PTR` records are kept in sync using RFC 2136 dynamic updates (`nsupdate`) authenticated with a TSIG key.

Supports **macOS** and **Linux** (Debian/Ubuntu).

---

## How it works

There are two components that work together on each host:

### 1. Watcher
Monitors the host's network interfaces for IPv6 address changes.

- **Linux** (`ipv6_dns_watch_linux.sh`) — a bash script that runs `ip monitor address`, filters for global IPv6 events, and uses a trailing-edge debounce (waits until changes have been quiet for 10 seconds) before triggering a sync. Managed by systemd.
- **macOS** (`ipv6_dns_watch_macos.py`) — a Python script that listens to macOS SystemConfiguration framework notifications for IPv6 changes. Managed by launchd.

Both watchers call the sync script when a relevant change is detected, and also run an initial sync on startup.

### 2. Sync script
`ipv6_dns_sync.py` does the actual DNS work. It runs on both platforms.

On each invocation it:
1. Downloads its configuration from a central URL (with local cache fallback if the URL is unreachable)
2. Discovers the host's current global IPv6 addresses
3. Compares them against the last known state (stored in a local state file)
4. Builds and runs an `nsupdate` script to add/remove `AAAA` and `PTR` records as needed
5. Saves the new state on success

Updates are idempotent — if nothing has changed, no DNS updates are sent.

### Configuration
The sync script fetches its config from a URL at runtime. The config specifies:
- DNS server address
- TSIG key file path
- Default domain and TTL
- Allowed/excluded address prefixes
- Reverse zones (each optionally mapped to a different domain)

A reference copy of the config is in [`docs/config.json`](docs/config.json).

### TSIG authentication
DNS updates are authenticated using a TSIG key stored in `~/.mykey` (which resolves to `/var/root/.mykey` on macOS and `/root/.mykey` on Linux). The key file is in BIND format:

```
key "keyname" {
    algorithm hmac-sha256;
    secret "your-secret-here";
};
```

---

## Repository layout

```
├── usr/local/bin/
│   ├── ipv6_dns_sync.py           # Sync script (macOS + Linux)
│   ├── ipv6_dns_watch_linux.sh    # Linux watcher
│   └── ipv6_dns_watch_macos.py    # macOS watcher
│
├── etc/systemd/system/
│   └── ipv6-dns-watch.service     # systemd service unit (Linux)
│
├── Library/LaunchDaemons/
│   └── au.hosteng.ipv6-dns-watch.plist  # launchd daemon config (macOS)
│
├── var/root/
│   └── .mykey                     # TSIG key placeholder (macOS path)
│
├── docs/
│   └── config.json                # Reference config (no secrets)
│
├── scripts/
│   └── bootstrap-macos.sh         # Homebrew + Python bootstrap (called by Ansible)
│
└── ansible/                       # Automated deployment (see below)
```

---

## Installation

### Automated (recommended) — Ansible

The `ansible/` directory contains a playbook that deploys everything to any number of hosts automatically. See [`ansible/README.md`](ansible/README.md) for full details.

**Quick start:**

1. Install Ansible on your local machine:
   ```bash
   pip install ansible
   ```

2. Clone this repo:
   ```bash
   git clone https://github.com/ivahos/ipv6_sync.git
   cd ipv6_sync
   ```

3. Add your hosts to `ansible/inventory/hosts.yml`

4. Create a file for each host with its TSIG key:
   ```bash
   mkdir -p ansible/inventory/host_vars
   echo 'ipv6_tsig_key: "your-actual-tsig-key"' > ansible/inventory/host_vars/<hostname>.yml
   ```

5. Run the playbook:
   ```bash
   ansible-playbook ansible/site.yml --limit myhostname
   ```

The playbook handles everything automatically per platform:
- **Linux**: installs `bind9-dnsutils`, deploys scripts, enables systemd service
- **macOS**: installs Homebrew + Xcode CLT + Python 3, creates Python venv with PyObjC, deploys scripts, loads launchd daemon

---

### Manual installation

#### Linux (Debian/Ubuntu)

```bash
# Install nsupdate
apt install bind9-dnsutils

# Deploy scripts
cp usr/local/bin/ipv6_dns_sync.py /usr/local/bin/
cp usr/local/bin/ipv6_dns_watch_linux.sh /usr/local/bin/
chmod +x /usr/local/bin/ipv6_dns_sync.py /usr/local/bin/ipv6_dns_watch_linux.sh

# Deploy service
cp etc/systemd/system/ipv6-dns-watch.service /etc/systemd/system/

# Write the TSIG key
install -m 600 /dev/null /root/.mykey
# Edit /root/.mykey and add your key in BIND format (see above)

# Create cache directory
mkdir -p /root/.cache/ipv6_dns_sync

# Enable and start
systemctl daemon-reload
systemctl enable --now ipv6-dns-watch
```

#### macOS

```bash
# Install Homebrew (also installs Xcode Command Line Tools)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python 3
brew install python3

# Deploy scripts
cp usr/local/bin/ipv6_dns_sync.py /usr/local/bin/
cp usr/local/bin/ipv6_dns_watch_macos.py /usr/local/bin/
chmod +x /usr/local/bin/ipv6_dns_sync.py /usr/local/bin/ipv6_dns_watch_macos.py

# Create Python venv and install required PyObjC packages
python3 -m venv /opt/ipv6-dns-sync/venv
/opt/ipv6-dns-sync/venv/bin/pip install pyobjc

# Deploy launchd plist
# Note: edit the plist first to set the correct venv Python path
sudo cp Library/LaunchDaemons/au.hosteng.ipv6-dns-watch.plist /Library/LaunchDaemons/

# Write the TSIG key
sudo install -m 600 /dev/null /var/root/.mykey
# Edit /var/root/.mykey and add your key in BIND format (see above)

# Create cache directory
sudo mkdir -p /var/root/.cache/ipv6_dns_sync

# Load the service
sudo launchctl bootstrap system /Library/LaunchDaemons/au.hosteng.ipv6-dns-watch.plist
```

---

## Verifying it works

Check the watcher is running:

```bash
# Linux
systemctl status ipv6-dns-watch

# macOS
sudo launchctl print system/au.hosteng.ipv6-dns-watch
```

Run the sync script manually in verbose mode to see what it would do:

```bash
sudo /usr/local/bin/ipv6_dns_sync.py --config-url https://your-config-url/config.json -v
```

Or in preview mode (no DNS changes made):

```bash
sudo /usr/local/bin/ipv6_dns_sync.py --config-url https://your-config-url/config.json --preview
```

---

## Logs

| Platform | File |
|----------|------|
| Linux (watcher) | `/var/log/ipv6_watch.out` and `/var/log/ipv6_watch.err` |
| macOS (watcher) | `/var/log/ipv6_dns_watch.out.log` and `/var/log/ipv6_dns_watch.err.log` |
