#!/usr/bin/env python3
"""
dnf-check-update-style report for the pinned container images in
group_vars/all.yml, plus end-of-life status for whatever version each is
currently pinned to.

By default this runs entirely from the control machine against Docker Hub
and endoflife.date - it never touches the VPS, so it's safe to run anytime,
as often as you like.

Usage: ./check-updates.py          (from anywhere; paths are resolved
       ./check-updates.py --live    relative to this script's own location)

Three things get reported for each image, because they call for different
responses:

  - "latest patch under your pin" is informational by itself. Postgres/
    Valkey are major-pinned and Traefik is minor-pinned (see
    docs/maintenance.md), so newer patches within that pin are picked up
    automatically the next time you force-pull - nothing to edit. Whether
    you're actually behind it is a separate question - see --live below.
  - "newer line available" means a tag beyond your current pin boundary
    exists (a new Traefik minor, or a new Postgres/Nextcloud/Valkey major).
    That's the signal to make a deliberate decision - edit group_vars/all.yml
    and follow the relevant upgrade doc. It is never applied automatically.
  - "deployed" (only with --live) is what's actually running on the VPS
    right now, fetched by running check-deployed-versions.yml over SSH via
    your inventory.ini. Without this, the script has no way to know if a
    floating tag's registry-side patch (e.g. pinned "9-alpine", registry has
    9.1.5) has actually reached your container - Docker never re-pulls a
    tag on its own, only an explicit `docker compose pull` does. This is
    the one part of the script that touches the VPS, which is why it's
    opt-in rather than the default.

Exit status: 0 if nothing needs attention, 1 if a newer line is available,
an EOL date has passed or is coming up within EOL_WARN_DAYS, and/or (with
--live) the deployed version is behind the latest under its pin - so this
can be wired into cron/monitoring later as a yes/no signal without having to
parse the printed report.

Limitations, so the report isn't over-trusted:
  - Each image is scanned via a Docker Hub `name` substring filter, walked
    across up to MAX_PAGES pages. This is a convenience check bounded for a
    quick manual run, not an exhaustive registry scan (though in practice
    it comfortably covers each image's full release history at MAX_PAGES=10).
  - Valkey's EOL data upstream is tracked per-minor (9.0, 9.1, ...) while
    the pin here floats at the major (9-alpine). The EOL shown is for
    whichever minor is currently newest under that major, since that's
    what's actually running at any given time.
  - Not every image supports every column. socket-proxy has no EOL data
    upstream and no way to report its own version from inside the
    container, so it gets the release check only - see its IMAGES entry.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ALL_YML = Path(__file__).parent / "group_vars" / "all.yml"
INVENTORY = Path(__file__).parent / "inventory.ini"
DEPLOYED_VERSIONS_PLAYBOOK = Path(__file__).parent / "check-deployed-versions.yml"
HUB_API = "https://hub.docker.com/v2/repositories/{repo}/tags"
EOL_API = "https://endoflife.date/api/{slug}.json"
USER_AGENT = "check-updates.py (docker-nextcloud-ansible)"
EOL_WARN_DAYS = 90

# One entry per pinned image. prefix/suffix bracket the numeric version in
# a full tag (e.g. traefik's "v3.7.7" = prefix "v", suffix ""; postgres's
# "18.2-trixie" = prefix "", suffix "-trixie"). Every Docker Hub repo listed
# here uses this same "<prefix><numbers><suffix>" shape for real release
# tags, which is what makes one shared parser workable.
#
# eol_slug is OPTIONAL: omit it for a project endoflife.date doesn't track,
# and the EOL row is skipped for that image.
#
# deployed_task/deployed_regex/deployed_json_field are OPTIONAL too, and
# only used with --live: they match a task name in
# check-deployed-versions.yml's output to a regex (or, for Nextcloud, a JSON
# field) that pulls a plain "X.Y.Z" version string back out of that
# command's raw stdout. Omit them for a container that can't report its own
# version, and the deployed row is skipped rather than showing a permanent
# "could not determine".
IMAGES = [
    {
        "label": "Traefik",
        "var": "traefik_image",
        "hub_repo": "library/traefik",
        "prefix": "v",
        "suffix": "",
        "eol_slug": "traefik",
        "deployed_task": "check traefik version",
        "deployed_regex": r"Version:\s*v?([\d.]+)",
    },
    {
        # Release-check only, deliberately. endoflife.date tracks no cycle
        # for this project, and the container is HAProxy-based with no
        # command that reports the proxy's OWN release (haproxy -v gives
        # HAProxy's version, not v0.5.0), so there's nothing for --live to
        # probe. Listed here anyway because this is the one image pinned to
        # an exact release: it never picks anything up from a force-pull, so
        # without a release check it would silently rot. Note its version
        # line is 0.x, where new releases bump the MINOR - "newer line
        # available" is therefore the signal to watch here, not a new major.
        "label": "socket-proxy",
        "var": "socket_proxy_image",
        "hub_repo": "tecnativa/docker-socket-proxy",
        "prefix": "v",
        "suffix": "",
    },
    {
        "label": "Postgres",
        "var": "postgres_image",
        "hub_repo": "library/postgres",
        "prefix": "",
        "suffix": "-trixie",
        "eol_slug": "postgresql",
        "deployed_task": "check postgres version",
        "deployed_regex": r"PostgreSQL\)\s*([\d.]+)",
    },
    {
        "label": "Nextcloud",
        "var": "nextcloud_image",
        "hub_repo": "library/nextcloud",
        "prefix": "",
        "suffix": "-apache",
        "eol_slug": "nextcloud",
        "deployed_task": "check nextcloud version",
        "deployed_json_field": "versionstring",
    },
    {
        "label": "Valkey",
        "var": "valkey_image",
        "hub_repo": "valkey/valkey",
        "prefix": "",
        "suffix": "-alpine",
        "eol_slug": "valkey",
        "deployed_task": "check valkey version",
        "deployed_regex": r"v=([\d.]+)",
    },
]


def read_pin(var_name):
    text = ALL_YML.read_text()
    m = re.search(rf'^{re.escape(var_name)}:\s*"?([^"\s#]+)"?', text, re.MULTILINE)
    if not m:
        sys.exit(f"error: {var_name} not found in {ALL_YML}")
    return m.group(1).split(":", 1)[1]  # "traefik:v3.7" -> "v3.7"


def parse_version(tag: str, prefix: str, suffix: str) -> tuple[int, ...] | None:
    """Return a tuple of ints for a real release tag, or None if this tag
    isn't one (pre-releases, platform variants, floating aliases, etc.)."""
    if not tag.startswith(prefix) or not tag.endswith(suffix):
        return None
    middle = tag[len(prefix): len(tag) - len(suffix) if suffix else len(tag)]
    if not re.fullmatch(r"\d+(\.\d+){0,2}", middle):
        return None
    return tuple(int(p) for p in middle.split("."))


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


