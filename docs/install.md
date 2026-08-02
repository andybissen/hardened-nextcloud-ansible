# Initial Installation

First-time deployment of the stack onto a fresh Rocky Linux 9 VPS. Run
everything from this repo's root directory on your control machine.

> This procedure is destructive-safe on a *new* host but assumes the host
> is empty. It installs Docker, hardens the host, and brings the whole
> stack up from nothing.

---

## 1. Prerequisites

On the **control machine** (where you run Ansible):

- `ansible-core` and the `gpg` CLI installed.
- SSH access to the VPS as `root` with the key referenced in
  `inventory.ini` (`~/.ssh/id_ed25519` by default).

On the **VPS**:

- Fresh Rocky Linux 9/10, reachable over SSH.

In **DNS**:

- An `A` (and `AAAA` if you have IPv6) record for `nc.example.com` pointing
  at the VPS's public IP. **This must resolve before you deploy** — Traefik's
  ACME HTTP-01 challenge fails without it, and you can get rate-limited by
  Let's Encrypt for repeated failures.

---

## 2. One-time setup

First, create your working config from the tracked `*.example` templates. These
real files are **gitignored** so your secrets, domain, and bucket name never
get committed:

```bash
cp group_vars/all.yml.example    group_vars/all.yml
cp group_vars/vault.yml.example  group_vars/vault.yml
cp inventory.ini.example         inventory.ini
```

Then edit `inventory.ini` to point at your VPS (host + SSH user/key), and work
through the steps below to fill in `all.yml` and `vault.yml`.

### 2a. Trust the host key

```bash
ssh-keyscan -H nc.example.com >> ~/.ssh/known_hosts
```

### 2b. Install the required Ansible collections

```bash
ansible-galaxy collection install -r requirements.yml
```

### 2c. Fill in and encrypt secrets

The `group_vars/vault.yml` you just copied has placeholder values. Edit it
with real secrets, then encrypt it in place:

```bash
# edit group_vars/vault.yml — set real values for all of:
#   vault_postgres_password
#   vault_nextcloud_admin_password
#   vault_valkey_password
#
# Only needed if you're enabling optional S3 primary storage (see step 2e
# below) — leave these commented out otherwise:
#   vault_s3_key_id
#   vault_s3_secret_key
#   vault_s3_sse_c_key        (generate with: openssl rand -base64 32)

ansible-vault encrypt group_vars/vault.yml
```

Notes:

