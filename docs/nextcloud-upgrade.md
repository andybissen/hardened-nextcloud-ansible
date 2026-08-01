# Upgrades

Both minor/patch and major Nextcloud upgrades use the same playbook,
`nextcloud-upgrade.yml`. It **always takes a full backup first** (it imports
`backup.yml`), scans the target image with Grype, recreates only the
`nextcloud` and `cron` containers with the new tag, and then polls
`occ status` until Nextcloud's automatic upgrade routine reports done.

Run everything from this repo's root directory.

> **Check first — you may not need to run any of this.** `./check-updates.py`
> (see [maintenance.md](maintenance.md)) tells you whether a newer patch or
> major is even available before you spend time on a backup + Grype scan +
> upgrade cycle for a version you're already on.

> **The one rule that matters:** Nextcloud refuses to skip major versions.
> Upgrade **one major at a time** (33 → 34 → 35), never 33 → 35 directly.
>
> This is **enforced** by a pre-flight guard that runs *before* the
> backup: it reads the deployed version, compares it to your target tag, and
> refuses a jump of more than one major (or any downgrade) with a clear
> message — so a mistyped target can't quietly start an unrecoverable
> upgrade. The same pre-flight also confirms the target tag actually exists
> in the registry. In the rare case you genuinely need to override the
> version check, add `-e skip_version_guard=true` (this bypasses only the
> major-jump check, not the tag-exists check).

---

## Background: how the image tag works

`nextcloud_image` in `group_vars/all.yml` is a *major-version* tag, e.g.
`nextcloud:33-apache`. Upstream repoints that tag in place as it publishes
patch releases (33.0.1, 33.0.2, …). So:

- **Minor/patch upgrade** = re-pull the *same* major tag to pick up the
  latest patch within that major.
- **Major upgrade** = move to the *next* major tag (`34-apache`).

---

## Minor / patch upgrade (e.g. 33.0.1 → 33.0.5)

Because the tag is unchanged, just re-pull the current major tag and let the
playbook recreate the containers:

```bash
ansible-playbook nextcloud-upgrade.yml --ask-vault-pass \
  -e nextcloud_target_tag=33-apache
```

(Use whatever major you're currently on. Since `all.yml` already names that
same tag, there's nothing to update afterward.)

The playbook does `pull: always`, so it fetches the freshest `33-apache`
even though the tag string didn't change.

---

## Major upgrade (e.g. 33 → 34)

```bash
ansible-playbook nextcloud-upgrade.yml --ask-vault-pass \
  -e nextcloud_target_tag=34-apache
```

Then **persist the new tag** so your source of truth stays consistent:

> ### ⚠️ Update `all.yml` after a successful major upgrade
>
> `nextcloud-upgrade.yml` renders the running host's `docker-compose.yml` with the
> tag you passed, but it does **not** edit `group_vars/all.yml`. If you
> leave `all.yml` on the old major and later run `playbook.yml` (a normal
> deploy/re-render), it will re-render with the **old** tag and try to
> **downgrade** Nextcloud — which Nextcloud refuses, leaving the app
> unable to start.
>
> After confirming the upgrade succeeded, edit `group_vars/all.yml`:
>
> ```yaml
> nextcloud_image: "nextcloud:34-apache"    # was 33-apache
> ```

If you're several majors behind, repeat the whole procedure once per major:

```bash
ansible-playbook nextcloud-upgrade.yml --ask-vault-pass -e nextcloud_target_tag=34-apache
# update all.yml → 34-apache, verify, then:
ansible-playbook nextcloud-upgrade.yml --ask-vault-pass -e nextcloud_target_tag=35-apache
# update all.yml → 35-apache, verify, etc.
```

---

## What the playbook does, step by step

1. **Pre-flight** (before the backup) — confirms a target tag was given and
   that it exists in the registry, reads the deployed version, and refuses a
   skip-a-major jump or a downgrade. Fails here without taking a backup if
   the invocation is wrong.
2. **Backup** — imports `backup.yml` in full (DB dump + `nc_data` +
   Traefik certs, encrypted and fetched locally). If the backup fails, the
   upgrade stops before touching anything.
3. **Scan** the target image with Grype (informational).
4. **Render** `docker-compose.yml` on the host with the new tag.
5. **Pull & recreate** only `nextcloud` and `cron` (Postgres, Valkey,
   Traefik, socket-proxy are left running).
6. **Wait** — polls `php occ status --output=json` up to 60× at 15s
   intervals until `needsDbUpgrade` reports `false`, then prints the status.
   (`installed: true` isn't useful here — Nextcloud reports itself
   "installed" throughout an upgrade, so this waits for the DB migration to
   finish, not just for the container to come back up.)
7. **Repair** — runs `occ maintenance:repair --include-expensive` (mimetype
   migrations, etc.) and `occ db:add-missing-indices`. Nextcloud
   deliberately skips both during `occ upgrade` itself since they can be
   slow on large instances; both are idempotent, so running them every time
   is harmless even when there's nothing to do.
8. **Flush background jobs** — runs `php cron.php` three times. Some
   post-major-upgrade migrations are scheduled as background jobs rather
   than run synchronously, and Nextcloud's own docs warn these need to run
   (via a few cron passes) before starting another major upgrade. Without
   this, the `cron` sidecar would eventually pick them up on its normal
   5-minute cycle, but the playbook would report done before that happened.
9. **Clean up the old image** — two steps, covering two cases. A **major**
   bump leaves the old tag as a distinct named image (e.g. `33-apache` vs
   `34-apache`) that never becomes "dangling" just because the compose file
   stopped referencing it, so it's removed by name. A same-tag **patch**
   refresh does leave the old image genuinely dangling once the tag moves to
   the freshly-pulled one, which `docker image prune -f` catches. Both steps
   run every time; whichever doesn't apply is a no-op.

---

## Verify

```bash
ssh root@nc.example.com 'docker exec nextcloud_app php occ status'
```

Confirm the reported `versionstring` matches the major you targeted, then
browse to https://nc.example.com and check the admin overview page for
warnings.

---

## If an upgrade goes wrong

The pre-upgrade backup is already on your control machine under
`./backups/` (newest `nextcloud-backup-*.tar.zst.gpg`). Follow
[restore.md](restore.md) to roll back to that snapshot.

Common gotchas:

- **Skipped a major version** — the only supported fix is to restore the
  backup and redo the upgrade one major at a time.
- **App incompatibility after a major bump** — some third-party apps lag
  new majors. Disable the offending app from the admin panel (or
  `occ app:disable <app>`) and re-enable once it's compatible.
