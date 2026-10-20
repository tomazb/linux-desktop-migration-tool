# Fedora XFCE Migration Implementation Plan

> For agentic workers: use the executing-plans / parallel-agent workflow and
> regression tests for changes that can overwrite user data.

**Goal:** Portable, verified Fedora XFCE 43 backups and reviewed restores on 44.

**Architecture:** A small Python CLI coordinates independent bundle and package
modules. The original shell script remains available for its existing workflow.

**Tech stack:** Python 3 standard library, DNF5, optional Flatpak, SELinux tools.

**Spec:** `docs/superpowers/specs/2026-10-20-fedora-xfce-migration.md`

## Global constraints

- Backup Fedora 43, restore Fedora 44, verify anywhere with Python.
- No third-party Python dependencies; no shell=True or evaluation of manifests.
- Restore is a preview unless `--apply` is supplied; package installation opt-in.
- Existing destination files must have recoverable prior copies before replacement.
- No direct changes to main or automatic merge; publish a reviewable branch/PR.

## Review focus

- Malformed manifests and symlinks must not cause writes outside the destination.
- Missing optional tools must not invent an empty successful inventory.
- Changed files or partial copies must not produce a completed valid backup.
- Package-manager failure must stop the operation with a nonzero status.
- Running XFCE and differing home paths require clear actionable handling.

## Tasks

- [x] Bundle module `fedora_migration/bundle.py`, tested in `tests/test_bundle.py`:
  write failing real-filesystem tests; implement discovery, atomic backup, full
  validation, dry-run restore and overwrite journal; run the complete suite.
  Interfaces: `create_bundle(home, output, source, packages, *, include=(),
  include_secrets=False, settings_only=False) -> dict`, `verify_bundle(path) ->
  dict`, `restore_bundle(bundle, home, *, dry_run=True, backup_dir=None) -> dict`.
- [x] Packages module `fedora_migration/packages.py`, tested in
  `tests/test_packages.py`: test export syntax and errors before implementation;
  implement `collect_packages(*, skip_rpm=False, skip_flatpak=False) -> dict`,
  `validate_package_manifest(data) -> None`, `package_commands(data, *,
  skip_rpm=False, skip_flatpak=False) -> list[list[str]]`, and
  `install_packages(data, *, skip_rpm=False, skip_flatpak=False) -> None`.
  Collect name.arch requests and installed inventory; preserve Flatpak ref/origin/scope;
  validate remotes and propagate transaction errors.
- [x] CLI `fedora-migration.py` and `fedora_migration/cli.py`, with
  `tests/test_cli.py`: test help/verify and mocked platform/session refusal,
  default preview, explicit apply/package selection, and reported failures;
  implement Fedora/source/architecture/user/session checks and SELinux relabeling.
- [x] Rewrite README around the new supported workflow; add
  `docs/FEDORA_43_TO_44.md` with primary-source links, exact commands,
  inclusion/recovery/privacy limitations and a manual VM rehearsal.
- [x] Add CI for Python unit tests and shell syntax in Fedora 43 and 44 containers.
- [x] Compare Codeberg commit `7cc2443b50f710ea9a978d8391862cf764826bd6` and adapt
  useful legacy Flatpak/GnuPG/group-check features with isolated Bash regressions;
  document accepted behavior, retained fork fixes and upstream provenance.
- [x] Run the full suite, a standalone CLI backup/verify/restore rehearsal on
  fixtures, and an independent branch review; address concrete findings.
- [x] Publish all changes on `codex/fedora-43-to-44-xfce` based on upstream commit
  `272c2d63fc003c308071b2cbb0a42225597efa14`, open a draft PR, and report limits.

## Verification record before publication

- Full suite: 90 tests passed, including 21 isolated legacy Bash regressions.
- `bash -n migration.sh` and `git diff --check` passed.
- A fixture CLI rehearsal passed backup, standalone verification, preview, apply,
  overwritten-file recovery and unrelated-file retention. Platform, package,
  session and SELinux operations were mocked; file operations were real.
- Independent reviews of the Python workflow and selective Codeberg port completed.
  The Python review's XDG, timestamp, interruption and journal-performance findings
  have regression coverage and corrections. The legacy review found no blocking
  new defect and checked real local rsync filtering on disposable fixtures.
- Live Fedora XFCE migration and user-hardware behavior remain unverified.
  Fedora container CI results will be visible on the published pull request.

## Fedora CI follow-up

Published draft PR #1. The first Fedora 43/44 container runs passed all 90 tests
but caught a real DNF5 selector error: `--installed` cannot be combined with
`--userinstalled`. Corrected the request query and strengthened its mocked
regression to reject that combination. The separate complete installed-inventory
query still uses `--installed`. CI is rerun on the correction before final delivery.
