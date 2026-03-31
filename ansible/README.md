# Ansible deployment for ipv6_dns_sync

Automates deployment of the IPv6 DNS sync watcher to macOS and Linux (Debian/Ubuntu) hosts.
A single command handles everything — dependencies, scripts, TSIG key, and service setup.

## Requirements

On the machine running Ansible (your local machine):
- Ansible 2.12+: `pip install ansible`

On target hosts:
- SSH access using a key (password-free)
- A user with passwordless sudo privileges

## Directory structure

```
ansible/
├── site.yml                  # Main playbook — always use this
├── site-remove.yml           # Removal playbook
├── ansible.cfg               # Ansible configuration (logging, default inventory)
├── inventory/
│   ├── hosts.yml             # Your hosts — edit this
│   └── host_vars/
│       └── <hostname>.yml    # Per-host variables (TSIG key etc)
└── roles/
    └── ipv6_dns_sync/
        ├── defaults/
        │   └── main.yml      # Variables with defaults — override in host_vars
        ├── tasks/
        │   ├── main.yml      # Entry point, branches per OS
        │   ├── linux.yml     # Linux-specific tasks
        │   ├── macos.yml     # macOS-specific tasks
        │   ├── remove.yml    # Removal entry point
        │   ├── remove_linux.yml  # Linux removal tasks
        │   └── remove_macos.yml  # macOS removal tasks
        ├── templates/
        │   ├── mykey.j2                             # TSIG key file template
        │   └── au.hosteng.ipv6-dns-watch.plist.j2  # macOS launchd plist template
        └── handlers/
            └── main.yml      # Service reload handlers
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

2. **Add your hosts** to `ansible/inventory/hosts.yml`. Put Linux hosts under `linux:` and macOS hosts under `macos:`:
   ```yaml
   all:
     children:
       linux:
         hosts:
           mylinuxhost.example.com:
             ansible_user: root
       macos:
         hosts:
           mymachost.example.com:
             ansible_user: ivar
   ```

3. **Set the TSIG key** for each host:
   ```bash
   mkdir -p ansible/inventory/host_vars
   echo 'ipv6_tsig_key: "your-actual-tsig-key"' > ansible/inventory/host_vars/<hostname>.yml
   ```

4. **Run the playbook** from the repo root:
   ```bash
   ansible-playbook ansible/site.yml --limit myhostname
   ```

That's it. The playbook detects the OS and handles everything automatically:
- **Linux**: installs `bind9-dnsutils`, deploys scripts, enables systemd service
- **macOS**: installs Homebrew + Python 3 (including Xcode CLT), creates Python venv with PyObjC, deploys scripts, loads launchd daemon

## Removing from a host

```bash
ansible-playbook ansible/site-remove.yml --limit myhostname
```

This stops and removes the service and all deployed files. Installed packages (`bind9-dnsutils` on Linux, Homebrew packages on macOS) are left in place.

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

Ansible logs all runs to `~/ansible_runs.log` on the controller machine automatically.

## Overriding variables per host

Any variable in `defaults/main.yml` can be overridden in `host_vars/<hostname>.yml`.

For example, on an Intel Mac where Homebrew lives at `/usr/local`:
```yaml
ipv6_homebrew_python: "/usr/local/bin/python3"
```

## Notes

- The TSIG key is deployed to `/root/.mykey` on Linux and `/var/root/.mykey` on macOS,
  with permissions `0600` (root-readable only).
- The macOS watcher runs in a Python venv at `/opt/ipv6-dns-sync/venv` using
  Homebrew Python. The venv is created automatically during deployment.
- Re-running the playbook is always safe — all tasks are idempotent.
- The macOS bootstrap (Homebrew + Python install) takes 2-3 minutes on first run
  due to downloading packages. Subsequent runs skip it instantly.
