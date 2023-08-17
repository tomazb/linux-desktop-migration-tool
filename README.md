# linux-desktop-migration-tool

Linux Desktop Migration Tool aims to make migration from one Linux desktop machine to another as easily as possible.

## Prerequisites
- It is aimed for and tested on Fedora Silverblue, but it should work on any modern desktop distribution.
- Both computers are expected to be on the same network.
- The destination computer is expected to be freshly installed with the user set up.
- Both users should have the same UID, names could be different.
- rsync, sshpass, xdg-user-dirs are installed.

## Planned features
- Migration of Toolbx containers.
- Migration of arbitrary directories in home.
- Migration of ssh certificates, nss database.
- Migration of GNOME desktop settings.
