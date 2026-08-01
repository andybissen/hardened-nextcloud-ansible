# Hardened Nextcloud — Single-VPS Ansible Deployment

Reproducible, security-hardened deployment of Nextcloud on one Rocky Linux 9/10
host, managed entirely with Ansible. The stack is Traefik (TLS termination +
Let's Encrypt) → Nextcloud (Apache) with a cron sidecar, backed by Postgres
and Valkey, with optional S3-compatible primary object storage. Traefik
reaches the Docker API only through a locked-down read-only socket proxy.

Highlights:

- **Hardened TLS** — TLS 1.3 with post-quantum hybrid key exchange
  (`X25519MLKEM768`), a minimal modern TLS 1.2 fallback, ECDSA P-384 certs,
  HSTS preload, and `sniStrict` (raw-IP probes get nothing).
- **Secrets kept out of the config** — file-based Docker secrets + Ansible
  Vault; no passwords in the compose file, container environments, or
  `docker inspect`.
- **Defense in depth on the database** — internal-only Docker network, a
  DOCKER-USER iptables backstop, and scram-only `pg_hba`.
- **Encrypted, tested backup/restore** — streamed `pg_dump` + volume archives,
  GPG public-key encrypted (the VPS can't decrypt its own backups), with a
  restore playbook and per-service upgrade procedures.
- **S3-compatible primary storage** — files on an S3-compatible object store, not
  just local disk. Optional: leave it unconfigured and Nextcloud falls back to
  local storage, no other changes needed (see `all.yml.example`).
- **A minimal-privilege Docker API surface** — Traefik only ever talks to a
  read-only, whitelisted socket proxy; nothing here has broad Docker socket
  access.
- **Confined container runtimes** — `no-new-privileges` on every service,
  capabilities dropped everywhere (all of them on Traefik and the socket
  proxy, which run as root), on top of Docker's SELinux confinement.

## Why does this exist?

I had trouble finding a Nextcloud setup that was genuinely hardened *and*
reproducible — most of what's out there trades one for the other.

The goal of this project is to use best practices (maybe going a step or two
further) for Docker, network segmentation, and encryption, with an added
focus on working backups and a tested method for restoring or migrating data.

The trade-off is deliberate. The fastest one-command installers get you
running in minutes by putting the whole stack behind a single control surface.
This does the opposite: you bring a VPS, DNS, and a GPG key and drive it with
Ansible, and in return every service, secret, firewall rule, and backup step
is something you can read, diff, audit, and reproduce. If a five-minute install
matters most, this isn't it. If understanding and owning exactly what runs, and
how your data is protected, matters more — that's what this is for.

There are no plans to support more than RHEL-based distros at this time.

## Documentation

Start here → **[docs/README.md](docs/README.md)** (index), then:

| Task | Doc |
| --- | --- |
| First-time deployment | [docs/install.md](docs/install.md) |
| Check whether anything needs upgrading (`./check-updates.py`) | [docs/maintenance.md](docs/maintenance.md) |
| Nextcloud upgrades | [docs/nextcloud-upgrade.md](docs/nextcloud-upgrade.md) |
| Traefik / Postgres / Valkey upgrades & version pinning | [docs/maintenance.md](docs/maintenance.md) |
| Backups | [docs/backup.md](docs/backup.md) |
| Restore / migrate to a new host | [docs/restore.md](docs/restore.md) |

## Configuration & the secrets model

**No real per-site values live in version control.** The repo ships
`*.example` templates; you copy each to its real (gitignored) name and fill it
in:

```bash
cp group_vars/all.yml.example    group_vars/all.yml     # domain, email, S3 bucket, tuning
cp group_vars/vault.yml.example  group_vars/vault.yml   # secrets (then: ansible-vault encrypt)
cp inventory.ini.example         inventory.ini          # your host + SSH details
```

Gitignored so you can't publish them by accident: `group_vars/all.yml`,
`group_vars/vault.yml`, `inventory.ini`, `files/backup_public_key.asc`, and
`backups/`. See [.gitignore](.gitignore). Your backup-encryption public key is
generated per-user — see
[files/backup_public_key.asc.README](files/backup_public_key.asc.README).

> The encrypted `vault.yml` must never be committed either — not even in its
> ansible-vault form — since on a public repo that becomes a permanent,
> offline-crackable artifact guarded only by your vault passphrase.

## Known limitations (read before assuming "encrypted" means "fully protected")

This repo makes deliberate hardening choices throughout, but it is not a
zero-knowledge or end-to-end-encrypted system. Two structural facts underlie
most of what follows: **server-side encryption never covers metadata**, and on
a single VPS **the keys live with the data they protect**. Worth understanding
before relying on any of it:

- **S3 object metadata isn't encrypted.** SSE-C (see `vault_s3_sse_c_key`)
  protects file *content*, not file *paths*: Nextcloud's objectstore code
  (`ObjectStoreStorage::writeStream()`) writes each file's real path and
  owning-user storage ID as plaintext object metadata on every write. This is
  inherent to server-side encryption, not provider-specific — AWS's and
  Backblaze's docs both confirm SSE protects object data only, and Nextcloud's
  metadata-writing code has no provider branching, so switching providers
  wouldn't change it. Anyone with read access to the bucket (a leaked
  application key, or the provider itself) can see every file's folder
  structure and filename, even without being able to open the file.

- **SSE-C protects against the storage provider, not against this server.**
  The SSE-C key lives in `config.php` on the VPS. Its actual guarantee: your
  files stay unreadable to the S3 provider if it's breached, subpoenaed, or
  malicious, or if a bucket-only credential leaks on its own. It does **not**
  protect against the Nextcloud host being rooted — arguably the likelier
  attack path for a single-VPS deployment — which hands over the key along
  with everything else it protects.

- **The database holds the full plaintext file tree, unencrypted at rest.**
  All metadata (filenames, folder structure, sharing info) lives in Postgres,
  not the object store — standard Nextcloud behavior, confirmed against
  `oc_filecache` here. Reading the `pg_data` volume off disk exposes the
  complete file tree even setting aside the S3 metadata point above. (Backups
  are GPG-encrypted; the *live* Postgres data on disk is not.)

- **No end-to-end encryption.** Your deployment, its admin, and Nextcloud
  itself can always read file content in the clear. SSE-C is a storage-layer
  protection, not a guarantee the operator can't see your data (unlike
  Nextcloud's own E2EE app, unused here). Expected and fine for most
  self-hosters, but worth stating. Nextcloud's own [encryption
  documentation](https://docs.nextcloud.com/server/latest/admin_manual/configuration_files/encryption_configuration.html)
  is the authority on where each mode's protection stops — including that
  server-side encryption "does not protect data from a compromised Nextcloud
  server or malicious administrator", and that E2EE is what to reach for if
  that is your threat model.

- **Local storage gets no built-in encryption, deliberately.** If you use
  local storage instead of S3, this repo does not enable Nextcloud's
  server-side encryption app. Its master-key mode stores the key on the same
  disk as the files — the same "key travels with the ciphertext" problem, with
  no separate system involved, so it adds no real protection against the threat
  that matters (someone getting the disk) while adding real risk: losing the
  key means permanent, total data loss. Not worth the trade-off.

The at-rest gaps (the Postgres volume, and `nc_data` on local storage) can be
closed one layer down, by encrypting the underlying disk — host-level FDE (e.g.
LUKS unlocked at boot) or a provider volume-encryption option. That's below
Ansible, at the provisioning layer, so it's out of scope here. One caveat: most
provider-managed encryption has the provider hold the key and decrypt
transparently for your running VM, which guards against a powered-off disk
being read (decommissioned or stolen media) but not against the provider or a
compromised running host.

None of the above are misconfigurations, and none are things this repo's
Ansible config can suppress — they're structural properties of Nextcloud and
the S3 encryption model. If any matter for your threat model, factor them in
before treating this as "fully encrypted."

## Extending

Two low-friction extension points, both optional:

- **Outgoing email (SMTP)** — off by default; uncomment the SMTP block in
  `group_vars/all.yml` (see `all.yml.example`) and add `vault_smtp_password`
  to `vault.yml`. Without it, Nextcloud can't send password resets or share
  invitations.
- **Entrypoint hooks** — the Nextcloud image runs any executable `*.sh`
  scripts mounted into `/docker-entrypoint-hooks.d/<hook>/`
  (`pre/post-installation`, `pre/post-upgrade`, `before-starting`), so you
  can run custom setup at those points by adding a bind mount to the
  `nextcloud` service in `templates/docker-compose.yml.j2` — no playbook
  changes needed. See the
  [image documentation](https://github.com/nextcloud/docker#auto-configuration-via-hook-folders)
  for details.

## Requirements

- A control machine with `ansible-core`, the `gpg` CLI, and (for restores)
  `gnupg2` + `zstd`.
- A fresh Rocky Linux (or compatible RHEL 9/10) VPS reachable over SSH as
  root, plus DNS for your hostname pointing at it before first deploy.
- Collections: `ansible-galaxy collection install -r requirements.yml`.

## Status

Provided as-is. Review it against your own threat model before running it in production.

This project was created with assistance from Claude (Sonnet, Opus & Fable), Gemma 4 (12b), and GPT 5.6, 
and has been manually reviewed and tested.

## License

[MIT](LICENSE)
