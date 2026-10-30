"""Platform checks and explicit user-facing backup/restore commands."""

import argparse
from collections import Counter
import getpass
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

from . import __version__, bundle, packages


class MigrationError(RuntimeError):
    """An actionable failure that should not be displayed as a traceback."""


def read_os_release(path=Path("/etc/os-release")):
    """Read os-release assignments without evaluating shell substitutions."""
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        if not separator:
            continue
        parts = shlex.split(raw, comments=True, posix=True)
        if len(parts) > 1:
            raise MigrationError(f"Invalid assignment in {path}: {key}")
        values[key] = parts[0] if parts else ""
    return values


def platform_info():
    release = read_os_release()
    return {
        "id": release.get("ID", ""),
        "version_id": release.get("VERSION_ID", ""),
        "pretty_name": release.get("PRETTY_NAME", "unknown operating system"),
        "architecture": platform.machine(),
        "username": getpass.getuser(),
        "uid": os.getuid(),
        "home": str(Path.home().resolve()),
    }


def require_platform(info, version):
    if info["uid"] == 0:
        raise MigrationError(
            "Run this command as your normal desktop user, without sudo. "
            "Package installation requests sudo separately when needed."
        )
    if info["id"] != "fedora" or info["version_id"] != version:
        raise MigrationError(
            f"This command requires Fedora {version}; found "
            f"{info.get('pretty_name', info['id'])} (release {info['version_id']}). "
            "The verify command can run on other systems."
        )
    home = Path(info["home"])
    if not home.is_dir() or home == Path("/"):
        raise MigrationError("The current user's home must be an existing directory other than /.")


