# Maintenance: image versions & non-Nextcloud upgrades

Nextcloud upgrades have their own doc ([nextcloud-upgrade.md](nextcloud-upgrade.md)). This one
covers the other pinned images — **Traefik, Postgres, and Valkey** — and the
version-pinning strategy behind them.

Run everything from this repo's root directory.

---

## Checking for updates

`check-updates.py` is a `dnf check-update`-style report for all five pinned
images (Traefik, socket-proxy, Postgres, Nextcloud, Valkey). Run it anytime:

```bash
./check-updates.py
```

By default it reads the tags pinned in `group_vars/all.yml` and queries
Docker Hub and [endoflife.date](https://endoflife.date/) - nothing touches
the VPS, so it's always safe to run. For each image it reports:

- **Latest patch under your current pin** - informational by itself. Since
  every pin here is a floating alias (see the pinning table below), a
  force-pull already fetches this automatically; there's nothing to edit
  for it. Whether you're actually *behind* it is a separate question - see
  `--live` below.
- **⚠ A newer line beyond your pin**, if one exists (a new Traefik minor, or
  a new Postgres/Nextcloud/Valkey major) - this is the actionable one: it
  means a deliberate tag change in `all.yml` plus the relevant upgrade doc,
  not something to apply automatically.
- **EOL status** for the cycle you're currently on, with a warning if it's
  within 90 days of end-of-life or already past it.

### `--live`: is the deployed version actually behind?

```bash
./check-updates.py --live
```

Everything above only asks Docker Hub "what's the newest tag" — it can't tell
whether that tag is already running on the VPS. Because the pins are floating
aliases, Docker never re-pulls on its own (detailed under "How pulls actually
happen" below), so a new patch under your pin (pinned `9-alpine`, registry now
has `9.1.5`, container still on `9.1.0`) goes unnoticed unless you force-pull
or use this flag.

`--live` runs `check-deployed-versions.yml` over SSH (via `inventory.ini`) to
ask each container its actual version, then compares against the
latest-under-pin from Docker Hub. It's the only part of the script that
touches the VPS — which is why it's opt-in, keeping plain `./check-updates.py`
a zero-touch check you can run from anywhere. A missing container or
unreachable host reports "could not determine" for that image rather than
failing the run.

You can also run `ansible-playbook check-deployed-versions.yml` directly for a
"what's actually running" report without the Docker Hub/EOL comparison.

Exit code is `0` if nothing needs attention, `1` if a newer line is available,
an EOL warning fired, and/or (with `--live`) the deployed version is behind —
easy to wire into cron or a monitor if you'd rather be notified than remember
to run it.

### Checking for vulnerabilities

```bash
ansible-playbook rescan-images.yml
```

`check-updates.py` only answers "is there a newer version" — a different
question from "has a CVE been disclosed against what I'm already running." A
vulnerability can land against a version you haven't touched, with no newer
tag to switch to, so version-checking alone won't catch it. `rescan-images.yml`
re-runs the same Grype scan that `playbook.yml` and `nextcloud-upgrade.yml` run
before any deploy/upgrade, but on demand against whatever's currently pinned.

It's a separate playbook rather than part of `check-updates.py` because Grype
needs Docker to pull and scan each image's manifest, taking real
seconds-to-tens-of-seconds per image — folding it in would turn a fast,
dependency-free script into a slow one. Informational only: findings are
printed, nothing fails or changes.

---

## How pulls actually happen (read this first)

`playbook.yml` brings the stack up with `pull: policy`, which only fetches an
image that is **missing locally**. Consequences:

- A tag that floats within a line (e.g. `traefik:v3.7` receiving 3.7.x
  patches) does **not** auto-update a running host on a normal redeploy — the
  tag is already present, so nothing is re-pulled.
- New content behind an unchanged tag only lands when you **force a pull**.
- Changing the tag **string** (e.g. `v3.7` → `v3.8`) *does* pull, because the
  new tag isn't present locally.

So there are two distinct operations:

| You want… | Do this |
| --- | --- |
| The latest content **within** the current pin (patches, and minors for a major-pinned tag) | **Force-pull** the current tag (below) |
| To **cross** the pin boundary (a new minor/major tag string) | Edit the tag in `group_vars/all.yml`, then re-run `playbook.yml` |

socket-proxy is pinned to an exact release, so only the second row ever
applies to it — a force-pull has nothing newer to fetch behind that tag.

### Force-pull recipe

```bash
# Refresh one service to the newest image behind its current tag:
ssh root@nc.example.com \
  'cd /opt/docker-conf && docker compose pull valkey && docker compose up -d valkey && docker image prune -f'
```

Since the tag string is unchanged, there's nothing to update in the repo —
no drift. (`/opt/docker-conf` is `compose_dir` from `all.yml`.)

`docker image prune -f` clears the now-dangling previous image left behind
by the pull, so repeated patching doesn't quietly eat disk over time.

---

## Pinning strategy

| Service | Tag | Pin level | Rationale |
| --- | --- | --- | --- |
| **Traefik** | `traefik:v3.7` | minor | Terminates TLS; its *minors* add features and occasionally shift defaults/config, so restores should be reproducible to the minor and minor jumps should be a deliberate choice |
| **Postgres** | `postgres:18-trixie` | major | Within-major is bugfix-only and safe to float; crossing a major needs a real migration, which is gated by `pg-major-upgrade.yml` |
| **Valkey** | `valkey:9-alpine` | major | Stateless cache/lock store — blast radius of any surprise is a cache flush, so floating within a major costs nothing |
| **socket-proxy** | `tecnativa/docker-socket-proxy:v0.5.0` | patch | The only container with any access to the Docker socket, so tag drift here has the highest blast radius of anything in the stack; upstream cuts releases roughly twice a year, making an exact pin cheap to carry |

Why pin at all when some upgrades are trivial? Pinning isn't about upgrade
*difficulty* — it's about (1) **deterministic restores** (a recovery brings
up a known line, not "whatever's newest at restore time") and (2) keeping a
**human gate at the semver boundary** where breaking changes are permitted.
That's why even the trivial-to-upgrade Valkey stays pinned to a major rather
than running `latest`.

