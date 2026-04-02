# Ansible deployment for ipv6_sync

Automates deployment of the IPv6 DNS sync watcher to macOS and Linux (Debian/Ubuntu/Raspberry Pi OS) hosts.
A single command handles everything — dependencies, scripts, TSIG key, and service setup.

## Requirements

On the machine running Ansible (your controller):
- Ansible 2.12+: `pip install ansible`

On target hosts:
- SSH access using a key (password-free)
- A user with passwordless sudo privileges

## Directory structure

```
ansible/
├── site.yml                  # Main playbook — always run this
├── site-remove.yml           # Removal playbook
├── ansible.cfg               # Ansible configuration (logging, default inventory)
├── inventory/
│   ├── hosts.yml             # Your hosts — edit this (git-ignored, never commit)
│   └── host_vars/
│       └── <hostname>.yml    # Per-host variables: TSIG key etc (git-ignored)
└── roles/
    ├── ipv6_dns_sync/        # Core role: deploys sync scripts and services
    │   ├── defaults/main.yml # Variables with defaults — override in host_vars
    │   ├── tasks/
    │   │   ├── main.yml      # Entry point, deploys sync script, branches per OS
    │   │   ├── linux.yml     # Linux-specific tasks (apt, systemd)
    │   │   ├── macos.yml     # macOS-specific tasks (venv, launchd)
    │   │   ├── remove.yml    # Removal entry point
    │   │   ├── remove_linux.yml
    │   │   └── remove_macos.yml
    │   ├── templates/
    │   │   ├── mykey.j2                             # TSIG key file
    │   │   ├── ipv6-dns-cleanup.service.j2          # Shutdown cleanup service
    │   │   ├── au.hosteng.ipv6-dns-watch.plist.j2  # macOS launchd plist
    │   │   └── au.hosteng.ipv6-dns-shutdown.plist.j2
    │   └── handlers/main.yml # systemd daemon-reload handler
    │
    ├── raspberry_pi/         # Pi role: NVMe boot migration + driver install
    │   ├── defaults/main.yml # NVMe and driver variables
    │   ├── tasks/main.yml    # Full migration sequence (see below)
    │   └── templates/        # Copies of ipv6_dns_sync templates for chroot use
    │
    └── r8152/                # Optional: Realtek USB network driver
        ├── defaults/main.yml # Repo URL, source dir, modprobe conf path
        ├── tasks/main.yml    # Build and install out-of-tree r8152 driver
        ├── handlers/main.yml # Regenerate initramfs on modprobe.d change
        └── files/
            └── realtek-usb.conf  # Blacklists cdc_ncm/cdc_ether
```

Also in the repo root:
```
scripts/
└── bootstrap-macos.sh   # Homebrew + Python bootstrap (called automatically by site.yml)
```

## Quick start

1. **Clone the repo** on your Ansible controller machine:
   ```bash
   git clone https://github.com/ivahos/ipv6_sync.git
   cd ipv6_sync
   ```

2. **Add your hosts** to `ansible/inventory/hosts.yml` (this file is git-ignored — it stays local):
   ```yaml
   all:
     children:
       linux:
         hosts:
           mylinuxhost.example.com:
             ansible_user: ubuntu
       macos:
         hosts:
           mymac.example.com:
             ansible_user: ivar
       raspberry_pi:
         hosts:
           mypi.example.com:
             ansible_host: 192.168.1.x
             ansible_user: ivar
   ```
   See `inventory/hosts.yml` for a full example with all groups.

3. **Set the TSIG key** for each host (also git-ignored):
   ```bash
   mkdir -p ansible/inventory/host_vars
   echo 'ipv6_tsig_key: "your-actual-tsig-key"' > ansible/inventory/host_vars/<hostname>.yml
   ```

4. **Run the playbook** from the repo root:
   ```bash
   ansible-playbook ansible/site.yml --limit myhostname
   ```

The playbook detects the OS and handles everything automatically:
- **Linux**: installs `bind9-dnsutils`, deploys scripts, enables systemd services
- **macOS**: installs Homebrew + Python 3 (including Xcode CLT), creates Python venv with PyObjC, deploys scripts, loads launchd daemon
- **Raspberry Pi**: full NVMe migration sequence (see below), plus r8152 driver if needed

## Inventory groups

| Group | Purpose |
|-------|---------|
| `linux` | Any Linux host running Debian/Ubuntu |
| `macos` | Any macOS host |
| `raspberry_pi` | Pis that need NVMe boot migration; also add to `linux` |
| `r8152` | Hosts with a Realtek USB network adapter needing the out-of-tree driver |

A Raspberry Pi with a Realtek USB adapter should appear in `linux`, `raspberry_pi`.
The r8152 driver is installed automatically into the NVMe chroot by the `raspberry_pi` role,
so you only need the `r8152` group for hosts that are already booted natively (VMs etc.).

## Raspberry Pi NVMe migration

