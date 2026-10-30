# Linux Desktop Migration Tool

Move selected settings and personal data from **Fedora XFCE 43 to a fresh Fedora
XFCE 44 installation** using a portable, verified backup directory. The new
`fedora-migration.py` workflow is separate from the original GNOME/SSH script.
It does not install Fedora or perform an operating-system upgrade.

**Validation status:** automated checks cover filesystem and command behavior.
A live two-VM Fedora 43/44 migration rehearsal is still required; this is not a
claim of successful migration on real Fedora desktops or your hardware.

## Choose the workflow

| Goal | Workflow |
| --- | --- |
| Reinstall or move to a new Fedora XFCE 44 machine | Back up on 43, verify, install 44 separately, preview and restore |
| Upgrade the existing Fedora 43 installation to 44 | Make a backup, then follow [DNF5 system upgrade](docs/FEDORA_43_TO_44.md#in-place-upgrade-is-a-separate-operation) |
| Use the original GNOME/SSH migration script | See [Legacy workflow](#legacy-gnomessh-workflow) |

## Requirements

- Python 3; the Python workflow uses the standard library.
- Backup: Fedora 43, run as the normal desktop user.
- Restore: Fedora 44, run as the normal destination desktop user.
- Log out of XFCE and close applications before backup or applying a restore. Run from a
  console TTY with that user's XFCE session and `xfconfd` stopped. The tool does
  not log you out or stop processes automatically.
- A restore preview can run while XFCE is active.
- A new output directory **outside the source home**, with sufficient space.
  Prefer an encrypted external drive with a Linux filesystem.
- Optional package installation: DNF5 and/or Flatpak, working repositories and
  networking. Source and target architectures must match for package install.
- `verify` only needs Python 3 and can run on another operating system.
- Backup/restore require the default `~/.config` and `~/.local/share` XDG layout;
  nondefault `XDG_CONFIG_HOME`/`XDG_DATA_HOME` values are rejected.

An active application may change files while they are read. Close browsers,
mail clients and Flatpak apps as well as XFCE; this is not an application-aware
backup of live databases. Backup and `restore --apply` refuse to start while
Firefox, Thunderbird or any Flatpak app is running and, when keys or keyrings are
part of the operation, while `gpg-agent`, `ssh-agent` or `gnome-keyring-daemon`
runs. The tool never stops these processes itself.

## Quick start

Use your external disk's actual mount path in place of `/mnt/backup`.
Do not use `sudo` to run the Python CLI.

### 1. Back up on Fedora XFCE 43

Save work, log out of XFCE, switch to a console TTY and log in as the source user.
From this checkout:

```bash
python3 fedora-migration.py backup --output /mnt/backup/fedora43-backup
python3 fedora-migration.py verify --bundle /mnt/backup/fedora43-backup
```

The output must be a new directory; choose a different name for another backup.
The bundle is complete only after the command finishes writing the checksum
manifest. Keep the entire directory together. Copy a completed bundle with:

```bash
rsync -a /mnt/backup/fedora43-backup/ /another/mount/fedora43-backup/
python3 fedora-migration.py verify --bundle /another/mount/fedora43-backup
```

Verify the transferred copy before relying on it or erasing the source system.
FAT/NTFS and some network filesystems may not preserve Unix modes or symlinks.
Verify on the destination filesystem; verify checks content/tree/targets, while
restore takes modes/times from the manifest. It does not certify the backup copy's
actual file permissions. This workflow does not create a tar archive.

### 2. Preview on Fedora XFCE 44

Install Fedora XFCE 44 separately, create the destination user and install required
panel plugins, themes and applications. The same username/home path reduces
absolute-path repairs. Log out of XFCE and use a console TTY:

```bash
python3 fedora-migration.py verify --bundle /mnt/backup/fedora43-backup
python3 fedora-migration.py restore --bundle /mnt/backup/fedora43-backup
```

`restore` previews by default. It verifies the complete bundle and reports planned
changes; it does not copy files until `--apply` is supplied.

The preview and the apply run both end with read-only "Destination checks":

- how many existing files or symlinks will be replaced (originals are kept in the
  rollback directory) and how many new ones are created;
- when the source home differs from this one, which bundle settings files still
  name the old home path, with a count per file (see `--rewrite-home` below);
- panel plugins the bundled panel configuration uses that are not installed here,
  and autostart entries whose program cannot be found.

These findings are informational and never block the restore. Nothing in the bundle
is executed to produce them.

### 3. Apply and check

```bash
python3 fedora-migration.py restore \
  --bundle /mnt/backup/fedora43-backup --apply
```

Restore overlays selected paths and keeps unrelated target files. Files it
replaces are retained under a private rollback directory with a journal. Record
that directory. Its journal is an intent plan written before copying: listed paths
may have been touched; `previous/` contains only complete saved originals, which are
assembled under `staging/` and moved into place when finished. Ctrl+C
returns exit code 130 and reports the retained recovery directory. There is no
automatic rollback command; see the
[recovery instructions](docs/FEDORA_43_TO_44.md#recover-overwritten-files).
File/type conflicts and symlink ancestors are rejected instead of followed.
When SELinux is enabled, target `restorecon` relabels restored paths.

If the destination home path differs from the source's, `--rewrite-home` (with
`--apply`) replaces the old home path with the new one in the files the preview
listed. Only the old path is replaced, byte for byte, and only where it is followed
by `/`, the end of the file or a character that cannot continue a name (so
`/home/alice` never changes `/home/alice2`). Only small XFCE `*.xml` settings, `*.desktop`
launchers under `.config/`, `.local/share/applications/` or directly in `Desktop/`, `.gtkrc-2.0`, GTK bookmarks, `user-dirs.dirs`, `mimeapps.list` and
Thunar text files are considered; browser, mail and Flatpak data are never
touched. Each file is rewritten only after its checksum has been validated, and the
overwritten originals still go to the rollback directory. Without the option nothing
is rewritten.

Log in to XFCE and inspect the panel, shortcuts, display layout and key apps.
Keep both the source backup and rollback files until the desktop is checked.

## What is included

Default settings cover XFCE, Thunar, GTK 3/4, autostart entries, MIME associations,
user-directory configuration, personal themes/icons/fonts/backgrounds, launchers, Bash files
and local executables. Default data covers configured XDG personal directories,
Firefox (`.mozilla`), Thunderbird (`.thunderbird`) and Flatpak app data (`.var/app`).
Only existing selected paths are copied; this is not an entire-home backup.
XFCE saved-session caches are excluded. By default the XFCE monitor layout
(`.config/xfce4/xfconf/xfce-perchannel-xml/displays.xml`, which names the source
machine's outputs) and Flatpak app caches (`.var/app/*/cache`) are also left out,
so the new installation keeps its own display configuration.

| Option | Effect |
| --- | --- |
| `backup --settings-only` | Exclude default personal/app data |
| `backup --include Projects --include .config/my-app` | Add home-relative paths explicitly |
| `backup --exclude 'Downloads/*.iso'` | Leave out a home-relative path or glob (repeatable; `*` matches one path segment; wins over includes) |
| `backup --no-default-excludes` | Also back up the monitor layout and Flatpak app caches |
| `backup --include-secrets` | Opt in to SSH/GPG/PKI/keyrings and GNOME Online Accounts configuration |
| `backup --skip-rpm` / `--skip-flatpak` | Omit that package inventory |
| `restore --apply --install-packages` | Opt in to package reinstallation with native confirmations |
| `restore --skip-rpm` / `--skip-flatpak` | Skip that package installation path |
| `restore --apply --rewrite-home` | Replace the old home path with this home in the settings files the preview lists; without `--apply` the preview shows what would change |
| `restore --apply --rollback-dir /path/new-recovery-dir` | Choose a new private rollback directory |

Known SSH/GPG/PKI/keyring/Online Accounts paths are excluded by default. `--include-secrets` may copy private keys
and credentials; the bundle itself is not encrypted. App profiles may also contain
credentials without this option. Store and transfer backups accordingly.
Explicitly including one of those secret roots or a path beneath it also opts it
in; broad parent includes do not silently opt in to the known secret trees.
GnuPG includes the complete selected `.gnupg` tree, not just public keys. GNOME
Online Accounts configuration is paired with keyring files, but may still require
reauthorization on 44; the tool does not import keys or activate accounts.

## Packages and scope

RPM requests use `name.arch`, allowing Fedora 44 to select its current packages;
old Fedora 43 versions are recorded for reference, not reinstalled. DNF5's
`--userinstalled` also includes group/module and unknown installation reasons.
DNF configuration excludes can affect requests and inventory.
Review unavailable or renamed requests and configure compatible trusted repositories
yourself. Package installation is optional and is not atomic with file restore.

Flatpak records user/system scope, full ref and origin, plus remote name and URL.
Configure matching trusted remotes on 44 before installation. The tool checks the
remote URL and does not automatically add repositories or switch origins. Custom
named Flatpak installations are unsupported; use `--skip-flatpak` and handle those
manually. Flatpak app data and installation requests are separate.

The Python workflow does not migrate system services, NetworkManager profiles,
Toolbx/Podman containers, container volumes or arbitrary system configuration.
Additional application settings outside the defaults need explicit `--include`.
Selected directories are traversed even if they contain mounted subtrees; unrelated
external mounts are not selected automatically. Review capacity before backup;
the CLI reports the logical payload byte total, not extra filesystem overhead.

## Preservation and trust limits

Regular file modes, modification times and symlinks are preserved. Symlinks remain
links; their external targets are not included. ACLs, extended attributes,
ownership and source SELinux labels are not preserved. Restored files belong to
the normal target user; target SELinux policy supplies labels.

Checksums detect corruption. They do **not** authenticate a bundle whose contents
and manifest an attacker can both replace. Only restore a bundle you trust.
Rehearse changed home paths, external symlinks and monitor layouts before erasing 43.

See [the Fedora 43 → 44 guide](docs/FEDORA_43_TO_44.md) for package commands,
manual recovery, authoritative references and the two-VM rehearsal checklist.

## Legacy GNOME/SSH workflow

`migration.sh` is the original interactive SSH-based workflow aimed at Fedora
Silverblue/GNOME. It includes separate GNOME, NetworkManager, keys and Toolbx
operations. Selected changes from the newer Codeberg upstream preserve Flatpak
origin/ref/scope, migrate the complete GnuPG directory and improve group checks,
while retaining this fork's existing fixes. See the
[commit-pinned comparison](docs/UPSTREAM_CODEBERG.md) for adaptations and deferrals.
Flatpak retains native confirmations and requires matching configured remotes;
it does not silently change scope or architecture. Legacy GnuPG stages persistent
keyring files privately, skips sockets/devices/locks and refuses a nonempty target
`.gnupg`; it does not automatically stop agents or merge existing keyrings.
Close GnuPG clients and stop their agents manually before copying a keyring in
either workflow. Custom `GNUPGHOME` locations need separate migration.
The legacy workflow remains **unverified on live Fedora XFCE 43/44**. Do not assume
the Python workflow's validation, preview or recovery behavior applies to it.

For that legacy workflow, inspect the script and install its required tools
(`rsync`, `sshpass`, `xdg-user-dir`, `bc` and feature-specific commands), then run:

```bash
bash migration.sh
```

It requires Bash; do not invoke it with `sh`.

## Development checks

```bash
python3 -m unittest discover -s tests -v
bash -n migration.sh
```

Passing automated checks does not replace the [live rehearsal](docs/FEDORA_43_TO_44.md#two-vm-rehearsal).
GitHub Actions runs this suite in Fedora 43 and 44 containers and exercises the
real DNF5 inventory commands and an empty Flatpak installation in each container.
