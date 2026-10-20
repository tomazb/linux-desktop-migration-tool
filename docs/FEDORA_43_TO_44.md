# Fedora XFCE 43 → 44 migration guide

Use `fedora-migration.py` to move selected files to a **fresh Fedora 44** desktop.
Use DNF5 system upgrade for an **existing Fedora 43** installation. Neither path
removes the need for an independently verified backup. Fedora 44 was released on
[April 28, 2026](https://fedoramagazine.org/announcing-fedora-linux-44/).

## Prepare a clean-install migration

1. Identify additional data outside the defaults: projects, application settings,
   external mounts, service databases and containers. Unrelated mounts, databases
   and containers are not automatically selected. Selected trees do traverse any
   mounted subtrees within them. Add home-relative paths with `--include`.
2. Inventory third-party repositories, panel plugins, themes and fonts. Obtain
   compatible Fedora 44 packages from trusted publishers.
3. Close applications, save work and log out of XFCE. Use a console TTY with
   the source user's XFCE session and `xfconfd` stopped. The tool checks this
   condition and never kills a daemon or logs a user out.
4. Write the bundle outside the source home. Prefer a Linux filesystem on an
   encrypted external drive. The command creates a new private directory and
   reports logical payload bytes; allow extra space for filesystem overhead/recovery.

Nondefault `XDG_CONFIG_HOME`/`XDG_DATA_HOME` layouts are rejected on backup and
restore. Migrate them separately; do not unset these values merely to bypass the
check. Unsupported escaped assignments in `user-dirs.dirs` are rejected explicitly
rather than interpreted as shell code or silently replaced with default folders.

```bash
python3 fedora-migration.py backup --output /mnt/backup/fedora43-backup \
  --include Projects --include .config/my-app
python3 fedora-migration.py verify --bundle /mnt/backup/fedora43-backup
```

Use `--settings-only` to omit the default personal/application-data selection.
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

Close applications and log out of XFCE again. Read the journal in the reported
rollback directory and identify the exact paths to recover. Status is `complete`,
`failed` or `interrupted` after an attempt (`in_progress` if abruptly terminated).
The intent plan lists potentially touched paths, not proof every listed write
finished; inspect `previous/` for actual saved originals. Ctrl+C returns 130 and
reports the retained rollback directory. Keep a
copy of the current target versions if you need to compare them. Copy only the
chosen corresponding entries from `ROLLBACK/previous/` back beneath your home,
as the destination desktop user, preserving symlinks/modes with `cp -a` or
`rsync -a`. Inspect the directory layout before choosing source/destination paths;
do not merge the whole rollback tree blindly and do not use `--delete`.

No automatic rollback is supplied. Newly created files need separate review;
restoring overwritten versions does not remove new files or undo package installs.
Check ownership after any administrator-assisted recovery and relabel recovered
paths with target `restorecon` when SELinux is enabled. Then log in and recheck apps.

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