MAX_PAGES = 10  # 10 x page_size 100 = up to 1000 tags per query, per image


def fetch_tags_matching(image: dict, name_filter: str) -> dict[tuple[int, ...], str]:
    """All tags (up to MAX_PAGES worth) whose name contains name_filter as
    a substring - Docker Hub's `name` query param, not a prefix/anchor
    match. Paginated rather than trusting a single page: Docker Hub's
    last_updated timestamp can bump on OLD tags too (e.g. a registry-side
    manifest/metadata update touching historical tags), so sorting by
    recency and reading only page 1 can miss the actual newest release -
    walking every matching page is what makes this reliable.

    Returns {version_tuple: release_date_str}, where release_date_str is
    the tag's Docker Hub last_updated date (the day it was actually pushed
    to the registry - i.e. its effective release date)."""
    versions = {}
    url = (
        f"{HUB_API.format(repo=image['hub_repo'])}"
        f"?page_size=100&name={urllib.parse.quote(name_filter)}"
    )
    for _ in range(MAX_PAGES):
        if not url:
            break
        try:
            data = fetch_json(url)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  ! could not reach Docker Hub ({name_filter}): {e}")
            break
        for result in data.get("results", []):
            v = parse_version(result["name"], image["prefix"], image["suffix"])
            if v:
                versions[v] = result.get("last_updated", "")[:10]
        url = data.get("next")
    return versions


