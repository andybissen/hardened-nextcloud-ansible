# Backups

`backup.yml` produces a single encrypted archive containing everything
needed to rebuild the instance: the Postgres database, the Nextcloud data
volume (`nc_data`, which holds `config.php` and any local files), and the
Traefik certificate volume. It is also run automatically before every
upgrade.

Nextcloud is put into **maintenance mode** (read-only) for the few seconds it
takes to dump the database and archive `nc_data`, then taken back out
automatically - even if a step in between fails. This is what makes the DB
dump and the file archive one consistent snapshot instead of two taken
moments apart; without it, a file uploaded in that gap could land in one
snapshot and not the other. The Traefik-certs archive isn't part of this
window since it has no consistency dependency on Nextcloud's data.

Run from this repo's root directory.

```bash
ansible-playbook backup.yml --ask-vault-pass
```

---

## Prerequisites (one-time)

These are the same as the backup-key setup in [install.md](install.md):

1. Export your GPG **public** key to the repo:
   ```bash
   gpg --export --armor <your-key-id-or-email> > files/backup_public_key.asc
   ```
2. Set `gpg_recipient_key_id` in `group_vars/all.yml` to that same
   id/email/fingerprint.

The VPS only ever holds your **public** key, so a compromised VPS cannot
decrypt its own backups. Decryption requires the matching **private** key,
which lives only on your control machine / in your keyring.

Tooling: the control machine needs `rsync` (used to pull the encrypted
backup down); `gnupg2`, `zstd`, and `rsync` on the VPS are installed
automatically by the playbook.

---

## What it does

1. Ensures `gnupg2` and `zstd` are installed on the VPS.
2. Dumps Postgres straight to disk on the VPS (`pg_dump` streamed via the
   shell — never buffered through Ansible or SSH), reading the DB password
   from the container's mounted secret so it never appears on a command line.
3. Archives the `nc_data` and `traefik_certs` volumes into tarballs using
   throwaway `alpine` helper containers.
4. **Verifies the staged material** before anything is bundled or
   encrypted: each of the three files must exist and be over 1KB, both tar
   archives must pass `tar -tf` (catches truncation/corruption), and
   `nc_data.tar` must be at least half the size of the live `nc_data`
   volume as measured at the start of the run (catches a volume that
   mounted but came up empty or degraded, which `tar` would otherwise
   archive successfully into a small-but-valid file). Fails the play and
   leaves the staging directory in place for inspection if anything looks
   wrong — this check has to run on the plaintext staged files, before
   encryption, since decryption only ever happens on the control machine
   (see [restore.md](restore.md)).
5. Bundles all of that into one `zstd`-compressed tarball, then validates
   the bundle with `tar --zstd -tf` (decompresses the whole stream and
   parses the tar structure inside it). Once the bundle is confirmed
   intact, the staging directory is removed *before* encryption — the
   bundle is a complete plaintext superset of it, so keeping staging around
   would only inflate peak disk use by one final-backup-size (which matters
   most for local-storage deployments where `nc_data.tar` is many GB).
6. GPG-encrypts the bundle to your recipient key →
   `nextcloud-backup-<timestamp>.tar.zst.gpg`.
7. **Verifies the encrypted output was actually encrypted to the expected
   key** — compares the key ID `gpg --list-packets` reports the file was
   encrypted to against *all* of the recipient's fingerprints (primary key
   plus every subkey) from the local keyring, since GPG always encrypts to
   a dedicated encryption subkey rather than the primary key itself.
   Catches a misconfigured/stale `gpg_recipient_key_id` or a wrong key in
   the keyring right away, rather than discovering it only when a restore
   fails to decrypt. Fails the play and leaves both the encrypted output and
   the unencrypted bundle in place for inspection if it doesn't match — the
   bundle is a full plaintext copy, so a failed encryption never leaves you
   without a restorable artifact.
8. Deletes the unencrypted bundle from the VPS (the staging directory was
   already removed at step 5).
9. If `fetch_backups_locally: true` (the default), pulls the encrypted
   archive down (via `rsync`) to `./backups/` on your control machine.
10. Prunes old archives beyond `backup_retention_count` (default 8) on **both**
    the VPS and the control machine.

---

## What is NOT in the backup

- **`group_vars/vault.yml`** — deliberately excluded. The entire bundle is
  assembled and encrypted on the VPS from files on the VPS, and `vault.yml`
  only exists on your control machine. Keep its secrets in a password
  manager; you will need them to restore (see [restore.md](restore.md)).
