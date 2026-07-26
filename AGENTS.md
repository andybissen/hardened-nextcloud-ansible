# AGENTS.md

Guidance for AI coding assistants (Claude Code, Codex, Cursor, etc.) working
in this repo. Read this before proposing changes — several things that look
like cleanups or bugs are deliberate design decisions with a reason
documented either here or in the code's own comments.

For what this project *is* and why, start with [README.md](README.md); for
how to actually operate it, see [docs/](docs/). This file is scoped to
things an assistant would otherwise have to rediscover the hard way.

## Hard constraints — don't "fix" these without flagging it first

- **RHEL-family only** (Rocky/RHEL/Alma). There is no Debian/Ubuntu support
  and none is planned — don't suggest `apt`, Debian package names, or
  distro-agnostic abstractions for the `dnf`/`firewalld`/SELinux tasks.
- **Root SSH is the deliberate connection model.** Every playbook uses
  `ansible_user=root` + `become: false`. This is a conscious trade-off for a
  single-operator personal VPS, not an oversight — don't propose switching
  to `become: true` with a sudo user without calling out that it's a real
  behavioral change, not a lint fix.
- **No Ansible roles, on purpose.** The repo uses flat playbooks + shared
  `tasks/*.yml` includes instead of the Galaxy role layout
  (`roles/foo/tasks`, `defaults`, `meta`, ...). This isn't meant to be
  consumed as reusable/published content, so don't restructure it into
  roles as a "best practice" cleanup.
- **The `z-` prefix on custom Apache/PHP config filenames is load-bearing**,
  not cosmetic — `conf.d`/`conf-enabled` load alphabetically, and `z-`
  guarantees these load after the image's own stock files (which otherwise
  silently win). Preserve this convention for any new custom config file.
- **`vault_valkey_password` must stay alphanumeric only.** It's templated
  unescaped into a `requirepass` config line (`requirepass {{ ... }}`), and
  at runtime it's `cat`'d from its secret file onto a `valkey-cli -a` shell
  command line in the container healthcheck — a non-alphanumeric character
  (space, `#`, `$`, `"`, `\`) breaks one or both. This is enforced by a
  preflight `assert` — don't relax it without fixing both call sites
  (`tasks/render_config.yml` and the healthcheck in
  `templates/docker-compose.yml.j2`).
- **Secrets never appear in `docker-compose.yml`, container env, or
  `docker inspect`.** Everything sensitive goes through file-based Docker
  secrets (`/run/secrets/<name>`, rendered by `tasks/render_config.yml` from
  `vault.yml`). Any new secret must follow this pattern, not an environment
  variable.
- **The `*.example` + gitignored-real-file pattern is the whole secrets
  model.** `group_vars/all.yml`, `group_vars/vault.yml`, `inventory.ini`,
  `files/backup_public_key.asc`, and `backups/` are gitignored on purpose —
  never suggest committing them, and never remove them from `.gitignore`.
  `vault.yml` must never be committed even in its Ansible-Vault-encrypted
  form (see the comment in `vault.yml.example` for why).
- **Backups are not namespaced per deployment.** `remote_backup_dir` /
  `local_backup_dir` and `backup_retention_count` are shared, fixed paths —
  don't assume a backup's filename or location implies which instance
  produced it.

## Patterns to follow for new destructive/irreversible operations

Any playbook that deletes a volume, overwrites a database, or otherwise
can't be trivially undone (see `pg-major-upgrade.yml`, `restore.yml`,
`backup.yml`) follows the same shape — match it for anything new in that
category:

1. Preflight checks run and fail *before* anything destructive starts
   (tooling present, target reachable, version/downgrade guards).
2. Size/sanity checks on intermediate artifacts (dumps, archives) before the
   next irreversible step consumes them.
3. The last known-good copy is never deleted until the next one is
   verified.
4. A guard before referencing `.rc` on any
   `docker_container_exec`/`docker_container_info` result that might target
   a nonexistent container — a missing container fails at the module level
   with no `rc` key at all (confirmed live), so a bare `.rc` reference
   errors instead of skipping cleanly. Two forms are used depending on
   context: `rc is defined` in a `when:` list (see `preflight_install.yml`),
   or `rc | default(1)` inline where a value is needed regardless (see the
   upgrade playbooks and `backup.yml`). Match whichever the surrounding
   tasks use.

## Comment style

Comments in this repo explain **why**, not what — a hidden constraint, a
prior incident, a gotcha confirmed against a live host. Match that: don't
add comments that just restate the next line, and don't strip existing
rationale comments when editing nearby code.

## Secrets hygiene

Never `cat`/print/echo the contents of `vault.yml`, anything under
`secrets/`, or `/run/secrets/*` inside a container. Verify values via
checksums or a functional test (does the connection/auth succeed) instead.

## Before staging or committing

Never `git add -A` / `git add .` without reviewing `git status` first line
by line. Specifically confirm:

- Only `*.example` counterparts are staged for `group_vars/all.yml`,
  `group_vars/vault.yml`, `inventory.ini`, `files/backup_public_key.asc` —
  never the real files, even though none of them "look" like secrets by
  filename alone.
- No `backups/` contents, and nothing under a host-generated cache/tmp dir
  (`.ansible/`, `__pycache__/`) — if something like this shows up that
  `.gitignore` doesn't already cover, fix `.gitignore` itself rather than
  just excluding it by hand this one time.

## Testing

Run `ansible-lint` before finalizing any change to a playbook, task file, or
template:

```bash
ANSIBLE_VAULT_PASSWORD_FILE=~/.nc-vault-pass ansible-lint .
```

The vault password file is needed because five playbooks load the encrypted
`group_vars/vault.yml` via `vars_files`; without it those five fail their
syntax-check step with a decrypt error instead of being linted (see
`.ansible-lint`'s comment). No vault password available in your context (an
isolated worktree, a fresh checkout) is a normal situation, not a blocker —
in that case still run plain `ansible-lint .` and treat the resulting
`internal-error` on those five files as expected noise, not a new problem to
chase.

There's no CI or Molecule setup, deliberately (personal single-host repo,
not published for reuse) — `ansible-lint` is the one check that exists, and
it's run manually/by-agent rather than gated in a pipeline.
