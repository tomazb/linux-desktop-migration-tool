# Fedora XFCE 43 to 44 migration design

## Goal

Make this repository useful for preserving a Fedora XFCE 43 desktop when moving
to a freshly installed Fedora XFCE 44 system, including reinstalling the same
machine. A portable backup also supports preparation for an in-place upgrade.
The tool does not perform the OS upgrade or install an operating system.

## Workflow

Add `fedora-migration.py` with `backup`, `verify`, and `restore` commands.
Use Python's standard library, available on Fedora, with DNF5/Flatpak invoked
only for their package operations. Keep the existing `migration.sh` as the
documented legacy GNOME/SSH workflow.

Backup records source OS/home/architecture, selected files and SHA-256 checksums,
RPM name.arch requests and inventory, and Flatpak origin/ref/installation scope.
Defaults include XFCE, Thunar, GTK, launchers/themes/fonts/backgrounds, XDG personal directories,
Firefox/Thunderbird profiles and Flatpak data. Additional home-relative paths are
explicit. Keys are opt-in. Do not include the entire home or session caches.
The output is a new directory outside the source home and is private by default.
GNOME Online Accounts configuration is opt-in alongside keyrings. Nondefault XDG
config/data locations are rejected so their omission is explicit.

Verify checks the manifest schema, path containment, file types and all recorded
file hashes. It must work without Fedora or package managers. Checksums detect
corruption, not malicious replacement of both data and manifest.

Restore previews by default and requires `--apply` for writes. Validate the
complete bundle and destination before changes. Overlay selected files without
deleting unrelated destination files; retain overwritten files and a restore
journal in a private rollback directory. Persist the complete intent plan before
changes and report retained originals on failure or interruption. Reject directory/type conflicts and
symlink ancestors instead of following them. Reapply target SELinux labels.

Package installation is separately opt-in with `--install-packages`, retains
native package-manager confirmation, uses argument arrays, and never restores
old RPM versions or repository configuration blindly. Missing/changed Flatpak
remotes and package-manager failures must be actionable errors, not silent skips.

Backup/restore run as the normal desktop user on Fedora 43/44 respectively.
Require the user's XFCE session and xfconfd to be stopped before copying settings.
No automatic logout, daemon kill, reboot, network reconfiguration or key import.

## Validation and limits

Use temporary source/destination homes for filesystem round trips, corrupt and
malicious manifests, skipped special files, dry runs and overwrite recovery.
Mock only external package-manager/process calls in unit tests. Cover full RPM
name.arch requests, first Flatpak entry, origins/scopes, and transaction errors.
Add CI on Fedora 43 and 44 and a documented two-VM rehearsal checklist.
Automated tests do not establish successful migration on the user's hardware.
Same username simplifies absolute paths; differing usernames, monitor layouts,
third-party repositories, custom Flatpak installations, ACLs and external mounts
need explicit documentation of support or limits.

## Added upstream integration scope

The user also requested useful features from the newer Codeberg version. Compare
commit `7cc2443b50f710ea9a978d8391862cf764826bd6` and selectively adapt the legacy
script's Flatpak identity/scope handling, complete GnuPG state migration, and exact
privilege-group checks. Preserve this fork's existing SSH/Toolbx/SELinux repairs.
Record attribution and individual decisions in `docs/UPSTREAM_CODEBERG.md`.
Test these Bash helpers with isolated command stubs and synthetic keyring files;
live legacy SSH migration remains a separate rehearsal.
