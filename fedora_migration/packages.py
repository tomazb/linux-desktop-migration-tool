"""Portable RPM requests and scoped Flatpak app inventory.

Inventory is descriptive; restore never installs old RPM NEVRAs, copies repository
configuration, installs groups or environments, or adds a Flatpak remote. All
package manager calls use argv lists.
"""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tomllib


class PackageError(RuntimeError):
    """An invalid package inventory or failed package manager operation."""


INSTALLATIONS_DIR = Path("/etc/flatpak/installations.d")
# DNF5 keeps installed group/environment state here (version "1.0"), readable by
# every user. Group names are descriptive only: restore never requests groups.
GROUPS_STATE = Path("/usr/lib/sysimage/libdnf5/groups.toml")
ENVIRONMENTS_STATE = Path("/usr/lib/sysimage/libdnf5/environments.toml")
_STATE_VERSION = "1.0"
_MAX_STATE_BYTES = 16 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9._:+@-]+\Z")
# Bounds keep a hostile manifest from producing unbounded output or memory use.
_MAX_ID, _MAX_NAME, _MAX_URL, _MAX_PATH = 256, 512, 2048, 4096
_MAX_REPOS, _MAX_URLS, _MAX_GROUPS, _MAX_ENVIRONMENT_GROUPS = 1000, 64, 10000, 1000
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


def _command_environment(argv):
    """DNF5 and Flatpak output is parsed, so they always run in a fixed locale."""
    program = argv[1] if argv[0] == "sudo" and len(argv) > 1 else argv[0]
    if program in ("dnf5", "flatpak"):
        return {**os.environ, "LC_ALL": "C.UTF-8"}
    return None


def _run(argv, *, capture=True):
    """Run an external command, retaining the terminal for confirmations."""
    try:
        result = subprocess.run(argv, check=True, text=True, capture_output=capture,
                                env=_command_environment(argv))
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


# Exact keys of every package-manifest object, per manifest schema version. A new
# schema version adds an entry here instead of loosening the older ones, so each
# version keeps rejecting unexpected keys.
_KEYS_V1 = {
    "package": ("rpm", "flatpak", "warnings"),
    "rpm": ("status", "requests", "inventory"),
    "flatpak": ("status", "apps", "remotes"),
    "rpm_entry": ("spec", "reason", "from_repo", "full_nevra"),
    "remote": ("scope", "name", "url"),
    "app": ("scope", "origin", "ref"),
}
_KEYS_V2 = {
    **_KEYS_V1,
    "rpm": ("status", "requests", "inventory", "repos", "groups", "environments"),
    "repo": ("id", "name", "base_urls", "metalink", "mirrorlist", "gpg_keys", "repo_file_path"),
    "group": ("id", "userinstalled"),
    "environment": ("id", "groups"),
}
PACKAGE_KEYS = {1: _KEYS_V1, 2: _KEYS_V2}
CURRENT_SCHEMA = 2


def _list(value, label, limit=None):
    if not isinstance(value, list):
        raise PackageError(f"{label} must be a list")
    if limit is not None and len(value) > limit:
        raise PackageError(f"{label} has more than {limit} entries")


def _bounded(value, label, limit, *, allow_empty=True):
    _text(value, label, allow_empty=allow_empty)
    if len(value) > limit:
        raise PackageError(f"{label} is longer than {limit} characters")


def _identifier(value, label):
    _bounded(value, label, _MAX_ID, allow_empty=False)
    if not _ID.fullmatch(value):
        raise PackageError(f"Invalid {label}: {value!r}")


def _validate_repo(repo, keys):
    _mapping(repo, "RPM repository", keys["repo"])
    _identifier(repo["id"], "RPM repository id")
    _bounded(repo["name"], "RPM repository name", _MAX_NAME)
    for key in ("base_urls", "gpg_keys"):
        _list(repo[key], f"RPM repository {key}", _MAX_URLS)
        for url in repo[key]:
            _bounded(url, f"RPM repository {key} entry", _MAX_URL, allow_empty=False)
    for key in ("metalink", "mirrorlist"):
        _bounded(repo[key], f"RPM repository {key}", _MAX_URL)
    _bounded(repo["repo_file_path"], "RPM repository file path", _MAX_PATH)


def _validate_unique(items, label):
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        raise PackageError(f"Duplicate {label} id")