def fetch_tag_versions(image: dict, pin_numbers: tuple[int, ...]) -> dict[tuple[int, ...], str]:
    """Returns {version_tuple: release_date_str} - see fetch_tags_matching."""
    if image["suffix"]:
        # Postgres/Nextcloud/Valkey: the suffix alone (e.g. "-trixie",
        # "-apache", "-alpine") is shared across every major, so one
        # filtered, fully-paginated query covers our current pin and any
        # newer line at once.
        versions = fetch_tags_matching(image, image["suffix"])
    else:
        # Traefik has no shared suffix to filter on. Scope to the current
        # major (catches a newer minor) and explicitly peek one major
        # ahead (catches a fresh major bump) - two bounded queries rather
        # than an unfiltered scan of the whole repo's v1/v2/v3 history.
        current_major = f"{image['prefix']}{pin_numbers[0]}"
        next_major = f"{image['prefix']}{pin_numbers[0] + 1}"
        versions = fetch_tags_matching(image, current_major)
        versions.update(fetch_tags_matching(image, next_major))

    return versions


def release_age(date_str):
    """'released 2026-07-15 (2 days ago)', or '' if no date is available -
    lets the reader judge whether a flagged update is fresh enough that
    they'd rather wait a few days before adopting it."""
    if not date_str:
        return ""
    try:
        release_date = datetime.date.fromisoformat(date_str)
    except ValueError:
        return ""
    days = (datetime.date.today() - release_date).days
    if days <= 0:
        age = "today"
    elif days == 1:
        age = "1 day ago"
    else:
        age = f"{days} days ago"
    return f"released {date_str} ({age})"


def fetch_eol(slug):
    try:
        return fetch_json(EOL_API.format(slug=slug))
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  ! could not reach endoflife.date: {e}")
        return None


def fetch_deployed_stdout():
    """Runs check-deployed-versions.yml over SSH and returns {task_name:
    stdout}, omitting any task that failed or didn't run (missing
    container, unreachable host, etc.) - callers treat a missing entry as
    'could not determine' rather than an error."""
    if not INVENTORY.exists():
        print(f"  ! {INVENTORY} not found - copy inventory.ini.example and "
              f"configure it first; skipping --live checks\n")
        return {}

    env = {**os.environ, "ANSIBLE_STDOUT_CALLBACK": "json"}
    try:
        result = subprocess.run(
            ["ansible-playbook", "-i", str(INVENTORY), str(DEPLOYED_VERSIONS_PLAYBOOK)],
            capture_output=True, text=True, timeout=60, env=env,
        )
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as e:
        print(f"  ! could not query deployed versions: {e}\n")
        return {}

    stdout_by_task = {}
    for task in data.get("plays", [{}])[0].get("tasks", []):
        host_results = task.get("hosts", {})
        if not host_results:
            continue
        host_data = next(iter(host_results.values()))
        if not host_data.get("failed"):
            # Keyed lowercase, and looked up the same way in
            # parse_deployed_version, so the playbook's task-name casing
            # doesn't matter - ansible-lint's name[casing] rule forces those
            # names to start capitalized, and a mismatch here fails silently
            # as "could not determine" rather than as an error.
            stdout_by_task[task["task"]["name"].lower()] = host_data.get("stdout", "")
    return stdout_by_task


def parse_deployed_version(image, stdout_by_task):
    """Returns a version tuple parsed from the deployed-versions playbook's
    output for this image, or None if it couldn't be determined."""
    stdout = stdout_by_task.get(image["deployed_task"].lower())
    if not stdout:
        return None
    if "deployed_json_field" in image:
        try:
            version_str = json.loads(stdout)[image["deployed_json_field"]]
        except (json.JSONDecodeError, KeyError):
            return None
    else:
        m = re.search(image["deployed_regex"], stdout)
        if not m:
            return None
        version_str = m.group(1)
    try:
        return tuple(int(p) for p in version_str.split("."))
    except ValueError:
        return None


def format_eol(entries, cycle_candidates):
    """Returns (label, value, needs_attention)."""
    if entries is None:
        return None, None, False
    by_cycle = {e["cycle"]: e for e in entries}
    for cycle in cycle_candidates:
        if cycle in by_cycle:
            entry = by_cycle[cycle]
            label = f"EOL ({cycle})"
            eol = entry.get("eol")
            if eol in (False, None):
                return label, "not yet announced", False
            eol_date = datetime.date.fromisoformat(eol)
            days = (eol_date - datetime.date.today()).days
            if days < 0:
                return label, f"{eol} - ⚠ ALREADY END-OF-LIFE", True
            if days <= EOL_WARN_DAYS:
                return label, f"{eol} - ⚠ in {days} days", True
            return label, f"{eol} ({days // 365}y {days % 365 // 30}mo away)", False
    return "EOL", f"no matching cycle found upstream for {cycle_candidates[0]}", False


