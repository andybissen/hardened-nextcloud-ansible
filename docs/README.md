# Nextcloud on Docker — Operations Docs

Ansible-managed Nextcloud stack (Traefik + Postgres + Valkey + Nextcloud +
cron) on a single Rocky Linux 9 VPS, with optional S3-compatible primary
object storage and encrypted off-host backups.

All commands are run from this repo's root directory on your control machine.

| Task | Doc | Playbook |
| --- | --- | --- |
| First-time deployment | [install.md](install.md) | `playbook.yml` |
| Check whether anything needs upgrading | [maintenance.md](maintenance.md) | `check-updates.py` |
| Nextcloud upgrades (minor & major) | [nextcloud-upgrade.md](nextcloud-upgrade.md) | `nextcloud-upgrade.yml` |
| Traefik / Postgres / Valkey upgrades & version pinning | [maintenance.md](maintenance.md) | `playbook.yml`, `pg-major-upgrade.yml` |
| Taking a backup | [backup.md](backup.md) | `backup.yml` |
| Restoring onto a (new) host | [restore.md](restore.md) | `restore.yml` |

Every playbook is encrypted-vault–aware and must be run with
`--ask-vault-pass`.

## The stack at a glance

- **Traefik** — TLS termination, automatic Let's Encrypt certs (ECDSA
  P-384), HTTP→HTTPS redirect, modern TLS policy (TLS 1.3 with
  post-quantum key exchange, restricted TLS 1.2 fallback).
- **socket-proxy** — read-only broker in front of the Docker socket so
  Traefik never touches `docker.sock` directly.
- **Postgres** — the database. Data in a named volume; not reachable off-host.
- **Valkey** — Redis-protocol cache + transactional file-locking backend.
- **Nextcloud (apache)** + **cron** sidecar — the app and its background jobs.

Files can optionally be stored in an **S3-compatible object store** (e.g.
Backblaze B2) via the image's S3 object-store support, with SSE-C
client-side encryption — or left on local storage under `nc_data` by
default.