def _validate_group_state(rpm, keys):
    _list(rpm["repos"], "RPM repos", _MAX_REPOS)
    for repo in rpm["repos"]:
        _validate_repo(repo, keys)
    _validate_unique(rpm["repos"], "RPM repository")
    _list(rpm["groups"], "RPM groups", _MAX_GROUPS)
    for group in rpm["groups"]:
        _mapping(group, "RPM group", keys["group"])
        _identifier(group["id"], "RPM group id")
        if type(group["userinstalled"]) is not bool:
            raise PackageError("RPM group userinstalled must be true or false")
    _validate_unique(rpm["groups"], "RPM group")
    _list(rpm["environments"], "RPM environments", _MAX_GROUPS)
    for environment in rpm["environments"]:
        _mapping(environment, "RPM environment", keys["environment"])
        _identifier(environment["id"], "RPM environment id")
        _list(environment["groups"], "RPM environment groups", _MAX_ENVIRONMENT_GROUPS)
        for group in environment["groups"]:
            _identifier(group, "RPM environment group id")
    _validate_unique(rpm["environments"], "RPM environment")


def validate_package_manifest(data, schema_version=CURRENT_SCHEMA):
    """Validate without invoking package managers, so verification is portable.

    schema_version is the schema of the manifest the data came from; inventories
    collected now are validated as the current schema.
    """
    if type(schema_version) is not int or schema_version not in PACKAGE_KEYS:
        raise PackageError(f"Unsupported package manifest schema version: {schema_version!r}")
    keys = PACKAGE_KEYS[schema_version]
    _mapping(data, "Package manifest", keys["package"])
    rpm = data["rpm"]
    flatpak = data["flatpak"]
    _mapping(rpm, "RPM inventory", keys["rpm"])
    _mapping(flatpak, "Flatpak inventory", keys["flatpak"])
    if rpm["status"] not in ("captured", "skipped"):
        raise PackageError("RPM status must be captured or skipped")
    if flatpak["status"] not in ("captured", "skipped", "not-installed"):
        raise PackageError("Flatpak status must be captured, skipped or not-installed")
    for key in keys["rpm"][1:]:
        _list(rpm[key], f"RPM {key}")
        if rpm["status"] != "captured" and rpm[key]:
            raise PackageError("Skipped RPM inventory must be empty")
    if "repos" in keys["rpm"]:
        _validate_group_state(rpm, keys)
    for request in rpm["requests"]:
        _rpm_spec(request)
    if len(set(rpm["requests"])) != len(rpm["requests"]):
        raise PackageError("Duplicate RPM requests")
    for item in rpm["inventory"]:
        _mapping(item, "RPM inventory entry", keys["rpm_entry"])
        _rpm_spec(item["spec"])
        for key in ("reason", "from_repo", "full_nevra"):
            _text(item[key], f"RPM {key}", allow_empty=key == "from_repo")
    for key in ("apps", "remotes"):
        _list(flatpak[key], f"Flatpak {key}")
        if flatpak["status"] != "captured" and flatpak[key]:
            raise PackageError("Skipped or not-installed Flatpak inventory must be empty")
    remotes = {}
    for remote in flatpak["remotes"]:
        _mapping(remote, "Flatpak remote", keys["remote"])
        _scope(remote["scope"])
        _remote_name(remote["name"])
        _remote_url(remote["url"])
        key = (remote["scope"], remote["name"])
        if key in remotes:
            raise PackageError(f"Duplicate Flatpak remote: {key[0]}/{key[1]}")
        remotes[key] = remote["url"]
    apps = set()
    for app in flatpak["apps"]:
        _mapping(app, "Flatpak app", keys["app"])
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


def _oneline(text, limit=300):
    """A command's error text as one printable line, safe to store as a warning."""
    text = " ".join("".join(" " if ord(char) < 32 or ord(char) == 127 else char
                            for char in str(text)).split())
    return text if len(text) <= limit else text[:limit] + "..."


def _strip_credentials(url):
    """Drop user:password@ from a URL's authority, leaving everything else as is."""
    scheme, separator, rest = url.partition("://")
    if not separator:
        return url
    end = min((rest.find(char) for char in "/?#" if char in rest), default=len(rest))
    authority = rest[:end]
    if "@" not in authority:
        return url
    return f"{scheme}://{authority.rpartition('@')[2]}{rest[end:]}"


def _url_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value] if value else []
    if not isinstance(value, list):
        raise PackageError("expected a URL or a list of URLs")
    return [_strip_credentials(url) if isinstance(url, str) else url for url in value]


def _repo_record(item):
    """One `dnf5 repo info --json` object as a stored record, credentials removed."""
    if not isinstance(item, dict):
        raise PackageError("repository entry is not an object")

    def optional(key, default=""):
        value = item.get(key)
        return default if value is None else value

    def url(key):
        value = optional(key)
        return _strip_credentials(value) if isinstance(value, str) else value

    record = {
        "id": item.get("id"), "name": optional("name"),
        "base_urls": _url_list(item.get("base_url")),
        "metalink": url("metalink"), "mirrorlist": url("mirrorlist"),
        "gpg_keys": _url_list(item.get("gpg_key")),
        "repo_file_path": optional("repo_file_path"),
    }
    _validate_repo(record, _KEYS_V2)
    return record