def main():
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--live", action="store_true",
        help="Also SSH into the VPS (via inventory.ini) to check actually-deployed "
             "versions, not just what's available upstream. The only flag that "
             "touches the VPS.",
    )
    args = parser.parse_args()

    print(f"Checking pinned images against {ALL_YML} ...\n")
    attention_needed = False
    deployed_stdout = fetch_deployed_stdout() if args.live else {}

    # Field-label column width shared by every row printed below, so values
    # line up regardless of which field ("pinned", "latest under this pin",
    # "deployed", "EOL (x.y)", ...) is longest for a given image.
    FIELD_WIDTH = 22

    def row(label, value):
        print(f"  {label:<{FIELD_WIDTH}} {value}")

    for image in IMAGES:
        pin = read_pin(image["var"])
        pin_numbers = parse_version(pin, image["prefix"], image["suffix"])
        if pin_numbers is None:
            print(image["label"])
            row("pinned", f"{pin} (unrecognized tag shape, skipping)")
            print()
            continue

        versions = fetch_tag_versions(image, pin_numbers)
        pin_len = len(pin_numbers)  # how many components the pin itself specifies

        within_pin = [v for v in versions if v[:pin_len] == pin_numbers]
        beyond_pin = [v for v in versions if v[:pin_len] > pin_numbers]

        latest_within = max(within_pin) if within_pin else None

        print(image["label"])
        row("pinned", pin)

        # Informational by itself: the pin (e.g. "v3.7", "18-trixie") is a
        # floating alias, so a plain force-pull always fetches whichever
        # patch is newest under it - there's nothing to compare without
        # knowing what's actually deployed, which is what --live is for.
        if latest_within:
            tag_str = image["prefix"] + ".".join(str(n) for n in latest_within) + image["suffix"]
            row("latest under this pin", tag_str)
        else:
            row("latest under this pin", "unknown (no matching tags found in recent history)")

        # Images with no deployed_task can't answer "what's actually
        # running" at all, so neither the row nor the --live hint applies -
        # showing either would just be permanent noise.
        supports_live = "deployed_task" in image

        if args.live and supports_live:
            deployed = parse_deployed_version(image, deployed_stdout)
            if deployed is None:
                row("deployed", "could not determine (container missing, host unreachable, etc.)")
            elif latest_within and deployed < latest_within:
                deployed_str = ".".join(str(n) for n in deployed)
                latest_str = ".".join(str(n) for n in latest_within)
                age = release_age(versions.get(latest_within, ""))
                age_suffix = f", {age}" if age else ""
                row("⚠ deployed", f"{deployed_str} - behind the latest {latest_str}{age_suffix}; "
                                   f"force-pull to update (see docs/maintenance.md)")
                attention_needed = True
            else:
                row("deployed", f"{'.'.join(str(n) for n in deployed)}  (up to date)")
        elif latest_within and supports_live:
            row("", "(run with --live to check whether this is actually what's deployed)")

        if beyond_pin:
            newest_line = max(v[:pin_len] for v in beyond_pin)
            newest_full = max(v for v in beyond_pin if v[:pin_len] == newest_line)
            line_str = image["prefix"] + ".".join(str(n) for n in newest_line)
            age = release_age(versions.get(newest_full, ""))
            age_suffix = f", {age}" if age else ""
            row("⚠ newer line available", f"{line_str}{age_suffix}  -> see docs/maintenance.md before switching")
            attention_needed = True

        # EOL lookup: try the version at the pin's own granularity first
        # (exact match for Traefik/Postgres/Nextcloud), then fall back to
        # a coarser major-only cycle (handles Valkey's major-only pin
        # against endoflife.date's per-minor cycles).
        # Skipped entirely for an image with no eol_slug - endoflife.date
        # has nothing to say about it, and querying a made-up slug would
        # just print a 404 warning on every run.
        if image.get("eol_slug"):
            eol_entries = fetch_eol(image["eol_slug"])
            lookup_source = list(latest_within or pin_numbers)
            candidates = []
            if len(lookup_source) >= 2:
                candidates.append(".".join(str(n) for n in lookup_source[:2]))
            candidates.append(str(lookup_source[0]))
            eol_label, eol_value, eol_attention = format_eol(eol_entries, candidates)
            if eol_label:
                row(eol_label, eol_value)
            attention_needed = attention_needed or eol_attention

        print()

    return 1 if attention_needed else 0


if __name__ == "__main__":
    sys.exit(main())
