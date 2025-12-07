# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
