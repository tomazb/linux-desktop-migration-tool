# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Separate `fedora-migration.py` backup, verification and restore workflow for
  Fedora XFCE 43 to a freshly installed Fedora XFCE 44 system.
- Portable private backup directories with checksummed manifests, selected
  settings/app data, opt-in secrets, RPM requests/inventory and Flatpak origins/scopes.
- Restore preview by default, explicit apply and package-install options,
  overwritten-file recovery copies and a restore journal.
- Automated filesystem/manifest/package-command checks and a documented live
  two-VM rehearsal procedure; successful real Fedora desktop migration remains
  unverified pending that rehearsal.
- Fedora 43 to 44 guide separating clean-install migration from DNF5 OS upgrade.
- Commit-pinned Codeberg comparison and provenance notes for selectively adapted
  upstream features, retaining original authorship and GPL licensing.
- Default personal-background selection, opt-in GNOME Online Accounts configuration
  alongside keyrings, and a logical payload-byte summary in the Python CLI.

### Adapted from Codeberg upstream

- Legacy Flatpak migration keeps complete refs, origins and user/system scope,
  validates matching remote URLs, and avoids implicit remote creation, scope
  fallback or architecture rewriting.
- Legacy GnuPG migration copies the complete selected keyring rather than a
  public-key-only export, privately stages persistent files and refuses a nonempty
  target keyring. Group membership uses `id -nG` with exact checks.
- Preserved this fork's previous shell fixes instead of replacing its script
  wholesale. Legacy live Fedora XFCE 43/44 migration remains unverified.

### Added (rollback)

- `rollback --rollback-dir DIR` previews, and with `--apply` performs, an undo of a
  restore: saved originals are put back and files or symlinks the restore added are
  removed, but only where the path still holds what the restore wrote (the bundle
  is verified in full; `--bundle` points at it if it moved; `--rewrite-home`
  changes count as restored). Changed paths are reported as conflicts and left
  alone unless `--include-changed` is given. Created directories are removed only
  when empty.
- Rollback saves every current version under `undone-<time>/` before overwriting
  or removing it, records its own `rollback-journal.json` (`complete`, `partial`,
  `failed` or `interrupted`), refuses a second run after `complete`, and works on
  failed or interrupted restores. It requires the same guards as `restore --apply`.
- The restore journal now records the `rewrite_home` pair it used (or `null`).

### Added (restore preview report)

- `restore` preview and apply print read-only "Destination checks": how many
  existing files or symlinks are replaced versus created, which bundle settings
  files still name the old home path (with a count per file), panel plugins
  missing on the destination, and autostart entries whose program is not found.
  The findings are informational and do not block the restore.
- `restore --rewrite-home` (with `--apply`) replaces the old home path with the
  destination home in a fixed set of small XFCE, GTK, launcher and Thunar settings
  files, only after each file's checksum is validated. Browser, mail and Flatpak
  data are never scanned or modified; without the option nothing is rewritten.

### Added (backup safety)

- `backup --exclude PATTERN` (home-relative paths or one-segment globs, winning
  over includes) and default exclusion of the XFCE monitor layout and Flatpak
  app caches; `--no-default-excludes` keeps them. Exclusions are reported.
- Backup and `restore --apply` refuse while Firefox, Thunderbird or Flatpak apps
  run, and while key agents run when secrets are part of the operation.
- Backup computes each file's checksum while copying instead of reading it again.

### Hardened

- Correct the RPM request query to use DNF5's `--userinstalled` selector alone;
  Fedora container CI caught its incompatibility with `--installed`.
- Reject unsupported nondefault XDG config/data layouts and escaped user-directory
  assignments; bound manifest mtime to signed 64-bit nanoseconds.
- Write a complete restore intent plan before payload changes; retain saved
  originals and report the rollback path on failure or Ctrl+C interruption.
- Clarify content/tree verification versus metadata replay, configured DNF
  exclusions, and traversal of mounted subtrees within selected paths.
- Create copied files with mode `0600` from the start; restore keeps temporary
  files private through checksum validation and applies the manifest mode and
  timestamps only immediately before atomic replacement.
- Stage recovery copies privately and publish them under `previous/` only once
  complete and synced, so a failed or interrupted save leaves no partial original.
  File metadata is applied before fsync, and new rollback and recovery directories
  are synced before the original is overwritten.
- Backup and restore run with umask `077`, so a restrictive user umask (even
  `0777`) cannot make the tool's own new files and directories inaccessible.
- Reject a symlinked or non-regular `.config/user-dirs.dirs` during personal-folder
  discovery instead of silently selecting default folder names.
- Back up and restore filenames containing a backslash on Linux; previously one
  such file failed the whole backup after all copying. Backslash paths are still
  rejected where the platform treats `\` as a separator, and an unsupported name
  now fails when copying reaches it rather than at final verification.
- Restore preview verifies the bundle once instead of twice; apply verifies once
  for planning and again immediately before writing (previously three times).
- Shared Flatpak validation cases for the Python and legacy Bash workflows. Bash
  now requires a two-element application ID and validates in the C locale, so
  non-ASCII names and refs are rejected under any locale. Both reject ASCII spaces
  and control characters in remote URLs; non-ASCII URLs remain accepted.

### Documentation

- Corrected the legacy invocation to `bash migration.sh` and clearly separated
  its GNOME/SSH scope from the Fedora XFCE workflow. Selected upstream ports are
  documented separately and are not verified by a live Fedora XFCE 43/44 run.

The following existing unreleased entries describe earlier legacy-script work:

### Fixed

- Fixed corrupted line in Toolbx container migration that merged two commands together
- Fixed typo `mdir` → `mkdir` in SSH migration section
- Fixed typo `Processsing` → `Processing` in network migration output
- Fixed `connection_name` variable that was incorrectly placed outside its loop
- Prevent empty copy from temporary `.ssh` directory and avoid glob errors

### Changed

- Added `set -o pipefail` for safer pipeline execution
- Extracted repeated rsync commands into reusable `rsync_from_remote()` function
- Replaced hardcoded values with constants (`MAX_PASSWORD_ATTEMPTS`, `BYTES_PER_GB`)
- Initialized `dir_to_copy` array properly with `declare -a`
- Use `mktemp` for secure temporary directory creation during SSH migration
- Added `mkdir -p` calls before rsync operations to ensure destination directories exist
- Hardened script with `set -euo pipefail` and ERR trap
- Added local and remote dependency checks (rsync, sshpass, xdg-user-dir, bc, etc.)
- Restore SELinux contexts after migrations when SELinux is enabled