def _collect_repos(warnings):
    """Enabled repositories as descriptive records; failures only produce warnings."""
    try:
        output = _run(["dnf5", "repo", "info", "--enabled", "--json"])
    except (PackageError, FileNotFoundError) as exc:
        warnings.append(f"Could not read enabled DNF repositories, so none were recorded: {_oneline(exc)}")
        return []
    try:
        items = json.loads(output)
        if not isinstance(items, list):
            raise ValueError("expected a JSON list of repositories")
    except ValueError as exc:
        warnings.append(f"Could not parse 'dnf5 repo info --enabled --json', so no repositories were recorded: {_oneline(exc)}")
        return []
    repos, skipped = {}, 0
    for item in items:
        try:
            record = _repo_record(item)
        except PackageError:
            skipped += 1
            continue
        if record["id"] in repos:
            skipped += 1
            continue
        repos[record["id"]] = record
    if skipped:
        warnings.append(f"{skipped} enabled DNF repositor{'y' if skipped == 1 else 'ies'} could not be recorded "
                        "(unusable, duplicate or oversized details).")
    if len(repos) > _MAX_REPOS:
        warnings.append(f"More than {_MAX_REPOS} DNF repositories are enabled; none were recorded.")
        return []
    return list(repos.values())


def _state_table(path, table):
    """The `table` mapping of a DNF5 state file, or raise ValueError/OSError."""
    if path.stat().st_size > _MAX_STATE_BYTES:
        raise ValueError("file is unexpectedly large")
    with open(path, "rb") as stream:
        data = tomllib.load(stream)
    if data.get("version") != _STATE_VERSION:
        raise ValueError(f"unexpected state version {_oneline(data.get('version'), 40)!r}")
    section = data.get(table, {})
    if not isinstance(section, dict) or len(section) > _MAX_GROUPS:
        raise ValueError(f"unexpected [{table}] section")
    for identifier, body in section.items():
        if not isinstance(body, dict) or not _ID.fullmatch(identifier) or len(identifier) > _MAX_ID:
            raise ValueError(f"unexpected {table[:-1]} entry {_oneline(identifier, 40)!r}")
    return section


def _group_records(section):
    records = []
    for identifier, body in section.items():
        flag = body.get("userinstalled", False)
        if type(flag) is not bool:
            raise ValueError(f"userinstalled of group {identifier!r} is not true or false")
        records.append({"id": identifier, "userinstalled": flag})
    return sorted(records, key=lambda record: record["id"])


def _environment_records(section):
    records = []
    for identifier, body in section.items():
        members = body.get("groups", [])
        if (not isinstance(members, list) or len(members) > _MAX_ENVIRONMENT_GROUPS
                or not all(isinstance(member, str) and len(member) <= _MAX_ID and _ID.fullmatch(member)
                           for member in members)):
            raise ValueError(f"groups of environment {identifier!r} are not a list of group ids")
        records.append({"id": identifier, "groups": list(members)})
    return sorted(records, key=lambda record: record["id"])


def _collect_state(path, table, build, warnings):
    try:
        return build(_state_table(path, table))
    # TOMLDecodeError and UnicodeDecodeError are ValueErrors; deeply nested
    # input exhausts the parser's recursion.
    except (OSError, ValueError, RecursionError) as exc:
        warnings.append(f"Could not read DNF {table} from {path}, so none were recorded: {_oneline(exc)}")
        return []