When a host is in the `raspberry_pi` group the playbook detects how it is booted:

**Booted from SD card or USB stick (root not on NVMe):**
Runs the full migration sequence:
1. `e2fsck -fy` — cleans the ext4 filesystem (required after pi-manager first-boot)
2. `btrfs-convert` — converts ext4 → btrfs in-place (~5-15 min on a 4TB drive)
3. `gdisk` — converts MBR → GPT partition table (required for drives >2TB)
4. `parted resizepart 2 100%` — grows partition to fill the entire NVMe
5. Mounts NVMe under `/mnt`, grows btrfs to fill the partition
6. Updates `cmdline.txt` with new btrfs UUID and `rootfstype=btrfs`
7. Rewrites `/etc/fstab` with correct UUIDs and optimised btrfs mount options:
   `defaults,noatime,compress=zstd:3,discard=async,space_cache=v2,autodefrag`
8. Regenerates initramfs inside chroot
9. Installs ipv6_dns_sync into the chroot
10. Builds and installs the r8152 driver into the chroot
11. Unmounts everything — machine is ready to shut down and remove boot media

**Booted from NVMe (migration already done):**
Skips the migration entirely and performs a normal live ipv6_dns_sync install.

### Boot media

Use **Raspberry Pi OS Lite** (not the full desktop image) — it is much smaller and
writes faster. 32GB SD card or USB stick is more than enough. The boot media is
only needed for the initial migration and can be reused for other Pis.

The playbook works with any non-NVMe boot device — SD card (`mmcblk`), USB stick
(`sda`), or any other external media.

### NVMe prerequisites

Write a fresh Raspberry Pi OS image to the NVMe using
[Raspberry Pi Imager](https://www.raspberrypi.com/software/), then boot from it
once to let the first-boot wizard run (creates your user, sets hostname etc.).
Shut down, boot back to the SD card/USB stick, and run the playbook.

The playbook expects the standard two-partition layout:

| Partition | Type  | Label  | Purpose |
|-----------|-------|--------|---------|
| p1 | FAT32 | bootfs | Boot files, firmware, cmdline.txt |
| p2 | ext4  | rootfs | Root filesystem (will be converted to btrfs) |

### Configurable variables

Set in `inventory/host_vars/<hostname>.yml` to override defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `nvme_device` | `/dev/nvme0n1` | NVMe block device |
| `nvme_boot_partition` | `/dev/nvme0n1p1` | FAT32 boot partition |
| `nvme_root_partition` | `/dev/nvme0n1p2` | Root partition to convert |
| `nvme_chroot` | `/mnt` | Chroot mount point |
| `nvme_run_e2fsck` | `true` | Run e2fsck before conversion |
| `nvme_run_btrfs_check` | `false` | Run btrfs check after conversion (slow on large drives) |
| `nvme_convert_timeout` | `3600` | Seconds to wait for btrfs-convert |
| `r8152_repo` | `https://github.com/wget/realtek-r8152-linux.git` | Driver source |

## Realtek r8152 USB driver

The `r8152` role builds and installs the out-of-tree Realtek r8152 driver for USB
network adapters (r8152, r8153, r8156, r8157). Without it the generic `cdc_ncm`
driver loads instead and the adapter shows `Speed: Unknown, Link detected: no`.

Confirmed working with a Realtek r8157 USB 2.5G adapter on Raspberry Pi 500,
Debian Trixie (6.12.47+rpt-rpi-2712), negotiating a full 2500Mb/s link.

**Important:** This is a plain `make`/`make install`, not DKMS. After a kernel
update the module must be rebuilt. Re-running the playbook handles this automatically.

For Raspberry Pi hosts, the driver is built into the NVMe chroot during the
migration — add the host to the `r8152` group only for non-Pi hosts or Pis
already running natively from NVMe.

## Removing from a host

```bash
ansible-playbook ansible/site-remove.yml --limit myhostname
```

Stops and removes the service and all deployed files. Installed packages
(`bind9-dnsutils` on Linux, Homebrew packages on macOS) are left in place.

## Dry run

Preview what would change without making any changes:
```bash
ansible-playbook ansible/site.yml --limit myhostname --check
```

## Securing the TSIG key with Ansible Vault

Rather than storing the key in plain text, encrypt it with Ansible Vault:

```bash
ansible-vault encrypt_string 'your-actual-tsig-key-here' --name 'ipv6_tsig_key'
```

Paste the output into `host_vars/<hostname>.yml`. Then run the playbook with:
```bash
ansible-playbook ansible/site.yml --limit myhostname --ask-vault-pass
```

## Logs

Ansible logs all runs to `~/ansible_runs.log` on the controller machine automatically
(configured in `ansible.cfg`).

## Security note

`ansible/inventory/hosts.yml` and `ansible/inventory/host_vars/` are listed in
`.gitignore` and will never be committed. Keep your real inventory local only.
Do not commit hostnames, IP addresses, or TSIG keys to version control.