- **Valkey data** — it's a cache / lock store with nothing durable; there's
  nothing to back up.
- **The bulk of your files**, if they live in **S3** object
  storage rather than locally. The backup captures the *database and
  config* that reference those objects, but the object data itself is in
  S3. Ensure S3 has its own lifecycle/versioning protection. (Note: the
  SSE-C key needed to read those objects lives in `vault.yml` — guard it.)

> **Running local storage instead of S3?** (see the optional S3 section in
> `all.yml.example`) This changes what the backup protects, not just its size.
> With S3, the backup is "the recipe to rebuild everything except your files" —
> the files stay durable in S3 regardless of when backups run. With local
> storage, `nc_data` holds your file content, so `nc_data.tar` becomes the
> **only** copy outside the live disk. Consequences: backup frequency directly
> bounds your data-loss window (anything uploaded since the last backup is
> unprotected), losing the GPG private key loses your only copy of the files
> too, and a `restore.yml` run becomes full data restoration — a corrupted or
> missing backup is total loss, not an inconvenience. Backups also take longer
> and need more staging disk, since a volume with real content takes real time
> to tar/compress/encrypt.

---

## Off-site copies (3-2-1)

With `fetch_backups_locally: true` (the default), each encrypted archive
already lands on your control machine as well as the VPS — two separate
failure domains, which covers "the VPS is gone." For a true third location
(the *3* in a 3-2-1 strategy), push the archives somewhere off both machines.

