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
│   ├── ipv6_dns_watch_linux.sh    # Linux watcher (bash + ip monitor)
│   └── ipv6_dns_watch_macos.py    # macOS watcher (SystemConfiguration framework)
│
├── etc/systemd/system/
│   ├── ipv6-dns-watch.service     # systemd watcher service (Linux)
│   └── ipv6-dns-cleanup.service   # systemd shutdown cleanup service (Linux)
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
└── ansible/                       # Automated deployment (see ansible/README.md)
    ├── site.yml                   # Main playbook (4 stages)
    ├── site-remove.yml            # Removal playbook
    ├── inventory/
    │   ├── hosts.yml              # Your hosts (git-ignored, keep local)
    │   └── host_vars/<host>.yml   # Per-host TSIG keys (git-ignored, keep local)
    └── roles/
        ├── ipv6_dns_sync/         # Core role: scripts + services
        ├── raspberry_pi/          # Pi role: NVMe migration + chroot install
        └── r8152/                 # Optional: Realtek USB network driver
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

3. Add your hosts to `ansible/inventory/hosts.yml` (this file is git-ignored and stays local):
   ```yaml
   all:
     children:
       linux:
         hosts:
           myhost.example.com:
             ansible_user: ubuntu
       macos:
         hosts:
           mymac.example.com:
             ansible_user: ivar
       raspberry_pi:       # also add Pi hosts to 'linux' above
         hosts:
           mypi.example.com:
             ansible_host: 192.168.1.x
             ansible_user: ivar
   ```

4. Create a file for each host with its TSIG key (also git-ignored):
   ```bash
   mkdir -p ansible/inventory/host_vars
   echo 'ipv6_tsig_key: "your-actual-tsig-key"' > ansible/inventory/host_vars/<hostname>.yml
   ```

5. Run the playbook:
   ```bash
   ansible-playbook ansible/site.yml --limit myhostname
   ```

The playbook handles everything automatically per platform:
- **Linux**: installs `bind9-dnsutils`, deploys scripts, enables systemd services
- **macOS**: installs Homebrew + Xcode CLT + Python 3, creates Python venv with PyObjC, deploys scripts, loads launchd daemon
- **Raspberry Pi**: full NVMe btrfs migration, chroot install, r8152 driver — see `ansible/README.md` for details

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

---

## Raspberry Pi — NVMe boot migration

The Ansible playbook includes specialised support for migrating a Raspberry Pi
from SD card / USB boot to a btrfs NVMe root filesystem. This section explains
what the playbook does, what you need, and how to run it.

### What it does

When a Pi is in the `raspberry_pi` inventory group and is booted from an SD
card, the playbook automatically:

1. Converts the NVMe root partition from ext4 to btrfs in-place
2. Converts the partition table from MBR to GPT (required for drives >2TB)
3. Grows the root partition to fill the entire NVMe device
4. Updates `cmdline.txt` and `fstab` with the new btrfs UUID
5. Regenerates the initramfs inside a chroot
6. Installs ipv6_dns_sync into the chroot
7. Installs the Realtek r8152 USB network driver into the chroot (if applicable)
8. Unmounts everything and leaves the machine ready to shut down

When the Pi is already booted from NVMe (migration already done), the
playbook detects this and performs a normal live ipv6_dns_sync install instead.

### Prerequisites

**SD card / USB boot device:**
Use **Raspberry Pi OS Lite** rather than the full desktop image — it is
significantly smaller and writes much faster to a slow SD card or USB stick.
32GB is more than enough. The boot device is only needed for the migration
and can be discarded afterwards.

The playbook works with any non-NVMe boot device — SD card (`mmcblk`),
USB stick (`sda`), or any other external media. It detects the boot device
by checking whether root is on an NVMe device. If it is, the migration is
skipped and a normal live install is performed instead.