- **Keep the Valkey password alphanumeric.** It is interpolated into a
  shell healthcheck; characters like `$`, `"`, or `\` will break the
  rendered compose file.
- **Guard `vault_s3_sse_c_key` like the vault password itself.** It is the
  client-side encryption key for your files in your S3-compatible store. If
  it's ever lost, the data encrypted with it is permanently unrecoverable —
  the provider never stores it. (Not every S3-compatible provider supports
  SSE-C — confirm yours does before relying on this.)
- Store all of these in a password manager as well. `vault.yml` is **not**
  included in backups (see [backup.md](backup.md)).

### 2d. Set up the backup encryption key

Backups are encrypted to a GPG public key. Export yours and point the
config at it:

```bash
gpg --export --armor <your-key-id-or-email> > files/backup_public_key.asc
```

Then set `gpg_recipient_key_id` in `group_vars/all.yml` to that same
id/email/fingerprint.

> You need this in place before the first backup or upgrade (upgrades run a
> backup first). It is not strictly required for the very first install, but
> set it now so you don't forget.

### 2e. (Optional) Fill in S3 primary storage details

S3 primary storage (any S3-compatible provider) is off by default — see the S3
section in `all.yml.example`. It's the storage model this repo is built and
tested around, so it's recommended, but skip this step if you'd rather use
local disk under `nc_data`; nothing else needs to change for that path.

To enable it, uncomment the S3 section in `group_vars/all.yml` and fill in
the placeholder endpoint values from your bucket's details page in your
provider's console:

```yaml
s3_bucket:   "your-bucket-name"
s3_region:   "us-west-004"                       # e.g.
s3_hostname: "s3.us-west-004.backblazeb2.com"    # e.g.
```

Then add `vault_s3_key_id`, `vault_s3_secret_key`, and `vault_s3_sse_c_key`
to `group_vars/vault.yml` (see the vault.yml.example comments — generate
the SSE-C key with `openssl rand -base64 32`).

---

## 3. Confirm connectivity

```bash
ansible -i inventory.ini nextcloud_servers -m ansible.builtin.ping
```

You should get a green `pong`. If not, fix SSH/DNS before continuing.

---

## 4. Deploy

```bash
ansible-playbook playbook.yml --ask-vault-pass
```

This will, in order:

1. **Pre-flight validation** — confirms the host is RHEL-family, that
   `all.yml`/`vault.yml` aren't still full of `*.example` placeholders, that
   `hostname` resolves in DNS (Traefik's cert challenge needs it), and — if
   S3 is enabled — that the SSE-C key is well-formed and the endpoint is
   reachable. Fails here, before touching the host, if something's off. Also
   warns (without blocking) if the configured `*_cpus`/`*_memory_limit`
   values in `all.yml` add up to more than this host actually has — the
   `*.example` defaults are sized for a mid-range VPS, and a smaller host
   can end up oversubscribed even though every service's own limit is
   still individually enforced.
2. Install Docker CE + plugins, enable SELinux container confinement.
3. Configure firewalld (open 80/443) and the DOCKER-USER backstop that
   blocks external access to the Postgres (5432) and Valkey (6379) ports.
4. Configure journald log retention.
5. Scan all stack images with Grype (informational — findings are printed,
   nothing fails the run).
6. Render `docker-compose.yml`, the Traefik dynamic config, app config, and
   the file-based Docker secrets onto the host.
7. Bring the whole stack up and pull images.
8. Apply the config that only `occ` can set, once Nextcloud answers: the
   maintenance window hour, routing Nextcloud's log through journald, and
   `nextcloud_allowed_admin_ranges` if you set one.

First boot takes a few minutes: Nextcloud runs its install routine, and
Traefik requests the TLS certificate.

> **Validate your config without deploying:** run just the pre-flight checks
> as a dry run with `ansible-playbook playbook.yml --ask-vault-pass --tags preflight`.
>
> It reports any placeholder/DNS/config problems and changes nothing on the host.

When you're just iterating on config (templates, `all.yml` tuning) rather than
changing an image tag, the Grype re-scan is by far the slowest part of the run
and finds nothing new. Skip it with:

```bash
ansible-playbook playbook.yml --ask-vault-pass --skip-tags scan
```

Same flag works on `nextcloud-upgrade.yml`. For an on-demand CVE check without
a full deploy, `ansible-playbook rescan-images.yml` (see
[maintenance.md](maintenance.md)) scans the same images on its own.

---

## 5. Verify

```bash
# Containers should all be running/healthy:
ssh root@nc.example.com 'docker ps'

# Nextcloud should report installed:
ssh root@nc.example.com 'docker exec nextcloud_app php occ status'
```

Then browse to **https://nc.example.com** and log in with
`nextcloud_admin_user` (from `all.yml`) and `vault_nextcloud_admin_password`.

> **Change the admin password once you're in.** `vault_nextcloud_admin_password`
> is a bootstrap credential: Nextcloud's installer reads it once and the
> account then lives in the database, so changing it in `vault.yml` later has
> no effect on the account. Meanwhile the value stays in
> `/run/secrets/nextcloud_admin_password`, readable inside the container, for
> as long as the deployment lives — and an RCE in Nextcloud or any app you
> install lands as exactly the user who can read it. Resetting the password
> from the web UI is what makes that copy worthless. Nextcloud has no
> force-change-on-first-login setting, so this is a step you have to take.

Optional TLS check — you should see an A+ with `X25519MLKEM768` listed
first under key exchange:

```bash
testssl.sh nc.example.com      # or the SSL Labs online scanner
```

---

## What an IP-based probe sees

With `sniStrict` enabled, a client hitting the raw IP (no SNI) gets a
TLS handshake rejection on 443 and only a generic 301 redirect on 80 — no
certificate, no domain name, no app fingerprint is revealed. Access
requires presenting `nc.example.com` as the SNI, i.e. normal name-based access.
