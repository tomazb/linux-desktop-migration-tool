# Fedora XFCE 43 → 44 migration guide

Use `fedora-migration.py` to move selected files to a **fresh Fedora 44** desktop.
Use DNF5 system upgrade for an **existing Fedora 43** installation. Neither path
removes the need for an independently verified backup. Fedora 44 was released on
[April 28, 2026](https://fedoramagazine.org/announcing-fedora-linux-44/).

## Requirements and release pairs

- Backup: the source Fedora system, release 43 or newer.
- Restore and rollback: the destination Fedora system, release 43 or newer, with a
  bundle made on a Fedora release that is the same or up to two releases older.
- All commands run as the normal desktop user, not root.

### Other release pairs

Fedora 43 → 44 is the documented path and the only pair the project tests. Other
pairs inside the supported range (the same release, or an upgrade of one or two
releases such as 44 → 45 or 43 → 45) are allowed, but the restore prints
`Fedora S → D has not been tested with this tool; review the preview carefully.`
Read the preview, panel-plugin and autostart checks with extra care and rehearse
first. Downgrades (a newer backup on an older release), jumps of more than two
releases, non-Fedora sources and non-numeric `VERSION_ID` values are refused. The
package commands preview names the actual destination release.

Bundles record a manifest `schema_version`. This tool writes schema 2 and still
reads schema 1 bundles made by earlier versions unchanged. A bundle with a higher
schema was made by a newer version of this tool and is refused until you update
the tool. The recorded `tool_version` is informational and never compared.

## Prepare a clean-install migration

1. Identify additional data outside the defaults: projects, application settings,
   external mounts, service databases and containers. Unrelated mounts, databases
   and containers are not automatically selected. Selected trees do traverse any
   mounted subtrees within them. Add home-relative paths with `--include`.
2. Inventory third-party repositories, panel plugins, themes and fonts. Obtain
   compatible Fedora 44 packages from trusted publishers.
3. Close applications, save work and log out of XFCE. Use a console TTY with
   the source user's XFCE session and `xfconfd` stopped. The tool checks this
   condition, refuses while Firefox, Thunderbird or Flatpak apps run (and key
   agents when secrets are selected), and never kills a process or logs a user out.
4. Write the bundle outside the source home. Prefer a Linux filesystem on an
   encrypted external drive. The command creates a new private directory and
   reports logical payload bytes; allow extra space for filesystem overhead/recovery.

Nondefault `XDG_CONFIG_HOME`/`XDG_DATA_HOME` layouts are rejected on backup and
restore. Migrate them separately; do not unset these values merely to bypass the
check. Unsupported escaped assignments in `user-dirs.dirs` are rejected explicitly
rather than interpreted as shell code or silently replaced with default folders.
A symlinked or non-regular `user-dirs.dirs` is also rejected: replace it with a
regular file, or use `--settings-only` and select folders with `--include`.

```bash
python3 fedora-migration.py backup --output /mnt/backup/fedora43-backup \
  --include Projects --include .config/my-app
python3 fedora-migration.py verify --bundle /mnt/backup/fedora43-backup
```

Use `--settings-only` to omit the default personal/application-data selection.
Use `--exclude PATTERN` to leave out home-relative paths or globs; each `*` matches
one path segment and exclusions win over includes. The monitor layout
(`displays.xml`) and `.var/app/*/cache` are excluded by default because a new
machine's outputs differ; `--no-default-excludes` keeps them. Every exclusion is
reported as a warning in the summary and the restore preview, counting each
excluded tree once; a pattern of yours that matched nothing is reported too.
Supported secrets such as `.ssh`, `.gnupg`, `.pki`, keyrings and `.config/goa-1.0` require
`--include-secrets` or an explicit `--include` of that secret root or a path below it.
Broad parent includes do not implicitly select these known secret trees.
Browser, mail and Flatpak profiles can contain credentials
independently of that option. Protect all copies of the bundle.
The complete selected `.gnupg` tree is copied, rather than a public-key-only export.
GOA configuration accompanies keyring data; account reauthorization may be needed.
Personal backgrounds under `.local/share/backgrounds` are included by default.

The bundle is a directory, not an archive; wait for successful completion and its
manifest before transferring it. Transfer every file, including the manifest:

```bash
rsync -a /mnt/backup/fedora43-backup/ /target/mount/fedora43-backup/
python3 fedora-migration.py verify --bundle /target/mount/fedora43-backup
```

Do not add `--delete`. Check free space and the actual mount point first. FAT/NTFS
and some network filesystems can lose modes or symlinks; verification on the
transferred copy is essential. Verify checks payload bytes, tree and symlink targets;
restore applies modes/times from the manifest. Verify does not certify actual
backup-copy permissions. Manifest mtime values must fit signed 64-bit nanoseconds.
Checksums detect damage, not replacement of both payload and manifest by an attacker.
Keep the source until a live restore is checked.

## Restore into Fedora XFCE 44

Create the destination account and install required XFCE plugins/themes. Using
an identical username/home path avoids some absolute-path repairs; matching UIDs
are not required by this normal-user copy workflow. Log out of XFCE, close target
apps and log into a console TTY as the destination user. Do not run the CLI as root.

```bash
python3 fedora-migration.py restore --bundle /mnt/backup/fedora43-backup
python3 fedora-migration.py restore --bundle /mnt/backup/fedora43-backup \
  --apply --rollback-dir /mnt/backup/fedora44-before-restore
```

The first command previews. `--apply` enables copying. Complete verification and
destination validation happen before changes. Unrelated destination files remain;
selected files may be replaced. A private rollback directory retains overwritten
files under `previous/` with a journal. The journal is a complete intent plan
(`kind: intent_plan`) written before changes, rather than a per-file completion log.
Directory/type conflicts and symlink
ancestors are rejected. Use a new rollback directory for each apply.

Preview and apply print "Destination checks" before anything changes: how many
existing files or symlinks the restore replaces and how many new ones it creates,
which bundle files still contain the old home path (when the home paths differ),
and the panel plugin and autostart findings described under
[XFCE settings and application checks](#xfce-settings-and-application-checks).
They are informational and never block the restore.

When the destination home differs from the source's, add `--rewrite-home` to
`--apply` to replace the old home path with the new one:

```bash
python3 fedora-migration.py restore --bundle /mnt/backup/fedora43-backup \
  --apply --rewrite-home
```

The preview with the same flag lists what would be rewritten and still writes
nothing. A reference is the old home path followed by `/`, the end of the file or a
character other than letters, digits, `.`, `_` and `-`; `/home/alice` is therefore
not matched inside `/home/alice2`, while `file:///home/alice/x` is. Only files of
at most 1 MiB are considered: `*.xml` under `.config/xfce4/`, `*.desktop`
launchers under `.config/`, `.local/share/applications/` or directly in
`Desktop/` (elsewhere they may be project files), `.gtkrc-2.0`, `.config/gtk-3.0/bookmarks`, `.config/gtk-4.0/bookmarks`,
`.config/user-dirs.dirs`, `.config/mimeapps.list` and text files in
`.config/Thunar/`. Anything under `.mozilla`, `.thunderbird` or `.var`, and every
other path, is neither scanned nor modified. Each file is rewritten only after its
SHA-256 has been validated against the manifest; the new content is written to a
private temporary file, synced, given the manifest mode and mtime and then moved
into place. Replaced originals remain in the rollback directory. Without the flag
no file content changes.

Literal replacement is safe across these formats only for a conservative set of
home names: when source and destination differ, both paths must contain only
ASCII letters, digits, `/`, `.`, `_` and `-`. Whitespace, quotes, `&`, shell
metacharacters, percent signs and non-ASCII characters require restoring without
`--rewrite-home` and editing the paths manually with the appropriate escaping.
Preview and apply reject unsupported rewrites before package installation or
destination writes. A same-home rewrite is a no-op, and ordinary restore accepts
these home names.

Regular-file modes and mtime and symlinks are preserved. Ownership, ACLs, xattrs
and source security labels are not. Symlinks remain links: their external targets
are not backed up. Review links to old disks or paths after migration.

When SELinux is enabled, the restore applies target contexts with `restorecon`.
For a manual repair or recovery, relabel the specific restored subtree as needed:

```bash
sudo restorecon -R -v /home/USER/restored-subtree
```

Replace the example with the real target path. Keep SELinux enabled and use target
policy rather than copying source labels. Red Hat documents recursive relabeling
in its [SELinux troubleshooting guide](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/8/html/using_selinux/troubleshooting-problems-related-to-selinux_using-selinux).

## XFCE settings and application checks

[Xfconf documentation](https://docs.xfce.org/xfce/xfconf/start) says its XML files
should be edited only while Xfconf is stopped. Restore outside the running desktop
so cached settings cannot interfere. Install panel plugins before the first login.
An optional diagnostic listing on the running source desktop is:

```bash
xfconf-query -l
xfconf-query -c xfce4-panel -l -v
```

These [inspection commands](https://docs.xfce.org/xfce/xfconf/xfconf-query) are not
a lossless import format. The file backup preserves selected configuration.
Do not migrate `~/.cache/sessions`: XFCE stores session state there, and a restored
session starts applications in addition to autostart entries. See
[session file locations](https://docs.xfce.org/xfce/xfce4-session/advanced) and
[Session and Startup preferences](https://docs.xfce.org/xfce/xfce4-session/preferences).

After login, inspect panel launchers/plugins, keyboard shortcuts, MIME defaults,
themes, fonts and wallpaper paths. Changed monitor names/layouts or missing plugins
can require manual adjustment. Different usernames/home paths can leave absolute
references in launchers and app settings. Verify browser profiles/bookmarks,
Thunderbird accounts/local mail and important Flatpak apps before deleting 43.
Closed applications are required; the tool does not provide consistent snapshots
of live application databases.

The restore preview automates part of this check without changing anything:

- **Panel plugins.** Plugin types in the bundled `xfce4-panel.xml` (the `value` of
  each `plugin-N` property under `plugins`) are looked up as `TYPE.desktop` in
  `/usr/share/xfce4/panel/plugins`, `/usr/local/share/xfce4/panel/plugins` and
  `~/.local/share/xfce4/panel/plugins` of the destination user. Missing types are
  listed; install the packages that provide them before the first login, for
  example by searching with `dnf provides '*/xfce4/panel/plugins/TYPE.desktop'`.
  A panel file that is too large or is not plain XML without entity declarations
  is reported as a warning rather than parsed.
- **Autostart programs.** For each `.config/autostart/*.desktop` entry that is not
  `Hidden=true`, the program is `TryExec` if present, otherwise the first word of
  `Exec` after any leading `env` (with its options) and `NAME=value` words. Absolute
  paths, `PATH` candidates and symlink targets are checked against the final
  restore overlay: a bundled replacement takes precedence over an existing file,
  and a bundled executable can satisfy a command before it exists here. Restored
  files require owner execute permission. `--rewrite-home` is taken into account
  when selected. Missing programs are listed per entry. An `Exec` that cannot be
  split or checked conservatively is reported as unparseable; this includes
  `env -S` / `--split-string`, unsupported options and missing option arguments.
  Those entries need manual inspection. The entries are only read, never run.
- **Old home paths.** See `--rewrite-home` under
  [Restore into Fedora XFCE 44](#restore-into-fedora-xfce-44).

## RPM and Flatpak installation

Package inventory is captured by default; installation is separately opt-in:

```bash
python3 fedora-migration.py restore --bundle /mnt/backup/fedora43-backup \
  --apply --install-packages
```

DNF5/Flatpak retain their native confirmations. Source and target architectures
must match for package installation. `--skip-rpm` and `--skip-flatpak` can omit a
package path; backup also accepts these switches to omit its inventory.

RPM manifest data includes `rpm.requests` as `name.arch`, and `rpm.inventory` with
installation reason, source repository and full NEVRA. Fedora 44 resolves current
versions; the tool does not reinstall exact Fedora 43 builds. Equivalent manual
export syntax from [DNF5 repoquery](https://dnf5.readthedocs.io/en/latest/commands/repoquery.8.html):

```bash
dnf5 repoquery --userinstalled --queryformat '%{name}.%{arch}\n'
dnf5 repoquery --installed \
  --queryformat '%{name}.%{arch} %{reason} %{from_repo} %{full_nevra}\n'
```

`--userinstalled` selects installed packages on its own; DNF5 rejects combining
it with `--installed`. It also includes group/module and unknown reasons. It is not an
exact list of applications deliberately chosen by the user. Configured DNF
excludes can affect both requests and inventory. Review missing,
renamed or local RPM requests. Configure Fedora-44-compatible trusted repositories
yourself. [DNF5 install](https://dnf5.readthedocs.io/en/latest/commands/install.8.html)
resolves package requests and dependencies; automatic erasures/skips are not enabled.

Flatpak manifests retain each app's scope, full ref and origin, and remote
scope/name/URL. Full refs retain branch and architecture. Equivalent inventories:

```bash
flatpak list --user --app --columns=ref,origin
flatpak list --system --app --columns=ref,origin
flatpak remotes --user --show-disabled --columns=name,url
flatpak remotes --system --show-disabled --columns=name,url
```

Configure matching trusted remotes yourself. The importer validates remote URLs;
it does not add remotes or silently substitute another origin. Install commands
use `flatpak install --user ORIGIN REF` or `--system` for the recorded scope.
Custom named installations are unsupported: use `--skip-flatpak` and handle them
manually. App data in `.var/app` is separate from installation. See the
[upstream Flatpak reference](https://docs.flatpak.org/en/latest/flatpak-command-reference.html).
Package-manager failures are reported; file restoration and package installation
are not one atomic transaction. Review output before retrying.

## Recover overwritten files

Close applications and log out of XFCE again. The `rollback` command undoes a
restore from the rollback directory it reported; it previews by default:

```bash
python3 fedora-migration.py rollback --rollback-dir /mnt/backup/fedora44-before-restore
python3 fedora-migration.py rollback --rollback-dir /mnt/backup/fedora44-before-restore --apply
```

It works for any journal status, including a restore that `failed` or was
interrupted. Saved originals are put back over the restored files, and files or
symlinks the restore added are removed, but only where the path still holds what
the restore wrote. The bundle is verified in full for that comparison (pass
`--bundle PATH` if it moved); files changed by `--rewrite-home` count as restored.
Historical home-rewrite pairs containing spaces, Unicode or punctuation remain
usable for comparing already-written files. The stricter home-name requirements
for new `--rewrite-home` restores still apply to new writes.
Changed files are a CONFLICT and are left alone; if the bundle cannot be verified,
every existing path is a conflict. Review each reason, then add `--include-changed`
to process them anyway. Directories the restore created are removed only when
empty. New restore journals record the original mode and timestamp of existing
selected directory roots before any changes, and rollback restores that metadata
after its file actions. Partial or interrupted rollbacks can be continued without
losing this information. Directory metadata is restored only when it still matches
the verified bundle or the saved original; changes are conflicts left alone unless
`--include-changed` is supplied. An unavailable bundle cannot authorize changed
metadata. An interrupted attempt's own directory-entry changes may make a timestamp
unverifiable too; inspect the conflict before opting in on retry. Older journals
are still accepted, with a warning in the
preview, result and rollback journal that the original directory metadata is
unavailable and must be recovered by hand.

Before any overwrite or removal the current version is moved to
`ROLLBACK/undone-<UTC time>/`, so nothing is lost, and progress is written to
`ROLLBACK/rollback-journal.json`. A finished rollback is refused a second time; one
that left conflicts (`partial`) or failed can be run again. Package installs are not
undone. With SELinux enabled the put-back paths are relabeled.

Across filesystems, rollback moves the original inode into a private
`.fedora-migration-quarantine-*/entry` directory beside the former path, then makes
a durable copy under `undone-*`. Before moving anything, it atomically writes and
syncs a small individual recovery record under
`ROLLBACK/quarantine-records/<identifier>.json`. The original inode remains there
after success so that writes through
an already-open descriptor remain recoverable, including writes after rollback
finishes. These individual records are durable recovery evidence after an abrupt
interruption: the summary `rollback-journal.json` may not yet list the latest
captures. Keep `quarantine-records/` with the rollback directory. Each record is an
intent reference, so its entry may be absent if interruption happened before the
move or just after a successful put-back. Retries validate and merge the records
with older `retained_quarantines` references from the summary, and normal
completion or failure consolidates the locations into the summary. The command
prints the locations. A failed put-back removes a quarantine and its individual
record only after returning that same
inode to its former path without replacing anything. Otherwise, keep these
directories until all writers have stopped, compare the retained originals with
the `undone-*` copies, and save any newer data before removing a quarantine by
hand. Restore-created parent directories containing retained originals are kept.

Rollback temporarily grants owner access to managed read-only directories through
directory descriptors. On failure it resets temporary modes; on success existing
selected roots receive their original metadata. If the owner cannot read or
traverse a directory at all (for example mode `000`), rollback fails closed with
an access diagnostic: repair owner access by hand before retrying. It does not
follow a symlink to make that repair.

Threat model: rollback assumes the home is quiescent, which the command checks
(no XFCE session, no browser, mail or Flatpak applications). It never overwrites or
deletes an entry it has not first captured, and it publishes put-back
files with a hard link that fails rather than replaces, so a concurrent writer can at
most make one path fail with both versions kept; the error names both locations. The
home and its ancestors are walked from `/` without following symlinks, so swapping a
parent directory mid-run cannot redirect a write. Because of the hard-link step, a
filesystem without hard links makes rollback stop with status `failed` at the first
path it must put back; later paths are not processed. Restore the remaining put-back
paths by hand from `previous/`, or retry only on a filesystem that supports hard links.

### Manual recovery (fallback)

Read the journal in the rollback directory and identify the exact paths to recover.
Status is `complete`, `failed` or `interrupted` after an attempt (`in_progress` if
abruptly terminated). The intent plan lists potentially touched paths, not proof
every listed write finished; inspect `previous/` for actual saved originals. A
recovery copy is assembled under `staging/` and moved into `previous/` only once
complete, so `previous/` never holds a partial original; after an abrupt
termination, ignore anything left in `staging/`. Ctrl+C returns 130 and reports the
retained rollback directory. Keep a copy of the current target versions if you need
to compare them. Copy only the chosen corresponding entries from `ROLLBACK/previous/`
back beneath your home, as the destination desktop user, preserving symlinks/modes
with `cp -a` or `rsync -a`. Inspect the directory layout before choosing
source/destination paths; do not merge the whole rollback tree blindly and do not
use `--delete`.

Newly created files then need separate review. Check ownership after any
administrator-assisted recovery and relabel recovered paths with target `restorecon`
when SELinux is enabled. Then log in and recheck apps.

## In-place upgrade is a separate operation

Back up and verify first. On the existing Fedora 43 installation:

```bash
sudo dnf5 upgrade --refresh
sudo reboot
```

After reboot, download/review the Fedora 44 transaction and apply it offline:

```bash
sudo dnf5 system-upgrade download --releasever=44
sudo dnf5 offline status
sudo dnf5 offline reboot
```

DNF5 includes system-upgrade; the DNF4 plugin is not an unconditional prerequisite.
Review dependency problems and proposed removals before considering `--allowerasing`.
The tool never runs these commands or reboots for you. Do not run migration restore
automatically after an in-place upgrade. For diagnostics use
`sudo dnf5 offline log --number=-1`; `sudo dnf5 offline clean` cancels a stored
transaction and removes cached packages. See
[DNF5 system upgrade](https://dnf5.readthedocs.io/en/latest/commands/system-upgrade.8.html),
[offline transactions](https://dnf5.readthedocs.io/en/latest/commands/offline.8.html) and
[Fedora's upgrade guide](https://docs.fedoraproject.org/en-US/quick-docs/upgrading-fedora-offline/).

## Two-VM rehearsal

Live validation is pending. Use disposable Fedora XFCE 43 and 44 VMs with separate
normal users and a shared Linux-filesystem backup disk; record exact OS/package versions.

1. On 43, customize panel/shortcuts/theme and create sample XDG files, a hidden
   extra directory, an executable, an internal symlink and a link outside selection.
   Install an additional RPM and user/system Flatpak apps from known remotes.
2. Close apps, log out and back up from TTY. Verify the bundle. Confirm default
   secrets are absent, then test a separate opt-in secrets bundle with disposable keys.
3. Copy the whole bundle and verify again. Corrupt a copy of a payload file and
   confirm verify/restore reject it. Keep the original bundle intact.
4. On 44, create a conflicting file and an unrelated file, stop XFCE and preview.
   Confirm preview changes neither files nor installed packages.
5. Apply without package installation. Check content, modes, mtime, symlinks,
   unrelated-file retention, overwritten-file recovery and SELinux labels.
6. Configure matching remotes/repositories, then test opt-in package installation.
   Confirm confirmations and origin/scope/branch preservation. Test a changed remote
   URL and unavailable RPM; errors must be visible rather than silently skipped.
7. Log in and inspect the desktop and key apps. Repeat with a different home path
   and monitor layout. Record manual repairs, errors and package-manager output.

Run `python3 -m unittest discover -s tests -v` and `bash -n migration.sh` separately.
Passing those checks does not establish a successful live Fedora migration.

The legacy script also incorporates selected newer upstream behavior; see
[the Codeberg comparison](UPSTREAM_CODEBERG.md). Its live migration validation
remains separate from these Python checks and is still pending.