def collect_packages(*, skip_rpm=False, skip_flatpak=False):
    """Capture RPM requests and both supported Flatpak installation scopes."""
    data = {
        "rpm": {"status": "skipped" if skip_rpm else "captured", "requests": [], "inventory": [],
                "repos": [], "groups": [], "environments": []},
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
        data["rpm"]["repos"] = _collect_repos(data["warnings"])
        data["rpm"]["groups"] = _collect_state(GROUPS_STATE, "groups", _group_records, data["warnings"])
        data["rpm"]["environments"] = _collect_state(
            ENVIRONMENTS_STATE, "environments", _environment_records, data["warnings"])
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


def collect_destination_rpms():
    """Installed and available name.arch sets on this system (read-only queries).

    Querying available packages can download repository metadata.
    """
    try:
        # Configured excludes must not hide what is already installed; they still
        # limit what can be installed below and in the transaction.
        installed = _run(["dnf5", "repoquery", "--installed", "--setopt=disable_excludes=*",
                          "--queryformat", "%{name}.%{arch}\n"])
        available = _run(["dnf5", "repoquery", "--available", "--queryformat", "%{name}.%{arch}\n"])
    except FileNotFoundError as exc:
        raise PackageError("dnf5 is required to check RPM availability; install it or use --skip-rpm") from exc
    return ({row[0] for row in _rows(installed, 1, "DNF5 installed packages")},
            {row[0] for row in _rows(available, 1, "DNF5 available packages")})


# Packages whose source repository cannot be enabled by name: installed from a
# local file, or with no recorded origin. They are listed, never installed.
_MANUAL_REPOS = frozenset(("@commandline", "<unknown>", ""))


def triage_rpm(data, installed, available):
    """Classify every RPM request against the destination's installed/available sets.

    Returns sorted lists `installed` and `installable`; `manual` entries (dicts
    with spec, from_repo and full_nevra) that need manual handling; and
    `unavailable`, a dict of source repository id -> sorted specs. Groups and
    environments are never requested: DNF5 would reinstall their default members.
    """
    # Installonly packages (kernels) can have several inventory rows per spec.
    origins = {}
    for item in data["rpm"]["inventory"]:
        origins.setdefault(item["spec"], []).append(item)
    result = {"installed": [], "installable": [], "installable_from_local": [],
              "manual": [], "unavailable": {}}
    for spec in sorted(data["rpm"]["requests"]):
        records = origins.get(spec, [])
        repos = sorted({record["from_repo"] for record in records} - _MANUAL_REPOS)
        if spec in installed:
            result["installed"].append(spec)
        elif spec in available:
            result["installable"].append(spec)
            if not repos:
                # Installed from a local file or an unknown source on the old
                # system; a repository here provides the same name now.
                result["installable_from_local"].append(spec)
        elif repos:
            # Any record from a real repository makes it unavailable (strict),
            # whatever order the rows came in.
            result["unavailable"].setdefault(repos[0], []).append(spec)
        else:
            origin = sorted(record["from_repo"] for record in records)
            result["manual"].append({
                "spec": spec, "from_repo": origin[0] if origin else "",
                "full_nevra": ", ".join(sorted(r["full_nevra"] for r in records)) or spec})
    result["unavailable"] = dict(sorted(result["unavailable"].items()))
    return result


def unavailable_hint(data, repo):
    """What to do about requests whose source repository is not usable here."""
    recorded = next((item for item in data["rpm"].get("repos", []) if item["id"] == repo), None)
    if recorded is None:
        return (f"repository {repo} from the source system is not recorded; "
                "configure it or drop these packages")
    details = []
    if recorded["base_urls"]:
        details.append("base URL " + ", ".join(recorded["base_urls"]))
    if recorded["metalink"]:
        details.append(f"metalink {recorded['metalink']}")
    if recorded["mirrorlist"]:
        details.append(f"mirrorlist {recorded['mirrorlist']}")
    shown = "; ".join(details) if details else "no URL"
    return f"enable repository {repo} (recorded {shown}) and retry"


def package_commands(data, *, skip_rpm=False, skip_flatpak=False, schema_version=CURRENT_SCHEMA,
                     triage=None, skip_unavailable=False):
    """Build a reviewable command preview without running external commands.

    With a triage result only its installable RPMs are requested; without one,
    every recorded request is. --skip-unavailable is only passed when given.
    """
    validate_package_manifest(data, schema_version)
    commands = []
    if not skip_rpm:
        requests = data["rpm"]["requests"] if triage is None else sorted(triage["installable"])
        for request in requests:
            _rpm_spec(request)
        if requests:
            commands.append(["sudo", "dnf5", "install",
                             *(["--skip-unavailable"] if skip_unavailable else []), *requests])
    if not skip_flatpak:
        batches = {}
        for app in data["flatpak"]["apps"]:
            batches.setdefault((app["scope"], app["origin"]), []).append(app["ref"])
        # User installations first, as they are inventoried; one transaction each.
        for scope, origin in sorted(batches, key=lambda key: (key[0] != "user", key[1])):
            commands.append(["flatpak", "install", f"--{scope}", origin, *sorted(batches[(scope, origin)])])
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


def install_packages(data, *, skip_rpm=False, skip_flatpak=False, schema_version=CURRENT_SCHEMA,
                     triage=None, skip_unavailable=False):
    """Preflight remotes, then run native package managers with confirmation."""
    if triage is not None and triage["unavailable"] and not skip_unavailable and not skip_rpm:
        count = sum(len(specs) for specs in triage["unavailable"].values())
        raise PackageError(f"{count} requested RPM packages are not available on this system; "
                           "nothing was installed. Fix the repositories, or use --skip-unavailable "
                           "to install only the available packages.")
    commands = package_commands(data, skip_rpm=skip_rpm, skip_flatpak=skip_flatpak,
                                schema_version=schema_version, triage=triage,
                                skip_unavailable=skip_unavailable)
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
