# linux-desktop-migration-tool

Linux Desktop Migration Tool aims to make migration from one Linux desktop machine to another as easy as possible.

## What It Can do
- Migrate data in XDG directories (Documents, Pictures, Downloads...) and other arbitrary directories in home.
- Reinstall flatpaks on the new machine.
- Migrate Flatpak app data.
- Migrate Toolbx containers.
- Migrate ssh certificates, PKI certificates and nss database, GPG keys, keyring, GNOME Online Accounts.
- Migrate GNOME desktop/app settings.
- Migrate network settings (wired, wi-fi, VPNs..., only NetworkManager supported).

## Prerequisites
- It is aimed for and tested on Fedora Silverblue, but it should work on any modern desktop distribution.
- Both computers are expected to be on the same network.
- The destination computer is expected to be freshly installed with the user set up.
- Both users should have the same UID, names could be different.
- rsync, sshpass, xdg-user-dirs, gawk, gpg are installed.

## How to Install and Run
- just download the migration.sh file, open the terminal app and run 'sh migration.sh' command.

## Post-migration Steps
- Log out and log in for all changes to take effect.
- You will be prompted to unlock the keyring with the password from the origin computer for the first time.

## FAQ

### Why not to just copy over the entire home directory/partition?
A new computer is always a fresh start from me, so I never copy over everything, but use it as an opportunity to leave unnecessary files behind. That is why the script gives more control over what is migrated.

### Why is it not a desktop app?
I'd love it to be a desktop app, but I also would like it to be a nice app - polished, written in a modern toolkit... And I don't have time and skills for that. A shell script is great to quickly prototype and it's also easy to build your own solution on.

### What desktop environments does it support?
I primarily target GNOME since it's the desktop environment in Silverblue, but most operations are desktop environment agnostic if there is anything GNOME specific, it will be skipped if not available. I'm open to contributions that will add support for other desktop environments, but I will not work on it myself since I have very little experience with them.

### Is there going to be support for snap or appimage applications?
I'm open to contributions, but I will not work on it myself since I don't use those formats myself and thus have little experience with them.
