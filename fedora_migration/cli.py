"""Platform checks and explicit user-facing backup/restore commands."""

import argparse
from collections import Counter
import getpass
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys

from . import __version__, bundle, packages, report, rollback


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


MIN_RELEASE = 43
TESTED_PAIR = (43, 44)
MAX_UPGRADE = 2  # releases; Fedora supports upgrading across at most two


def parse_release(version_id):
    """A Fedora VERSION_ID as an int, or None unless it is a plain decimal integer."""
    if isinstance(version_id, str) and re.fullmatch(r"[1-9][0-9]{0,3}", version_id):
        return int(version_id)
    return None


def platform_release(info):
    """The running Fedora release (>= MIN_RELEASE); refuse anything else."""
    release = parse_release(info.get("version_id")) if info.get("id") == "fedora" else None
    if release is None or release < MIN_RELEASE:
        raise MigrationError(
            f"This command requires Fedora {MIN_RELEASE} or newer; found "
            f"{info.get('pretty_name', info.get('id'))} (release {info.get('version_id')}). "
            "The verify command can run on other systems."
        )
    return release


def source_release(source):
    """The Fedora release a bundle was made on, from its source metadata."""
    release = parse_release(source.get("version_id")) if source.get("id") == "fedora" else None
    if release is None:
        raise MigrationError(
            "This workflow expects a backup made on Fedora with a numeric release; the "
            f"bundle's source is {source.get('id')} release {source.get('version_id')}.")
    return release


def restore_release_note(source, destination):
    """Check a source -> destination pair; return a note unless it is the tested pair.

    Restoring on the same release or up to MAX_UPGRADE releases later is allowed.
    """
    if destination < source:
        raise MigrationError(
            f"Cannot restore a Fedora {source} backup on Fedora {destination}: downgrades "
            f"are not supported. Restore on Fedora {source} to {source + MAX_UPGRADE}.")
    if destination > source + MAX_UPGRADE:
        raise MigrationError(
            f"Cannot restore a Fedora {source} backup on Fedora {destination}: that is more "
            f"than two releases apart, beyond Fedora's supported upgrade path. Restore on "
            f"Fedora {source} to {source + MAX_UPGRADE}, or make a new backup on a closer release.")
    if (source, destination) == TESTED_PAIR:
        return None
    return (f"Fedora {source} → {destination} has not been tested with this tool; "
            "review the preview carefully.")


def require_platform(info):
    """Refuse root, unsupported releases and an unusable home; return the release."""
    if info["uid"] == 0:
        raise MigrationError(
            "Run this command as your normal desktop user, without sudo. "
            "Package installation requests sudo separately when needed."
        )
    release = platform_release(info)
    home = Path(info["home"])
    if not home.is_dir() or home == Path("/"):
        raise MigrationError("The current user's home must be an existing directory other than /.")
    return release


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
    packages.validate_package_manifest(manifest.get("packages"), manifest["schema_version"])


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


MAX_LISTED = 25


def print_limited(lines):
    for line in lines[:MAX_LISTED]:
        print(line)
    if len(lines) > MAX_LISTED:
        print(f"    ... and {len(lines) - MAX_LISTED} more")


def _names(items):
    """A sorted, capped, comma-separated list of identifiers from bundle data."""
    shown = [report.clip(item, 60) for item in items[:MAX_LISTED]]
    more = f", ... and {len(items) - MAX_LISTED} more" if len(items) > MAX_LISTED else ""
    return ", ".join(shown) + more


