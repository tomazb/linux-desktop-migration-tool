"""Portable RPM requests and scoped Flatpak app inventory.

Inventory is descriptive; restore never installs old RPM NEVRAs, copies repository
configuration, or adds a Flatpak remote. All package manager calls use argv lists.
"""

from pathlib import Path
import re
import shlex
import subprocess


class PackageError(RuntimeError):
    """An invalid package inventory or failed package manager operation."""


INSTALLATIONS_DIR = Path("/etc/flatpak/installations.d")
_RPM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]*\Z")
_RPM_ARCHES = frozenset((
    "noarch", "x86_64", "i386", "i486", "i586", "i686", "aarch64",
    "armv5tel", "armv6hl", "armv7hl", "armv7hnl", "ppc", "ppc64",
    "ppc64le", "s390", "s390x", "riscv64", "loongarch64",
))
_REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_REF_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
# URLs are only compared with destination remotes, never used to add one.
# Reject ASCII spaces and control characters (via _text) only: Unicode
# whitespace classes differ between Python and glibc, and non-ASCII URLs in
# existing bundles must remain readable.
_ASCII_SPACE = frozenset(" \t\n\r\v\f")


def _run(argv, *, capture=True):
    """Run an external command, retaining the terminal for confirmations."""
    try:
        result = subprocess.run(argv, check=True, text=True, capture_output=capture)
    except FileNotFoundError:
        # Backup distinguishes an absent optional Flatpak binary from a failure.
        raise
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else " (see the package manager output above)"
        raise PackageError(
            f"Package command failed (exit {exc.returncode}): {shlex.join(argv)}{suffix}. "
            "Review package availability and repository configuration, then retry."
        ) from exc
    except OSError as exc:
        raise PackageError(f"Cannot run package command {shlex.join(argv)}: {exc}") from exc
    return result.stdout or ""


def _custom_installations():
    try:
        # Reject even broken symlinks/config files: inventory completeness cannot
        # be established for installations this tool does not support.
        return sorted(path for path in INSTALLATIONS_DIR.iterdir() if path.name.endswith(".conf"))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PackageError(f"Cannot inspect Flatpak installations in {INSTALLATIONS_DIR}: {exc}") from exc


def _check_standard_installations():
    custom = _custom_installations()
    if custom:
        names = ", ".join(str(path) for path in custom)
        raise PackageError(
            f"Unsupported custom Flatpak installations configured in {names}. "
            "Only the user and default system installations are supported; "
            "handle custom installations separately or use --skip-flatpak."
        )


