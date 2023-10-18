# linux-desktop-migration-tool

Linux Desktop Migration Tool aims to make migration from one Linux desktop machine to another as easy as possible.

## What It Can do
- Migrate data in XDG directories (Documents, Pictures, Downloads...) and other arbitrary directories in home.
- Reinstall flatpaks on the new machine.
- Migrate Flatpak app data.
- Migrate Toolbx containers.

## Prerequisites
- It is aimed for and tested on Fedora Silverblue, but it should work on any modern desktop distribution.
- Both computers are expected to be on the same network.
- The destination computer is expected to be freshly installed with the user set up.
- Both users should have the same UID, names could be different.
- rsync, sshpass, xdg-user-dirs, gawk are installed.

## Planned features
- Migration of ssh certificates, nss database.
- Migration of GNOME desktop settings.

## How to Install and Run
- just download the migration.sh file, open the terminal app and run 'sh migration.sh' command.

## FAQ

### Why not to just copy over the entire home directory/partition?
A new computer is always a fresh start from me, so I never copy over everything, but use it as an opportunity to leave unnecessary files behind. That is why the script gives more control over what is migrated.

### Why is it not a desktop app?
I'd love it to be a desktop app, but I also would like it to be a nice app - polished, written in a modern toolkit... And I don't have time and skills for that. A shell script is great to quickly prototype and it's also easy to build your own solution on.