---

## Upgrading multiple services in one sitting

Traefik, Postgres, and Valkey don't depend on each other in
`docker-compose.yml.j2` (only Nextcloud/cron depend on them), so nothing
forces you to upgrade them one at a time. The reason to sequence them is
**diagnosability**: bump all three at once and, if something breaks, you can't
tell which change did it without rolling each back to isolate it.

Calibrate by risk tier:

- **Valkey + Traefik patch/minor, together, is fine** — both are low-risk
  (Valkey has no durable state; Traefik minors don't touch data), so batching
  them to cut down on maintenance windows is a reasonable trade.
- **Anything major, or anything alongside a Postgres major, gets its own
  window** — verify it's healthy before starting the next one.

One case is a *concrete* technical reason, not just hygiene: the last step of
`pg-major-upgrade.yml` is "bring the full stack back up" with `pull: policy`.
If `traefik_image` or `valkey_image` in `all.yml` were also changed before you
ran it, that final step pulls and recreates those services too - bundling an
unrelated image bump into the middle of a database migration, right when you
most want a narrow, single-variable change. **Don't stage other image tag
changes in `all.yml` before a Postgres major upgrade** - land and verify it on
its own first.

---

## Per-service upgrade guidance

### Valkey

No durable state (no persistence volume; `allkeys-lru` eviction). Any
upgrade just discards the in-memory cache — a harmless cache miss / lock
re-acquire.

- **Patch or minor** (within `9-alpine`): force-pull the tag (recipe above).
- **Major** (9 → 10): change `valkey_image` to `valkey:10-alpine` in
  `all.yml`, run `playbook.yml`. Skim the release notes for any
  `requirepass`/ACL/config-directive changes, but no data steps are ever
  needed.

### Traefik

No data migration ever — the only persistent state is `acme.json` (issued
certs + ACME account key), which any 3.x reads.

- **Patch** (within `v3.7`): force-pull the tag.
- **Minor** (v3.7 → v3.8): change `traefik_image` in `all.yml`, run
  `playbook.yml`. Glance at the release notes.
- **Major** (v3 → v4): **read the migration guide first.** Traefik majors can
  change the static CLI flags in `docker-compose.yml.j2` and/or the dynamic
  config in `templates/traefik-dynamic.yml` (the v2→v3 jump did). Update those
  templates as the guide dictates, then run `playbook.yml`. Worst case you
  recreate the container and re-issue certs — nothing to restore.

### Postgres

The one with durable state (`pg_data` volume) and a genuine major-upgrade
procedure.

- **Patch / minor** (within `18-trixie`): safe in place. The on-disk format
  is stable within a major, so force-pull the tag and the new binary starts
  on the existing data directory. No migration.

  ```bash
  ssh root@nc.example.com \
    'cd /opt/docker-conf && docker compose pull postgres && docker compose up -d postgres && docker image prune -f'
  ```

- **Major** (18 → 19): you **cannot** swap the tag — a major-19 container
  refuses to start on a major-18 data directory. Use the dedicated playbook,
  which takes a full backup, dumps the DB, wipes the volume, re-inits at the
  new major, reloads the dump, and (once the upgrade is verified healthy)
  prunes the old major's now-orphaned data volume:

  ```bash
  ansible-playbook pg-major-upgrade.yml --ask-vault-pass \
    -e postgres_target_image=postgres:19-trixie
  ```

  See its header for details. **Afterward, update `postgres_image` in
  `all.yml` to the new major** (the playbook prints this reminder) — otherwise
  a later `playbook.yml` run re-renders the old major and refuses to start on
  the new data dir. This is the same source-of-truth footgun described for
  Nextcloud majors in [nextcloud-upgrade.md](nextcloud-upgrade.md). Cross only one major at a time.

  Both footguns above are caught by pre-flight guards: `pg-major-upgrade.yml`
  refuses a jump that isn't exactly one major ahead (or that can't reach the
  running DB, or names a tag that doesn't exist) before it deletes anything;
  and a normal `playbook.yml` run refuses to re-render a `postgres_image` (or
  `nextcloud_image`) pinned *behind* what's actually deployed, catching a
  forgotten `all.yml` bump before it tries to downgrade. Override the
  major-jump check with `-e skip_version_guard=true` if you ever truly need to.

---

## Nextcloud image config drift (watch the logs after image updates)

The Nextcloud image ships a set of config snippets in
`/usr/src/nextcloud/config/` (`redis.config.php`, `s3.config.php`,
`smtp.config.php`, ...) that its entrypoint copies into the persistent
config directory on `nc_data` at install time. After an image update, the
*shipped* versions can gain fixes/features while the *copies* on the volume
stay frozen at whatever version first installed them. When that happens,
Nextcloud logs a warning that a config file "differs from the latest
version" — upstream warns that leaving it unresolved can quietly break the
documented env-var behavior those snippets implement.

If you see that warning, sync the copies (upstream's own documented fix):

```bash
ssh root@nc.example.com \
  'docker exec nextcloud_app sh -c "cp /usr/src/nextcloud/config/*.php /var/www/html/config/"'
```

This only refreshes the image-owned snippet files — your real instance
config lives in `config/config.php`, which this doesn't touch.

## Where Nextcloud's own logs go

`playbook.yml` sets `log_type: errorlog` on first install, so Nextcloud's
structured application log (the same JSON entries `data/nextcloud.log` would
otherwise hold) goes to each container's stderr instead of a file on the
`nc_data` volume — which both keeps it out of your backups and puts it under
the same `journald` retention policy (`journald-retention.conf.j2`) as
everything else on the host, rather than growing unrotated forever.

```bash
journalctl CONTAINER_NAME=nextcloud_app -f     # web-triggered log entries
journalctl CONTAINER_NAME=nextcloud_cron -f    # background-job log entries
```

This is a fresh-install-only setting (it lives in `config.php` on `nc_data`,
so a restored or migrated instance already carries it). If you're on an
existing install from before this was added, set it once by hand:

```bash
docker exec nextcloud_app php occ config:system:set log_type --value=errorlog
```

---

## Unpinned images (aside)

`alpine:latest` (backup/restore helpers) still floats on `latest`. That's
acceptable — those helpers are ephemeral and hold no state.
