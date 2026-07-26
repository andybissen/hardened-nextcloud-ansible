# Restore

`restore.yml` rebuilds the instance from an encrypted backup onto a
(typically brand-new) VPS — e.g. after the original was destroyed or you're
migrating providers. It sets up the host, restores the database and volumes
*before* Nextcloud can auto-install over them, then brings the full stack up.

Run from this repo's root directory.

```bash
ansible-playbook restore.yml --ask-vault-pass \
  -e restore_backup_file=./backups/nextcloud-backup-<timestamp>.tar.zst.gpg
```

> **This is destructive.** It overwrites the database and Nextcloud data on
> the target host. The playbook pauses for confirmation before doing so
> (press ENTER to proceed, Ctrl+C then A to abort). To skip the prompt in an
> automated run, add `-e restore_confirm=true`.

---

## Prerequisites

On the **control machine** (not the VPS):

- `gnupg2`, `zstd`, and `rsync` installed. (The restore uses `rsync` to push
  the decrypted archives to the VPS; the target's `rsync` is installed
  automatically by the playbook.)
- The GPG **private** key matching `files/backup_public_key.asc` imported
  into your keyring (`gpg --import <private-key-file>`) — decryption happens
  entirely here; the private key never touches the VPS.
- The backup file present locally (under `./backups/`).

> **If the private key has a passphrase, unlock it once before running
> this.** The playbook's decrypt step runs `gpg --batch --yes --decrypt`,
> which suppresses GPG's confirmation prompts (overwrite, trust) but does not
> supply a passphrase — that's handled separately by `gpg-agent`/`pinentry`.
> Since `--batch` disables interactive prompting (and Ansible gives it no tty
> anyway), the step fails outright rather than prompting if the agent doesn't
> already have the key cached. Cache it first by running any `gpg --decrypt`
> against a test file manually, *then* run `restore.yml`. Cache lifetime is
> agent-dependent (commonly ~10 min to ~2 hrs), so do this right before the
> restore, not hours ahead.

Configuration must match the backup:

- **`group_vars/vault.yml` must hold the same secrets used when the backup
  was taken** — same Postgres password, and especially the **same
  `vault_s3_sse_c_key`**. If the SSE-C key doesn't match what encrypted your
  objects in your S3-compatible store, that file data is unreadable
  regardless of restore.
- **`inventory.ini`** pointed at the target VPS.
- **DNS** for `nc.example.com` pointed at the target VPS's IP *before* running
  — Traefik's ACME challenge needs it to resolve so it can (re)issue certs if
  the restored ones are absent or expired.

---

## What it does (ordering matters)

The official Nextcloud image auto-installs a fresh instance on first boot if
it finds no existing `version.php`. To avoid racing that, the playbook:

1. **Fails fast** if `restore_backup_file` is missing, then asks for
   confirmation.
2. **Decrypts the bundle** locally (control machine only) and **verifies it
   by listing** (`tar --zstd -tvf`) — confirms all expected members are
   present and non-trivial without extracting, so a corrupt/incomplete
   backup fails here before anything on the target is touched. The bundle is
   deliberately *not* expanded locally: it's shipped up still-compressed
   (much smaller than the uncompressed tars) and extracted on the VPS.
3. **Prepares the host** — runs the same `tasks/setup_host.yml` as a fresh install
   (Docker, firewalld, DOCKER-USER backstop, journald). Idempotent, so it's
   safe even if this is the original host being repaired.
4. **Renders** the compose file and config onto the host.
5. **Brings up Postgres only** and waits for it to be healthy.
6. **Transfers the compressed bundle** to the VPS (via `rsync`) and
   **extracts it** into staging (DB dump + volume tarballs), then **restores
   the database dump** into Postgres (password read from the mounted secret).
7. **Restores the `nc_data` and `traefik_certs` volumes** via `alpine`
   helper containers.
8. **Deletes the plaintext staging** (dump + tarballs) from the VPS.
9. **Brings up the full stack** — now Nextcloud sees an existing
   `version.php` and takes the upgrade-check path instead of a fresh install.
10. **Waits** for `occ status` to report `installed: true` and prints it.
11. **Cleans up** the local decrypted staging directory.

---

## Optional: skip restoring the Traefik certs

If you'd rather have Traefik issue fresh certificates (e.g. the backed-up
ones are expired, or you're on a new domain), you can delete the
`traefik_certs` volume before the stack fully comes up, or simply let ACME
re-issue. The certs are non-critical — losing them just means a new cert is
requested. DNS must resolve for that to succeed.

---

## Verify

```bash
ssh root@<new-host> 'docker exec -u www-data nextcloud_app php occ status'
```

Confirm `installed: true` and the expected `versionstring`, then browse to
https://nc.example.com and log in. Spot-check that files open (this exercises
the S3 object store + SSE-C key path, if S3 is enabled) and that the admin
overview is clean.

---

## Migrating to a new VPS — quick checklist

1. Provision the new Rocky Linux 9 host; note its IP.
2. Point `nc.example.com` DNS at the new IP; wait for it to propagate.
3. Update `inventory.ini` to the new host; `ssh-keyscan -H nc.example.com >>
   ~/.ssh/known_hosts`.
4. Ensure `group_vars/vault.yml` matches the original secrets (decrypt/check
   if unsure).
5. Run the `restore.yml` command above with your chosen backup file.
6. Verify as above, then decommission the old host.