**NVMe drive:**
The playbook expects a standard Raspberry Pi OS image written to the NVMe
by [Raspberry Pi Imager](https://www.raspberrypi.com/software/) (pi-manager),
giving the standard two-partition layout:

| Partition | Type  | Label  | Purpose              |
|-----------|-------|--------|----------------------|
| p1        | FAT32 | bootfs | Boot files, firmware |
| p2        | ext4  | rootfs | Root filesystem      |

**First boot from NVMe:**
After writing the NVMe image with pi-manager, boot from it once to let the
first-boot wizard run (creates your user, sets hostname etc.), then shut down
and boot back to the SD card to run the playbook. The playbook will not work
correctly if the first-boot wizard has not been completed.

**Kernel headers:**
Raspberry Pi OS ships with kernel headers pre-installed. No extra steps needed.

### Inventory setup

Add the Pi to **both** the `linux` and `raspberry_pi` groups in
`ansible/inventory/hosts.yml`:

```yaml
linux:
  hosts:
    mypi.example.com:
      ansible_host: 192.168.1.x
      ansible_user: ivar

raspberry_pi:
  hosts:
    mypi.example.com:
      ansible_host: 192.168.1.x
      ansible_user: ivar
```

If the Pi has a Realtek USB network adapter (r8152/r8153/r8156/r8157) also
add it to the `r8152` group — the driver will be installed into the NVMe
chroot as part of the migration:

```yaml
r8152:
  hosts:
    mypi.example.com:
      ansible_host: 192.168.1.x
      ansible_user: ivar
```

### Running the migration

Boot the Pi from the SD card, then from your Ansible controller:

```bash
ansible-playbook ansible/site.yml --limit mypi.example.com
```

The `btrfs-convert` step is the longest — expect 10–30 minutes depending on
drive size and how much data is on the NVMe. The playbook will show
`ASYNC POLL ... finished=0` during this step — this is normal.

When the playbook completes you will see:

```
NVMe boot preparation is complete. The machine is ready to shut down.
Remove the SD card before powering back on and the Pi will boot from the NVMe drive.
```

Shut the Pi down, remove the SD card, and power it back on. It will boot
from the NVMe into btrfs.

### Post-migration cleanup

After verifying the migrated system boots correctly and everything works,
you can delete the `ext2_saved` rollback subvolume that `btrfs-convert`
created. This reclaims the space used by the original ext4 filesystem
metadata.

**Only do this once you are confident the migration was successful** —
deleting `ext2_saved` makes the conversion irreversible (`btrfs-convert
--rollback` will no longer work).

```bash
# Delete the ext2 rollback subvolume
sudo btrfs subvolume delete /ext2_saved

# Reclaim freed block groups (usually instant at low disk usage)
sudo btrfs balance start -dusage=0 /

# Verify the result
sudo btrfs filesystem usage /
```

### Kernel updates

The r8152 driver is installed as a plain kernel module (not DKMS). After a
kernel update the module will need to be rebuilt. Re-running the playbook
after a kernel update will rebuild and reinstall the module automatically —
either via the `r8152` inventory group (live install) or by running the
full playbook from SD card again.

### Configurable variables

All variables have sensible defaults and can be overridden in
`inventory/host_vars/<hostname>.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `nvme_device` | `/dev/nvme0n1` | NVMe block device |
| `nvme_boot_partition` | `/dev/nvme0n1p1` | FAT32 boot partition |
| `nvme_root_partition` | `/dev/nvme0n1p2` | Root partition to convert |
| `nvme_chroot` | `/mnt` | Mount point for chroot |
| `nvme_run_e2fsck` | `true` | Run e2fsck before btrfs-convert |
| `nvme_run_btrfs_check` | `false` | Run btrfs check after convert (slow on large drives) |
| `nvme_convert_timeout` | `3600` | Seconds to wait for btrfs-convert |
| `r8152_repo` | `https://github.com/wget/realtek-r8152-linux.git` | Driver source repo |

---

## Licence

GPL v3 — see [LICENSE](LICENSE) for details. Any derivative works must also be released under GPL v3.
