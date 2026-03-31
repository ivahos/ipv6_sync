# Ansible deployment for ipv6_dns_sync

Automates deployment of the IPv6 DNS sync watcher to macOS and Linux (Debian/Ubuntu) hosts.

## Requirements

On the machine running Ansible (your local machine):
- Ansible 2.12+: `pip install ansible`

On target hosts:
- SSH access with a key already set up
- A user with sudo privileges

## Directory structure

```
ansible/
├── site.yml                  # Main playbook — run this
├── inventory/
│   └── hosts.yml             # Your hosts — edit this
└── roles/
    └── ipv6_dns_sync/
        ├── defaults/
        │   └── main.yml      # Variables with defaults — override in host_vars
        ├── tasks/
        │   ├── main.yml      # Entry point, branches per OS
        │   ├── linux.yml     # Linux-specific tasks
        │   └── macos.yml     # macOS-specific tasks
        ├── templates/
        │   ├── mykey.j2                          # TSIG key file
        │   └── au.hosteng.ipv6-dns-watch.plist.j2  # macOS launchd plist
        └── handlers/
            └── main.yml      # Service reload handlers
```

## Quick start

1. **Add your hosts** to `inventory/hosts.yml`

2. **Set the TSIG key** for each host. Create a file per host at:
   ```
   ansible/inventory/host_vars/<hostname>.yml
   ```
   With contents (plain text for now, see Vault section below for securing it):
   ```yaml
   ipv6_tsig_key: "your-actual-tsig-key-here"
   ```

3. **Run the playbook:**
   ```bash
   cd ansible
   ansible-playbook -i inventory/hosts.yml site.yml
   ```

4. **Deploy to a single host only:**
   ```bash
   ansible-playbook -i inventory/hosts.yml site.yml --limit myhostname
   ```

5. **Dry run** (shows what would change without making changes):
   ```bash
   ansible-playbook -i inventory/hosts.yml site.yml --check
   ```

## Securing the TSIG key with Ansible Vault

Rather than storing the key in plain text, encrypt it with Ansible Vault:

```bash
ansible-vault encrypt_string 'your-actual-tsig-key-here' --name 'ipv6_tsig_key'
```

Paste the output into `host_vars/<hostname>.yml`. Then run the playbook with:
```bash
ansible-playbook -i inventory/hosts.yml site.yml --ask-vault-pass
```

## Overriding variables per host

Any variable in `defaults/main.yml` can be overridden in `host_vars/<hostname>.yml`.

For example, to use a different Python on a specific Mac:
```yaml
ipv6_macos_python: "/usr/local/bin/python3"
```

## Notes

- The TSIG key is deployed to `/root/.mykey` on Linux and `/var/root/.mykey` on macOS,
  with permissions `0600` (root-readable only).
- The macOS watcher runs in a Python venv at `/opt/ipv6-dns-sync/venv` with the
  required PyObjC packages pre-installed. The venv is created using the macOS
  system Python (`/usr/bin/python3`). Override `ipv6_macos_python` in host_vars
  if a different Python is needed on a specific host.
- Re-running the playbook is safe — all tasks are idempotent.