def assert_xfce_stopped():
    if not shutil.which("pgrep"):
        raise MigrationError("Install procps-ng so the tool can check whether XFCE is running.")
    result = subprocess.run(
        ["pgrep", "-u", str(os.getuid()), "-x", "xfce4-session|xfconfd"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        raise MigrationError(
            "Log out of XFCE and run this command from a text console (Ctrl+Alt+F3) "
            "or SSH as the same user. xfce4-session or xfconfd is still running; "
            "its in-memory settings can conflict with copied XML files."
        )
    if result.returncode != 1:
        raise MigrationError(f"Could not check the XFCE session: {result.stderr.strip()}")


# Process names as pgrep matches them (the kernel truncates names to 15 bytes).
APPLICATIONS = ("firefox", "firefox-bin", "thunderbird")
SECRET_AGENTS = ("gpg-agent", "ssh-agent", "gnome-keyring-d")
FULL_NAMES = {"gnome-keyring-d": "gnome-keyring-daemon"}


def assert_applications_closed(*, secrets):
    """Refuse while applications can change the profiles being copied.

    Secret agents are checked only when keyrings or keys are part of the
    operation. The tool never stops processes itself.
    """
    names = APPLICATIONS + (SECRET_AGENTS if secrets else ())
    result = subprocess.run(
        ["pgrep", "-u", str(os.getuid()), "-x", "-l", "|".join(names)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode not in (0, 1):
        raise MigrationError(f"Could not check running applications: {result.stderr.strip()}")
    processes = sorted({line.split(maxsplit=1)[1] for line in result.stdout.splitlines()
                        if len(line.split(maxsplit=1)) == 2})
    flatpaks = []
    if shutil.which("flatpak"):
        listed = subprocess.run(["flatpak", "ps", "--columns=application"],
                                capture_output=True, text=True, check=False)
        if listed.returncode:
            raise MigrationError(
                f"Could not check running Flatpak applications: {listed.stderr.strip()}")
        flatpaks = sorted({line.strip() for line in listed.stdout.splitlines() if line.strip()})
    if not processes and not flatpaks:
        return
    agents = [name for name in processes if name in SECRET_AGENTS]
    shown = [FULL_NAMES.get(name, name) for name in processes] + flatpaks
    advice = []
    if set(processes) - set(agents):
        advice.append("close the browser or mail client")
    if flatpaks:
        advice.append("stop Flatpak apps with `flatpak kill APP`")
    if agents:
        advice.append("stop key agents with `gpgconf --kill all` and end ssh-agent or "
                      "gnome-keyring-daemon for this user")
    raise MigrationError(
        "Applications that use the selected data are still running: "
        + ", ".join(shown) + ". Please " + "; ".join(advice) + ", then retry."
    )


def check_selinux_tools():
    """Return whether relabeling is required; check dependencies before writes."""
    detector = shutil.which("selinuxenabled")
    if detector:
        result = subprocess.run([detector], check=False)
        if result.returncode not in (0, 1):
            raise MigrationError("selinuxenabled could not determine SELinux status.")
        enabled = result.returncode == 0
    else:
        enabled = Path("/sys/fs/selinux/enforce").exists()
    if enabled and not shutil.which("restorecon"):
        raise MigrationError("SELinux is enabled; install policycoreutils before restoring files.")
    return enabled


def relabel_paths(home, selected_paths):
    paths = [str(home / relative) for relative in selected_paths]
    if not paths:
        return
    command = ["restorecon", "-R", "--", *paths]
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise MigrationError(
            "File copying completed, but SELinux relabeling failed. Keep the rollback "
            "directory and run this command before starting the desktop:\n"
            + shlex.join(["sudo", *command])
        )


def validate_manifest_metadata(manifest):
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise MigrationError("The manifest has no valid source-system metadata.")
    for key in ("id", "version_id", "home", "username", "architecture"):
        value = source.get(key)
        if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
            raise MigrationError(f"Invalid source metadata: {key}")
    if type(source.get("uid")) is not int or source["uid"] < 0:
        raise MigrationError("Invalid source metadata: uid")
    if not Path(source["home"]).is_absolute() or ".." in Path(source["home"]).parts:
        raise MigrationError("Invalid source metadata: home must be an absolute path.")
    packages.validate_package_manifest(manifest.get("packages"))


def print_warnings(warnings):
    for warning in warnings:
        print(f"  Note: {warning}")


def print_manifest_summary(manifest):
    counts = Counter(entry["type"] for entry in manifest["entries"])
    total_bytes = sum(entry["size"] for entry in manifest["entries"]
                      if entry["type"] == "file")
    native = manifest["packages"]["rpm"]
    flatpak = manifest["packages"]["flatpak"]
    print(f"Source: Fedora {manifest['source']['version_id']} / {manifest['source']['architecture']}")
    print(f"Files: {counts['file']}; symlinks: {counts['symlink']}; directories: {counts['directory']}")
    print(f"File content: {total_bytes:,} bytes ({total_bytes / 1024**3:.2f} GiB); allow extra space for recovery copies.")
    print(f"RPM requests: {len(native['requests'])} ({native['status']})")
    print(f"Flatpak apps: {len(flatpak['apps'])} ({flatpak['status']})")
    print_warnings(manifest.get("warnings", []))
    print_warnings(manifest["packages"].get("warnings", []))


def backup_command(args):
    info = platform_info()
    require_platform(info, "43")
    assert_xfce_stopped()
    assert_applications_closed(
        secrets=bundle.selects_secrets(args.include, args.include_secrets))
    output = args.output.expanduser().absolute()
    home = Path(info["home"])
    # Fail early before collecting inventories for an unusable backup destination.
    resolved_output = output.resolve()
    if resolved_output == home or home in resolved_output.parents:
        raise MigrationError("Choose a new backup directory outside your home, preferably on external storage.")
    if output.exists() or output.is_symlink():
        raise MigrationError(f"Backup destination already exists: {output}")
    print("Collecting Fedora package inventories...", flush=True)
    inventory = packages.collect_packages(skip_rpm=args.skip_rpm, skip_flatpak=args.skip_flatpak)
    packages.validate_package_manifest(inventory)
    print("Copying selected files and computing SHA-256 checksums. Large profiles can take time.", flush=True)
    manifest = bundle.create_bundle(
        home, output, info, inventory, include=args.include,
        include_secrets=args.include_secrets, settings_only=args.settings_only,
        exclude=args.exclude, default_excludes=not args.no_default_excludes,
    )
    print(f"Backup created: {output}")
    print_manifest_summary(manifest)
    print("Copy the entire bundle to storage that survives reinstalling, then run verify on that copy.")
    print("The bundle is not encrypted; protect it as you would the original files.")
    return 0


def verify_command(args):
    manifest = bundle.verify_bundle(args.bundle.expanduser())
    validate_manifest_metadata(manifest)
    print("Bundle structure and recorded file checksums verified.")
    print("This verifies integrity, not the identity of whoever created the bundle.")
    print_manifest_summary(manifest)
    return 0


def restore_command(args):
    info = platform_info()
    require_platform(info, "44")
    source_path = args.bundle.expanduser()
    manifest = bundle.verify_bundle(source_path)
    validate_manifest_metadata(manifest)
    source = manifest["source"]
    if source["id"] != "fedora" or source["version_id"] != "43":
        raise MigrationError("This workflow expects a backup made on Fedora 43.")
    if source["architecture"] != info["architecture"]:
        if args.install_packages:
            raise MigrationError(
                "Source and destination architecture differ; RPM/Flatpak requests retain "
                "their source architecture. Restore files without --install-packages and "
                "select packages for the new architecture manually."
            )
        print("Note: architectures differ. Review copied local executables and application data.")
    home = Path(info["home"])
    rollback = args.rollback_dir.expanduser() if args.rollback_dir else None
    # Validate the destination before either package transactions or file writes.
    # Plan from the manifest verified above; --apply verifies the bundle again
    # immediately before writing, after any package transactions.
    plan = bundle.plan_restore(source_path, home, manifest, backup_dir=rollback)
    print("Restore preview:" if not args.apply else "Validated restore plan:")
    print_manifest_summary(manifest)
    print(f"Destination: {home}")
    print("Selected paths:")
    for relative in plan["selected_paths"]:
        print(f"  {relative}")
    if source["home"] != str(home):
        print(f"Note: source home was {source['home']!r}. Review absolute paths in launchers and XFCE settings.")
    commands = []
    if args.install_packages:
        commands = packages.package_commands(
            manifest["packages"], skip_rpm=args.skip_rpm, skip_flatpak=args.skip_flatpak,
        )
        print("Package commands (repositories must already be configured for Fedora 44):")
        for command in commands:
            print("  " + shlex.join(command))
        if not commands:
            print("  No package requests selected.")
    else:
        print("Package installation is not selected; use --install-packages to include it.")
    if not args.apply:
        print("Preview only: no files, packages, logs, or rollback directories were changed.")
        print("Review the plan, then add --apply to perform this restore.")
        return 0
    secrets = bundle.manifest_has_secrets(manifest)
    assert_xfce_stopped()
    assert_applications_closed(secrets=secrets)
    selinux = check_selinux_tools()
    if args.install_packages:
        print("Installing requested packages with their normal confirmation prompts...", flush=True)
        packages.install_packages(
            manifest["packages"], skip_rpm=args.skip_rpm, skip_flatpak=args.skip_flatpak,
        )
    # Package operations can take time: recheck the session immediately before copying.
    assert_xfce_stopped()
    assert_applications_closed(secrets=secrets)
    print("Restoring selected files and retaining prior versions...", flush=True)
    result = bundle.restore_bundle(source_path, home, dry_run=False, backup_dir=rollback)
    if result["backup_dir"]:
        print(f"Rollback directory: {result['backup_dir']}", flush=True)
    if selinux:
        relabel_paths(home, result["selected_paths"])
    print(f"Restored {result['files']} files and {result['symlinks']} symlinks.")
    print_warnings(result.get("warnings", []))
    print("Log into XFCE and check your panel, shortcuts, themes, applications, and files.")
    print("Keep the source backup and rollback directory until you have checked the result.")
    return 0


def parser():
    result = argparse.ArgumentParser(
        description="Portable Fedora XFCE 43 → 44 backup and restore. Restore previews by default.",
    )
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = result.add_subparsers(dest="command", required=True)
    backup = subcommands.add_parser("backup", help="Create a new portable bundle on Fedora 43")
    backup.add_argument("--output", type=Path, required=True, help="New directory outside HOME, ideally on external storage")
    backup.add_argument("--include", action="append", default=[], metavar="RELATIVE_PATH", help="Additional home-relative file/directory (repeatable)")
    backup.add_argument("--exclude", action="append", default=[], metavar="PATTERN", help="Home-relative path or glob to leave out, e.g. 'Downloads/*.iso' (repeatable; * matches one path segment)")
    backup.add_argument("--no-default-excludes", action="store_true", help="Also back up the XFCE monitor layout (displays.xml) and Flatpak app caches")
    backup.add_argument("--include-secrets", action="store_true", help="Include SSH/GPG/PKI/keyrings/online accounts; the bundle remains unencrypted")
    backup.add_argument("--settings-only", action="store_true", help="Omit default personal folders and browser/Flatpak data")
    backup.add_argument("--skip-rpm", action="store_true", help="Explicitly omit the RPM inventory")
    backup.add_argument("--skip-flatpak", action="store_true", help="Explicitly omit the Flatpak inventory")
    backup.set_defaults(handler=backup_command)
    verify = subcommands.add_parser("verify", help="Check a complete bundle without changing the system")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.set_defaults(handler=verify_command)
    restore = subcommands.add_parser("restore", help="Preview or apply a backup on Fedora 44")
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--apply", action="store_true", help="Perform the restore; otherwise show a read-only preview")
    restore.add_argument("--install-packages", action="store_true", help="Also reinstall recorded RPMs and Flatpaks, retaining manager confirmations")
    restore.add_argument("--skip-rpm", action="store_true", help="Omit RPMs when installing packages")
    restore.add_argument("--skip-flatpak", action="store_true", help="Omit Flatpaks when installing packages")
    restore.add_argument("--rollback-dir", type=Path, help="New directory for overwritten files and restore journal")
    restore.set_defaults(handler=restore_command)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except bundle.BundleInterrupted as error:
        print(str(error), file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Keep any reported rollback directory and inspect it before retrying.", file=sys.stderr)
        return 130