def _text(value, label, *, allow_empty=False):
    if not isinstance(value, str) or (not value and not allow_empty):
        raise PackageError(f"{label} must be {'a' if not allow_empty else 'an optionally empty'} string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise PackageError(f"{label} contains control characters")
    return value


def _rpm_spec(value):
    _text(value, "RPM request")
    name, dot, arch = value.rpartition(".")
    if not dot or not _RPM_NAME.fullmatch(name) or arch not in _RPM_ARCHES:
        raise PackageError(f"Invalid RPM name.arch request: {value!r}")


def _remote_name(value):
    _text(value, "Flatpak remote name")
    if not _REMOTE_NAME.fullmatch(value):
        raise PackageError(f"Invalid Flatpak remote name: {value!r}")


def _remote_url(value):
    _text(value, "Flatpak remote URL")
    if any(char in _ASCII_SPACE for char in value):
        raise PackageError(f"Flatpak remote URL must not contain spaces: {value!r}")


def _flatpak_ref(value):
    _text(value, "Flatpak app ref")
    parts = value.split("/")
    if len(parts) == 4 and parts[0] == "app":
        parts = parts[1:]
    if (len(parts) != 3 or "." not in parts[0]
            or not all(_REF_PART.fullmatch(part) for part in parts)):
        raise PackageError(f"Invalid Flatpak app ref (expected app/ID/arch/branch or ID/arch/branch): {value!r}")


def _scope(value):
    if value not in ("user", "system"):
        raise PackageError(f"Unsupported Flatpak scope: {value!r}; expected user or system")


def _mapping(value, label, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise PackageError(f"{label} must be an object containing {', '.join(keys)}")


def _list(value, label):
    if not isinstance(value, list):
        raise PackageError(f"{label} must be a list")


def validate_package_manifest(data):
    """Validate without invoking package managers, so verification is portable."""
    _mapping(data, "Package manifest", ("rpm", "flatpak", "warnings"))
    rpm = data["rpm"]
    flatpak = data["flatpak"]
    _mapping(rpm, "RPM inventory", ("status", "requests", "inventory"))
    _mapping(flatpak, "Flatpak inventory", ("status", "apps", "remotes"))
    if rpm["status"] not in ("captured", "skipped"):
        raise PackageError("RPM status must be captured or skipped")
    if flatpak["status"] not in ("captured", "skipped", "not-installed"):
        raise PackageError("Flatpak status must be captured, skipped or not-installed")
    for key in ("requests", "inventory"):
        _list(rpm[key], f"RPM {key}")
        if rpm["status"] != "captured" and rpm[key]:
            raise PackageError("Skipped RPM inventory must be empty")
    for request in rpm["requests"]:
        _rpm_spec(request)
    if len(set(rpm["requests"])) != len(rpm["requests"]):
        raise PackageError("Duplicate RPM requests")
    for item in rpm["inventory"]:
        _mapping(item, "RPM inventory entry", ("spec", "reason", "from_repo", "full_nevra"))
        _rpm_spec(item["spec"])
        for key in ("reason", "from_repo", "full_nevra"):
            _text(item[key], f"RPM {key}", allow_empty=key == "from_repo")
    for key in ("apps", "remotes"):
        _list(flatpak[key], f"Flatpak {key}")
        if flatpak["status"] != "captured" and flatpak[key]:
            raise PackageError("Skipped or not-installed Flatpak inventory must be empty")
    remotes = {}
    for remote in flatpak["remotes"]:
        _mapping(remote, "Flatpak remote", ("scope", "name", "url"))
        _scope(remote["scope"])
        _remote_name(remote["name"])
        _remote_url(remote["url"])
        key = (remote["scope"], remote["name"])
        if key in remotes:
            raise PackageError(f"Duplicate Flatpak remote: {key[0]}/{key[1]}")
        remotes[key] = remote["url"]
    apps = set()
    for app in flatpak["apps"]:
        _mapping(app, "Flatpak app", ("scope", "origin", "ref"))
        _scope(app["scope"])
        _remote_name(app["origin"])
        _flatpak_ref(app["ref"])
        if (app["scope"], app["origin"]) not in remotes:
            raise PackageError(f"Flatpak app {app['ref']} has no recorded {app['scope']} origin remote {app['origin']}")
        key = (app["scope"], app["ref"])
        if key in apps:
            raise PackageError(f"Duplicate Flatpak app: {key[0]}/{key[1]}")
        apps.add(key)
    _list(data["warnings"], "Package warnings")
    for warning in data["warnings"]:
        _text(warning, "Package warning")


def _rows(output, columns, description):
    """Parse non-terminal tabular command output; there is no header row."""
    rows = []
    for line in output.splitlines():
        if not line:
            continue
        row = line.split("\t")
        if len(row) != columns:
            raise PackageError(f"Unexpected {description} output: {line!r}; expected {columns} tab-separated columns")
        rows.append(row)
    return rows


def _remote_inventory(scope, *, include_disabled=True):
    argv = ["flatpak", "remotes", f"--{scope}"]
    if include_disabled:
        argv.append("--show-disabled")
    argv.append("--columns=name,url")
    output = _run(argv)
    remotes = []
    seen = set()
    for name, url in _rows(output, 2, "Flatpak remotes"):
        _remote_name(name)
        _remote_url(url)
        if name in seen:
            raise PackageError(f"Duplicate {scope} Flatpak remote in command output: {name}")
        seen.add(name)
        remotes.append({"scope": scope, "name": name, "url": url})
    return remotes


def collect_packages(*, skip_rpm=False, skip_flatpak=False):
    """Capture RPM requests and both supported Flatpak installation scopes."""
    data = {
        "rpm": {"status": "skipped" if skip_rpm else "captured", "requests": [], "inventory": []},
        "flatpak": {"status": "skipped" if skip_flatpak else "captured", "apps": [], "remotes": []},
        "warnings": [],
    }
    if not skip_flatpak:
        _check_standard_installations()
    if not skip_rpm:
        try:
            # --userinstalled already selects installed packages; DNF5 rejects
            # combining it with the separate --installed selector.
            requests = _run(["dnf5", "repoquery", "--userinstalled", "--queryformat", "%{name}.%{arch}\n"])
            inventory = _run(["dnf5", "repoquery", "--installed", "--queryformat", "%{name}.%{arch}\t%{reason}\t%{from_repo}\t%{full_nevra}\n"])
        except FileNotFoundError as exc:
            raise PackageError("dnf5 is required to capture RPM packages; install it or use --skip-rpm") from exc
        data["rpm"]["requests"] = list(dict.fromkeys(row[0] for row in _rows(requests, 1, "DNF5 requests")))
        data["rpm"]["inventory"] = [dict(zip(("spec", "reason", "from_repo", "full_nevra"), row))
                                    for row in _rows(inventory, 4, "DNF5 inventory")]
        data["warnings"].append(
            "DNF5 --userinstalled also includes group/module/unknown installation reasons; "
            "configured excludes can hide packages from both queries. Review RPM requests before "
            "installing. Old versions and repository configuration are not restored."
        )
    if not skip_flatpak:
        flatpak_started = False
        try:
            for scope in ("user", "system"):
                output = _run(["flatpak", "list", f"--{scope}", "--app", "--columns=ref,origin"])
                flatpak_started = True
                data["flatpak"]["apps"].extend(
                    {"scope": scope, "ref": ref, "origin": origin}
                    for ref, origin in _rows(output, 2, "Flatpak apps")
                )
                data["flatpak"]["remotes"].extend(_remote_inventory(scope))
        except FileNotFoundError as exc:
            if flatpak_started:
                raise PackageError("flatpak became unavailable while capturing packages; retry the backup") from exc
            data["flatpak"]["status"] = "not-installed"
            data["warnings"].append("Flatpak is not installed; no Flatpak apps or remotes were captured.")
    validate_package_manifest(data)
    return data


def package_commands(data, *, skip_rpm=False, skip_flatpak=False):
    """Build a reviewable command preview without running external commands."""
    validate_package_manifest(data)
    commands = []
    if not skip_rpm and data["rpm"]["requests"]:
        commands.append(["sudo", "dnf5", "install", *data["rpm"]["requests"]])
    if not skip_flatpak:
        for app in data["flatpak"]["apps"]:
            commands.append(["flatpak", "install", f"--{app['scope']}", app["origin"], app["ref"]])
    return commands


def _preflight_remotes(data):
    """Refuse missing, changed or disabled remotes before any transaction."""
    existing = {}
    enabled = {}
    for scope in ("user", "system"):
        existing.update({(scope, remote["name"]): remote["url"] for remote in _remote_inventory(scope)})
        enabled.update({(scope, remote["name"]): remote["url"]
                        for remote in _remote_inventory(scope, include_disabled=False)})
    # Every source remote is checked, including ones without installed apps.
    # Never reconstruct trust from a URL or add repositories automatically.
    for remote in data["flatpak"]["remotes"]:
        key = (remote["scope"], remote["name"])
        label = f"{key[0]} Flatpak remote {key[1]!r}"
        if key not in existing:
            raise PackageError(f"Missing {label}. Configure and review this remote manually (expected URL {remote['url']!r}), then retry or use --skip-flatpak.")
        if existing[key] != remote["url"]:
            raise PackageError(f"Changed {label}: expected URL {remote['url']!r}, found {existing[key]!r}. Review repository configuration before retrying or use --skip-flatpak.")
    used = {(app["scope"], app["origin"]) for app in data["flatpak"]["apps"]}
    for scope, name in sorted(used):
        if (scope, name) not in enabled:
            raise PackageError(f"Disabled {scope} Flatpak remote {name!r}. Review and enable it manually before retrying or use --skip-flatpak.")


def install_packages(data, *, skip_rpm=False, skip_flatpak=False):
    """Preflight remotes, then run native package managers with confirmation."""
    commands = package_commands(data, skip_rpm=skip_rpm, skip_flatpak=skip_flatpak)
    if not skip_flatpak and data["flatpak"]["status"] == "captured":
        _check_standard_installations()
        try:
            _preflight_remotes(data)
        except FileNotFoundError as exc:
            raise PackageError("flatpak is required to preflight captured remotes; install it or use --skip-flatpak") from exc
    for command in commands:
        try:
            _run(command, capture=False)
        except FileNotFoundError as exc:
            raise PackageError(f"Required package command {command[0]!r} is not installed; install it and retry") from exc