def print_rpm_triage(data, triage, *, skip_unavailable):
    """Report how the recorded RPM requests map onto this system's repositories."""
    unavailable = triage["unavailable"]
    skipped = sum(len(specs) for specs in unavailable.values())
    print("RPM triage against this system's repositories (read-only queries):")
    print(f"  Already installed: {len(triage['installed'])} (not requested again)")
    print(f"  To install: {len(triage['installable'])}")
    print_limited([f"    {report.clip(spec)}" for spec in triage["installable"]])
    if triage["installable_from_local"]:
        print(f"  Note: {len(triage['installable_from_local'])} of these were installed from a "
              "local file or an unknown source on the old system; this system's repositories "
              "provide the same name now. Check that it is the same software:")
        print_limited([f"    {report.clip(spec)}" for spec in triage["installable_from_local"]])
    print(f"  Unavailable: {skipped}" + (f" in {len(unavailable)} source repositories" if unavailable else ""))
    for repo, specs in list(unavailable.items())[:MAX_LISTED]:
        print(f"    From repository {report.clip(repo)} ({len(specs)} package{'s' if len(specs) != 1 else ''}): "
              + report.clip(packages.unavailable_hint(data, repo), 400))
        print_limited([f"      {report.clip(spec)}" for spec in specs])
    if len(unavailable) > MAX_LISTED:
        print(f"    ... and {len(unavailable) - MAX_LISTED} more repositories")
    if skipped and skip_unavailable:
        print(f"  Skipped: these {skipped} unavailable packages are left out because "
              "--skip-unavailable was given.")
    elif skipped:
        print("  Apply will refuse to start while packages are unavailable; fix the repositories "
              "and retry, or add --skip-unavailable to install only what is available.")
    print(f"  Manual handling: {len(triage['manual'])} (installed from a local file or an unknown "
          "source; this tool never installs them)")
    print_limited([f"    {report.clip(item['full_nevra'])} (from {report.clip(item['from_repo'] or 'unknown')})"
                   for item in triage["manual"]])
    environments = [item["id"] for item in data["rpm"].get("environments", [])]
    groups = [item["id"] for item in data["rpm"].get("groups", [])]
    if environments or groups:
        print(f"  The source had environment(s): {_names(environments) or 'none'}; "
              f"groups: {_names(groups) or 'none'}. These are not installed as groups "
              "\u2014 only explicit packages are requested.")


def unavailable_refusal(data, triage):
    """The error shown when apply would start with unavailable RPM packages."""
    lines = ["Some recorded RPM packages are not available on this system, so nothing was "
             "installed or restored:"]
    for repo, specs in list(triage["unavailable"].items())[:MAX_LISTED]:
        lines.append(f"  repository {report.clip(repo)}: {len(specs)} package"
                     f"{'s' if len(specs) != 1 else ''}; {report.clip(packages.unavailable_hint(data, repo), 400)}")
    if len(triage["unavailable"]) > MAX_LISTED:
        lines.append(f"  ... and {len(triage['unavailable']) - MAX_LISTED} more repositories")
    lines.append("Fix the repositories and retry, or add --skip-unavailable to install only the "
                 "available packages and skip these.")
    return "\n".join(lines)


def print_destination_checks(source_path, manifest, home, plan, *, rewrite, apply):
    """Report, read-only, what a restore will replace and what may need repair.

    These findings are informational and never block the restore.
    """
    print("Destination checks (informational; they do not block the restore):")
    print(f"  Will replace {plan['replace_count']} existing files or symlinks "
          "(originals are kept in the rollback directory) and create "
          f"{plan['new_count']} new ones.")
    source_home = manifest["source"]["home"]
    if source_home == str(home):
        if rewrite:
            print("  --rewrite-home has no effect: the source and destination homes are the same.")
    else:
        references = report.find_home_references(source_path, manifest, source_home)
        if not references:
            print(f"  No references to the old home path {source_home} were found in the "
                  "settings files that --rewrite-home can change.")
        else:
            total = sum(count for _, count in references)
            print(f"  The old home path {source_home} appears {total} times in "
                  f"{len(references)} bundle files:")
            print_limited([f"    {path}: {count}" for path, count in references])
            if not rewrite:
                print(f"  Not rewritten. Add --rewrite-home (with --apply) to replace it with "
                      f"{home} in exactly these files; no other file is changed.")
            elif apply:
                print(f"  --rewrite-home: these references will be rewritten to {home} after "
                      "each file's checksum is validated.")
            else:
                print(f"  --rewrite-home: these references would be rewritten to {home} "
                      "when you add --apply.")
    panel = report.check_panel_plugins(source_path, manifest, home)
    autostart = report.check_autostart(source_path, manifest, home, source_home,
                                       rewrite=rewrite)
    if panel["missing"]:
        print("  Panel plugins used by the bundled panel but not installed here:")
        print_limited([f"    {report.clip(kind)}" for kind in panel["missing"]])
        print("    Install the packages that provide them before first login, for example "
              "find them with: dnf provides '*/xfce4/panel/plugins/NAME.desktop'")
    if autostart["missing"]:
        print("  Autostart programs not found on this system:")
        print_limited([f"    {path}: {program}" for path, program in autostart["missing"]])
    print_limited([f"  Note: {report.clip(warning, 300)}"
                   for warning in panel["warnings"] + autostart["warnings"]])
    if not (panel["missing"] or panel["warnings"] or autostart["missing"]
            or autostart["warnings"]):
        print("  Panel plugins and autostart programs: no problems found.")