This is deliberately left to the admin rather than built into the playbook.
The `.tar.zst.gpg` is a self-contained, already-encrypted artifact, so getting
it to remote storage is a generic, well-solved problem — better handled by a
purpose-built tool than reimplemented here for one provider. And because it's
encrypted to your key (the VPS can't decrypt it, and neither can the remote),
it's safe on untrusted storage: an S3 bucket, `rsync.net`, or any cold
storage will do.

A one-liner against your local `./backups/` directory covers it — e.g. with
[`rclone`](https://rclone.org/):

```bash
rclone copy ./backups remote:my-nextcloud-backups
```

Run it after the backup (a cron job, or a systemd `ExecStartPost=` / second
timer alongside the scheduling setup below), and manage retention on the
remote side independently — an S3 lifecycle rule, or `rclone`'s own
`--max-age` / prune options. `backup_retention_count` only governs the VPS and
control-machine copies, not whatever you sync off-site.

---

## Configuration knobs (`group_vars/all.yml`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `remote_backup_dir` | `/opt/docker-conf/backups` | Where archives are written on the VPS |
| `local_backup_dir` | `./backups` | Where they're fetched to locally |
| `fetch_backups_locally` | `true` | Pull each archive down to the control machine |
| `backup_retention_count` | `8` | How many archives to keep (older ones pruned) |

> **These paths are not namespaced per deployment.** If you point a second
> instance at the same `remote_backup_dir`/`local_backup_dir`, both write
> into one shared directory and `backup_retention_count` prunes across the
> combined set — so a busy instance can age out the other's archives. An
> archive's filename and location say nothing about which instance produced
> it, so don't infer that when picking one to restore. Give each deployment
> its own paths if you run more than one.

---

## Verify a backup

```bash
ls -lh ./backups/                       # newest .tar.zst.gpg present?

# Confirm it decrypts (needs your private key), without unpacking:
gpg --decrypt ./backups/nextcloud-backup-<timestamp>.tar.zst.gpg \
  | zstd -dc | tar -tvf - | head
```

If that lists `nextcloud-db.sql`, `nc_data.tar`, and `traefik_certs.tar`,
the archive is intact and restorable.

---

## Scheduling

To run backups automatically from your control machine, `--ask-vault-pass`
won't work unattended (it blocks on a prompt) — both options below use a
vault password file instead:

```bash
# store the vault password readable only by you
echo 'your-vault-pass' > ~/.nc-vault-pass && chmod 600 ~/.nc-vault-pass
```

**Pick a time that clears Nextcloud's maintenance window.** The backup uses
maintenance mode to get a consistent `pg_dump`/`nc_data.tar` pair, but a
background job already mid-run keeps writing regardless — overlapping the
window undercuts exactly the consistency that snapshot exists for. The window
starts at `nextcloud_maintenance_window_start` (see `group_vars/all.yml.example`)
and spans five hours, not the four upstream's docs claim. It's also in UTC and
ignores DST, while the schedules below are local time and follow it, so leave
margin in both halves of the year.

### systemd timer (recommended) — runs as your own user, no root/sudo

**The working directory matters, not just cosmetically.** `ansible.cfg`
(which sets `inventory = inventory.ini`) is only discovered relative to the
*current working directory* the command is run from — not relative to the
playbook's own path. Get this wrong and the play doesn't error, it just
silently reports "no hosts matched" and does nothing, which is a bad way to
discover your weekly backups haven't been running. Setting
`WorkingDirectory` in the service unit is what avoids that.

1. Create `~/.config/systemd/user/nextcloud-backup.service` (substitute your
   actual path to this repo's root directory):

   ```ini
   [Unit]
   Description=Nextcloud stack backup

   [Service]
   Type=oneshot
   WorkingDirectory=/path/to/repo
   ExecStartPre=/usr/bin/sleep 300
   ExecStart=/usr/bin/ansible-playbook backup.yml --vault-password-file %h/.nc-vault-pass
   ```

2. Create `~/.config/systemd/user/nextcloud-backup.timer`:

   ```ini
   [Unit]
   Description=Weekly Nextcloud backup

   [Timer]
   OnCalendar=Sun *-*-* 03:30:00
   Persistent=true
   RandomizedDelaySec=10min

   [Install]
   WantedBy=timers.target
   ```

   `Persistent=true` means if your machine was off or asleep at the
   scheduled time, the timer fires as soon as it's back instead of silently
   skipping that week — worth having on a laptop/desktop that isn't always on.

   Together, `ExecStartPre=sleep 300` (a flat 5-minute floor, in the service
   unit) and `RandomizedDelaySec=10min` (0-10 minutes more, in the timer) give
   a catch-up run a 5-15 minute wait instead of firing the instant the machine
   boots or wakes — useful if it reboots again shortly after. Per
   `systemd.timer(5)`, `RandomizedDelaySec` applies to catch-up triggers too,
   not just on-schedule ones; the fixed floor is a plain `sleep` because
   systemd has no single floor+ceiling directive. Both apply to the normal
   Sunday 03:30 run as well — a harmless 5-15 minute shift.

3. Enable it:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now nextcloud-backup.timer
   ```

4. **Enable lingering, or this silently stops working when you log out.**
   By default, `systemctl --user` units only run while you have an active
   login session — a weekly timer will very likely try to fire while you're
   logged out. Lingering keeps your user's systemd instance (and its timers)
   running regardless of login state, as long as the machine is powered on:

   ```bash
   loginctl enable-linger "$USER"
   ```

5. **If your SSH key has a passphrase, point the service at your agent.** A
   user service doesn't inherit `SSH_AUTH_SOCK` from your login session, so
   ssh finds no agent and the run fails with `Permission denied
   (publickey)` — which reads like a key problem on the server rather than a
   missing agent. Add your agent's socket (`echo $SSH_AUTH_SOCK` in a normal
   shell) to the `[Service]` section:

   ```ini
   Environment=SSH_AUTH_SOCK=/path/to/your/agent/socket
   ```

   This only works if the agent is running with the key unlocked when the
   timer fires; a passphrase-less key used only for backups avoids the
   question entirely.

6. Verify:

   ```bash
   systemctl --user list-timers --all              # confirm next scheduled run
   systemctl --user status nextcloud-backup.timer
   journalctl --user -u nextcloud-backup.service    # backup run logs
   ```

### cron (alternative)

cron doesn't have the login-session restriction systemd user units do —
system cron runs regardless of whether you're logged in, so there's no
lingering-equivalent step needed. The `cd /path/to/repo &&` here isn't
just tidiness — it's the same fix as `WorkingDirectory` above, since cron
has no separate setting for a job's working directory:

```bash
# crontab -e — weekly, Sunday 03:30
SSH_AUTH_SOCK=/path/to/your/agent/socket
30 3 * * 0 cd /path/to/repo && ansible-playbook backup.yml \
  --vault-password-file ~/.nc-vault-pass >> ~/nc-backup.log 2>&1
```

The `SSH_AUTH_SOCK=` line is the same agent caveat as step 5 above — cron's
environment doesn't have it either. Drop the line if your key has no
passphrase, and note cron doesn't expand `~` in these assignments, so it
needs an absolute path.