def backup_command(args):
    info = platform_info()
    require_platform(info)
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
    destination = require_platform(info)
    source_path = args.bundle.expanduser()
    manifest = bundle.verify_bundle(source_path)
    validate_manifest_metadata(manifest)
    source = manifest["source"]
    note = restore_release_note(source_release(source), destination)
    if note:
        print(f"Note: {note}")
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
    print_destination_checks(source_path, manifest, home, plan,
                             rewrite=args.rewrite_home, apply=args.apply)
    rewrite_home = (source["home"], str(home)) if args.rewrite_home else None
    commands = []
    triage = None
    if args.install_packages:
        if not args.skip_rpm and manifest["packages"]["rpm"]["requests"]:
            triage = packages.triage_rpm(manifest["packages"], *packages.collect_destination_rpms())
            print_rpm_triage(manifest["packages"], triage, skip_unavailable=args.skip_unavailable)
            if args.apply and triage["unavailable"] and not args.skip_unavailable:
                raise MigrationError(unavailable_refusal(manifest["packages"], triage))
        commands = packages.package_commands(
            manifest["packages"], skip_rpm=args.skip_rpm, skip_flatpak=args.skip_flatpak,
            schema_version=manifest["schema_version"], triage=triage,
            skip_unavailable=args.skip_unavailable,
        )
        print(f"Package commands (repositories must already be configured for Fedora {destination}):")
        for command in commands:
            print("  " + shlex.join(command))
        if not commands:
            print("  No package requests selected.")
    else:
        print("Package installation is not selected; use --install-packages to include it.")
        if args.skip_unavailable:
            print("Note: --skip-unavailable has no effect without --install-packages.")
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
            schema_version=manifest["schema_version"], triage=triage,
            skip_unavailable=args.skip_unavailable,
        )
    def recheck_before_writing():
        # Package installs and re-verifying a large bundle take time: recheck
        # after verification, immediately before the first file changes.
        assert_xfce_stopped()
        assert_applications_closed(secrets=secrets)

    print("Restoring selected files and retaining prior versions...", flush=True)
    result = bundle.restore_bundle(source_path, home, dry_run=False, backup_dir=rollback,
                                   before_write=recheck_before_writing,
                                   rewrite_home=rewrite_home)
    if result["backup_dir"]:
        print(f"Rollback directory: {result['backup_dir']}", flush=True)
    if selinux:
        relabel_paths(home, result["selected_paths"])
    print(f"Restored {result['files']} files and {result['symlinks']} symlinks.")
    if rewrite_home and result["rewritten_count"]:
        count = result["rewritten_count"]
        print(f"Rewrote the old home path in {count} file{'s' if count != 1 else ''}.")
    print_warnings(result.get("warnings", []))
    print("Log into XFCE and check your panel, shortcuts, themes, applications, and files.")
    print("Keep the source backup and rollback directory until you have checked the result.")
    return 0


def print_rollback_plan(plan, *, apply):
    """Describe, read-only, what a rollback does; paths are home-relative."""
    print("Validated rollback plan:" if apply else "Rollback preview:")
    print(f"Rollback directory: {plan['rollback_dir']} (restore status: {plan['status']})")
    print(f"Destination: {plan['home']}")
    if plan["verified"]:
        print(f"Bundle: {plan['bundle']} (checksums verified)")
    else:
        print(f"WARNING: the bundle could not be verified: {plan['bundle_problem']}")
        print("  The tool cannot verify whether existing files are still as the restore")
        print("  left them, so every existing path is a CONFLICT. If the bundle moved, pass")
        print("  --bundle PATH (it is verified in full before it is used).")
    if plan["rewrite_home"]:
        print(f"The restore rewrote {plan['rewrite_home'][0]} to {plan['rewrite_home'][1]} in "
              "eligible settings files; rewritten files count as restored.")
    conflicting = {item["path"] for item in plan["conflicts"]}
    put_back = [path for path in plan["put_back"] if path not in conflicting]
    remove = [path for path in plan["remove"] if path not in conflicting]
    print(f"PUT_BACK: {len(put_back)} (saved originals are copied back over the restored files)")
    print_limited([f"    {report.clip(path)}" for path in put_back])
    print(f"REMOVE: {len(remove)} (files and symlinks the restore added, unchanged since)")
    print_limited([f"    {report.clip(path)}" for path in remove])
    print(f"UNCHANGED: {len(plan['unchanged'])} (the restore never replaced the original, "
          "or it is already back)")
    print_limited([f"    {report.clip(path)}" for path in plan["unchanged"]])
    print(f"CONFLICT: {len(plan['conflicts'])}")
    for item in plan["conflicts"][:MAX_LISTED]:
        outcome = ""
        if plan["include_changed"] and not item["blocked"]:
            outcome = " - processed because of --include-changed"
        print(f"    {report.clip(item['path'])} (would {item['action'].replace('_', ' ')}): "
              f"{item['reason']}{outcome}")
    if len(plan["conflicts"]) > MAX_LISTED:
        print(f"    ... and {len(plan['conflicts']) - MAX_LISTED} more")
    if plan["conflicts"] and not plan["include_changed"]:
        print("  Conflicts are left alone. Add --include-changed to process them anyway; "
              "the current version is always saved under the undone directory first.")
    print(f"Directories the restore created that would be removed if empty: "
          f"{len(plan['remove_directories'])}")
    print_limited([f"    {report.clip(path)}" for path in plan["remove_directories"]])
    if plan["kept_directories"]:
        print_limited([f"    kept {report.clip(item['path'])}: {item['reason']}"
                       for item in plan["kept_directories"]])


def rollback_command(args):
    info = platform_info()
    require_platform(info)
    home = Path(info["home"])
    rollback_dir = args.rollback_dir.expanduser()
    source = args.bundle.expanduser() if args.bundle else None
    plan = rollback.plan_rollback(rollback_dir, home, bundle=source,
                                  include_changed=args.include_changed)
    print_rollback_plan(plan, apply=args.apply)
    if not args.apply:
        print("Preview only: no files or rollback directories were changed.")
        print("Review the plan, then add --apply to roll the restore back.")
        return 0
    secrets = plan["secrets"]
    assert_xfce_stopped()
    assert_applications_closed(secrets=secrets)
    selinux = check_selinux_tools()

    def recheck_before_writing():
        assert_xfce_stopped()
        assert_applications_closed(secrets=secrets)

    print("Rolling back and saving current versions first...", flush=True)
    result = rollback.apply_rollback(rollback_dir, home, bundle=source,
                                     include_changed=args.include_changed,
                                     before_write=recheck_before_writing)
    if selinux:
        relabel_paths(home, result["put_back"])
    print(f"Put back {len(result['put_back'])} files or symlinks, removed "
          f"{len(result['removed'])} and {len(result['removed_directories'])} directories; "
          f"{len(plan['unchanged'])} needed nothing.")
    if result["undone_dir"]:
        print(f"Versions replaced or removed were saved in: {result['undone_dir']}", flush=True)
    for item in result["kept_directories"]:
        print(f"  Kept directory {report.clip(item['path'])}: {item['reason']}")
    if result["skipped"]:
        print(f"Left alone: {len(result['skipped'])} conflicts (see the list above). Review them, "
              "then run rollback again with --include-changed to undo them too.")
    print("Keep the rollback directory until you have checked the result.")
    return 0


def parser():
    result = argparse.ArgumentParser(
        description="Portable Fedora XFCE backup, restore and rollback, built and tested for 43 → 44. Restore and rollback preview by default.",
    )
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = result.add_subparsers(dest="command", required=True)
    backup = subcommands.add_parser("backup", help="Create a new portable bundle on the source Fedora system")
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
    restore = subcommands.add_parser("restore", help="Preview or apply a backup on the destination Fedora system")
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--apply", action="store_true", help="Perform the restore; otherwise show a read-only preview")
    restore.add_argument("--install-packages", action="store_true", help="Also reinstall recorded RPMs and Flatpaks, retaining manager confirmations")
    restore.add_argument("--skip-rpm", action="store_true", help="Omit RPMs when installing packages")
    restore.add_argument("--skip-flatpak", action="store_true", help="Omit Flatpaks when installing packages")
    restore.add_argument("--skip-unavailable", action="store_true", help="With --install-packages, continue when some recorded RPMs are unavailable here and install only the available ones (default: refuse before changing anything)")
    restore.add_argument("--rewrite-home", action="store_true", help="With --apply, replace the old home path with this home in the settings files listed by the preview (never browser or Flatpak data); the preview shows what would change")
    restore.add_argument("--rollback-dir", type=Path, help="New directory for overwritten files and restore journal")
    restore.set_defaults(handler=restore_command)
    undo = subcommands.add_parser("rollback", help="Preview or undo a restore from its rollback directory")
    undo.add_argument("--rollback-dir", type=Path, required=True, help="Rollback directory a restore --apply wrote")
    undo.add_argument("--bundle", type=Path, help="Bundle to compare against if it moved (default: the path in the journal)")
    undo.add_argument("--apply", action="store_true", help="Perform the rollback; otherwise show a read-only preview")
    undo.add_argument("--include-changed", action="store_true", help="Also process files changed since the restore; their current version is saved first")
    undo.set_defaults(handler=rollback_command)
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
