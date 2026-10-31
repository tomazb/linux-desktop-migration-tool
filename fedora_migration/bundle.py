"""Portable, checksummed home-directory bundles; never evaluate saved settings.

Bundles and destination homes must be controlled by the user and quiescent while
an operation runs. Checksums detect corruption, not replacement of a manifest by
an attacker. Symlinks are copied as links and are never traversed.
"""

import contextlib
from datetime import datetime, timezone
from fnmatch import fnmatchcase
import functools
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import uuid

from .report import entry_is_rewritable, replace_home_references, rewritable


class BundleError(RuntimeError):
    """An unsafe, inconsistent or unsuccessful filesystem operation."""


class BundleInterrupted(KeyboardInterrupt):
    """An interrupted restore with an actionable recovery location."""


SETTINGS = (
    ".config/xfce4", ".config/Thunar", ".config/gtk-2.0", ".config/gtk-3.0",
    ".config/gtk-4.0", ".gtkrc-2.0", ".config/autostart",
    ".config/mimeapps.list", ".config/user-dirs.dirs",
    ".config/user-dirs.locale", ".icons", ".themes", ".fonts",
    ".local/share/icons", ".local/share/themes", ".local/share/fonts",
    ".local/share/applications", ".local/share/backgrounds",
    ".local/bin", ".bashrc", ".bash_profile",
)
PERSONAL = {
    "DESKTOP": "Desktop", "DOWNLOAD": "Downloads", "TEMPLATES": "Templates",
    "PUBLICSHARE": "Public", "DOCUMENTS": "Documents", "MUSIC": "Music",
    "PICTURES": "Pictures", "VIDEOS": "Videos",
}
PROFILE_DATA = (".mozilla", ".thunderbird", ".var/app")
SECRETS = (
    ".ssh", ".gnupg", ".pki", ".local/share/keyrings", ".config/goa-1.0",
)
# Hardware-bound or disposable data that a fresh installation should not inherit:
# the XFCE monitor layout names the source machine's outputs, and Flatpak app
# caches can be large and are rebuilt on demand.
DEFAULT_EXCLUDES = (
    ".config/xfce4/xfconf/xfce-perchannel-xml/displays.xml",
    ".var/app/*/cache",
)


def _within_secret(relative):
    return any(relative == secret or relative.startswith(secret + "/") for secret in SECRETS)


def selects_secrets(include=(), include_secrets=False):
    """Whether a backup selection opts in to any known secret tree."""
    return include_secrets or any(_within_secret(os.fspath(value)) for value in include)


def manifest_has_secrets(manifest):
    """Whether a verified manifest contains files from a known secret tree."""
    return any(_within_secret(entry["path"]) for entry in manifest["entries"])


def _exclusion(patterns, relative):
    """Return the first pattern matching relative or one of its ancestors.

    Patterns are home-relative; each segment is matched separately, so "*"
    never crosses a "/".
    """
    parts = relative.split("/")
    for pattern in patterns:
        segments = pattern.split("/")
        if len(segments) <= len(parts) and all(
                fnmatchcase(part, segment) for part, segment in zip(parts, segments)):
            return pattern
    return None

# A deliberately bounded, portable nanosecond range for schema 1. Validate this
# before any destination changes rather than discovering an overflow in utime.
MIN_MTIME_NS = -(2**63)
MAX_MTIME_NS = 2**63 - 1


def _owner_accessible(function):
    """Run with umask 077 so everything the tool creates stays usable by its owner.

    Creation modes are reduced by the umask, so a mask such as 0777 would make
    new private files and directories unreadable even to the owner. Final modes
    are applied explicitly with chmod, which the umask does not affect.
    """
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        previous = os.umask(0o077)
        try:
            return function(*args, **kwargs)
        finally:
            os.umask(previous)
    return wrapper


def _check_xdg_layout(home):
    for variable, relative in (("XDG_CONFIG_HOME", ".config"),
                               ("XDG_DATA_HOME", ".local/share")):
        value = os.environ.get(variable)
        if value and (not Path(value).is_absolute()
                      or _absolute(value) != home / relative):
            raise BundleError(
                f"Custom {variable}={value!r} is unsupported by this workflow. "
                f"It expects {home / relative}. Back up and migrate your custom "
                "XDG layout separately; do not rely on a default-path bundle."
            )


# Backslash is an ordinary filename character where "/" is the only path
# separator (Linux, where backup and restore run). verify can also run on
# systems such as Windows that treat it as a separator; reject it there.
_BACKSLASH_IS_SEPARATOR = os.sep != "/" or os.altsep is not None


def _safe_path(value):
    if (not isinstance(value, str) or not value or "\0" in value
            or value.startswith("/")
            or (_BACKSLASH_IS_SEPARATOR and "\\" in value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise BundleError(f"Unsafe relative path: {value!r}")
    return value


def _within(path, parent):
    return path == parent or parent in path.parents


def _absolute(path):
    return Path(os.path.abspath(os.fspath(path)))


def _assert_no_symlink_ancestors(path, *, include_leaf=False):
    nodes = [path] if include_leaf else []
    nodes.extend(path.parents)
    for node in reversed(nodes):
        try:
            mode = node.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise BundleError(f"Refusing symlink ancestor: {node}")
        if node != path and not stat.S_ISDIR(mode):
            raise BundleError(f"Ancestor is not a directory: {node}")


def _require_directory(path):
    _assert_no_symlink_ancestors(path, include_leaf=True)
    if not path.is_dir():
        raise BundleError(f"Directory does not exist: {path}")


def _kind(mode):
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class _HashingReader:
    """Hash bytes as they are read, so a copy needs no second read for its checksum."""

    def __init__(self, stream):
        self._stream = stream
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        block = self._stream.read(size)
        self.digest.update(block)
        return block

    def fileno(self):
        return self._stream.fileno()


def _copy_regular(source, destination, expected=None, *, preserve_metadata=True):
    """Copy an opened regular file; reject replacement or mutation during copy.

    Returns the SHA-256 of the bytes read. The new destination is private
    (0600) from creation. With preserve_metadata=False it stays private so the
    caller can validate it before applying the intended mode and timestamps.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(source, flags)
    with os.fdopen(fd, "rb") as reader:
        before = os.fstat(reader.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise BundleError(f"Not a regular file: {source}")
        if expected is not None and _fingerprint(before) != _fingerprint(expected):
            raise BundleError(f"Source changed before copying: {source}")
        out_flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        hashing = _HashingReader(reader)
        with os.fdopen(os.open(destination, out_flags, 0o600), "wb") as writer:
            shutil.copyfileobj(hashing, writer, 1024 * 1024)
            writer.flush()
            # Apply metadata before fsync so content and metadata are durable together.
            if preserve_metadata:
                os.fchmod(writer.fileno(), stat.S_IMODE(before.st_mode))
                os.utime(writer.fileno(), ns=(before.st_atime_ns, before.st_mtime_ns))
            os.fsync(writer.fileno())
        after = os.fstat(reader.fileno())
        current = source.lstat()
        if (_fingerprint(before) != _fingerprint(after)
                or _fingerprint(before) != _fingerprint(current)):
            raise BundleError(f"Source changed while copying: {source}")
    return hashing.digest.hexdigest()


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_parents(directory):
    """Create directory and its missing ancestors, syncing each new entry."""
    missing = []
    while not os.path.lexists(directory):
        missing.append(directory)
        directory = directory.parent
    for node in reversed(missing):
        node.mkdir(mode=0o700)
        _fsync_directory(node.parent)


def _save_original(original, saved, staging):
    """Publish a recovery copy under previous/ only once it is complete.

    The copy is assembled in the private staging directory on the same
    filesystem, made durable, then renamed into place. A failed or interrupted
    save leaves no partial file under previous/.
    """
    temporary = staging / uuid.uuid4().hex
    try:
        if original.is_symlink():
            info = original.lstat()
            temporary.symlink_to(os.readlink(original))
            os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns),
                     follow_symlinks=False)
        else:
            _copy_regular(original, temporary)
        _private_parents(saved.parent)
        os.replace(temporary, saved)
        _fsync_directory(saved.parent)
    finally:
        # Never let cleanup replace the error that is already propagating.
        with contextlib.suppress(OSError):
            if os.path.lexists(temporary):
                temporary.unlink()


def _xdg_directories(home, warnings):
    result = dict(PERSONAL)
    config = home / ".config/user-dirs.dirs"
    _assert_no_symlink_ancestors(config)
    if not os.path.lexists(config):
        # Without a configuration, xdg-user-dirs uses these default names.
        return list(result.values())
    # Never substitute default names for a configuration that cannot be read
    # safely: that would complete a backup without the configured folders.
    remedy = ("Replace it with a regular file containing your personal-folder "
              "settings, or use --settings-only and select folders with --include.")
    if config.is_symlink():
        raise BundleError(f"{config} is a symlink; personal-folder discovery does "
                          f"not follow it. {remedy}")
    try:
        fd = os.open(config, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise BundleError(f"Cannot read {config}: {exc}. {remedy}") from exc
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        os.close(fd)
        raise
    if not regular:
        os.close(fd)
        raise BundleError(f"{config} is not a regular file. {remedy}")
    with os.fdopen(fd, "rb") as stream:
        text = stream.read().decode("utf-8", errors="replace")
    # Only this narrow assignment grammar is accepted. No shell expansion,
    # command substitution, variable evaluation, or backslash interpretation.
    pattern = re.compile(r'^\s*XDG_([A-Z]+)_DIR="([^"\\]*)"\s*(?:#.*)?$')
    for line in text.splitlines():
        match = pattern.fullmatch(line)
        assignment = re.match(r'^\s*XDG_([A-Z]+)_DIR\s*=', line)
        if assignment and assignment[1] in PERSONAL and not match:
            raise BundleError(
                f"Unsupported XDG_{assignment[1]}_DIR assignment in {config}. "
                "Escaped or nonstandard assignments need separate migration; "
                "refusing to substitute a different default directory."
            )
        if not match or match[1] not in PERSONAL:
            continue
        key, value = match.groups()
        if value in ("$HOME", "${HOME}", str(home)):
            result.pop(key, None)
            continue
        relative = None
        for prefix in ("$HOME/", "${HOME}/", str(home) + "/"):
            if value.startswith(prefix):
                relative = value[len(prefix):]
                break
        if relative is None or "$" in relative or "`" in relative:
            warnings.append(f"Ignored unsupported XDG_{key}_DIR: {value!r}")
            result.pop(key, None)
            continue
        try:
            result[key] = _safe_path(relative)
        except BundleError:
            warnings.append(f"Ignored unsafe XDG_{key}_DIR: {value!r}")
            result.pop(key, None)
    return list(result.values())


def _write_json(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


@_owner_accessible
def create_bundle(home, output, source, packages, *, include=(),
                  include_secrets=False, settings_only=False, exclude=(),
                  default_excludes=True):
    """Create a new private bundle outside HOME, publishing only on success."""
    home, output = _absolute(home), _absolute(output)
    stage = None
    try:
        _require_directory(home)
        _check_xdg_layout(home)
        _assert_no_symlink_ancestors(output)
        if _within(output, home):
            raise BundleError("Backup output must be outside the source HOME.")
        if os.path.lexists(output):
            raise BundleError(f"Backup output already exists: {output}")
        _require_directory(output.parent)
        if not isinstance(source, dict) or not isinstance(packages, dict):
            raise BundleError("Source and packages metadata must be dictionaries.")
        warnings = []
        paths = list(SETTINGS)
        if not settings_only:
            paths.extend(_xdg_directories(home, warnings))
            paths.extend(PROFILE_DATA)
        if include_secrets:
            paths.extend(SECRETS)
        explicit = tuple(_safe_path(os.fspath(value)) for value in include)
        paths.extend(explicit)
        user_excludes = tuple(_safe_path(os.fspath(value)) for value in exclude)
        defaults = DEFAULT_EXCLUDES if default_excludes else ()
        excludes = tuple(dict.fromkeys(user_excludes + defaults))
        # Existing paths left out, recorded under the first pattern that matches.
        excluded = {pattern: set() for pattern in excludes}

        def is_excluded(relative):
            pattern = _exclusion(excludes, relative)
            if pattern is None:
                return False
            if os.path.lexists(home / relative):
                excluded[pattern].add(relative)
            return True

        def secret_excluded(relative):
            for secret in SECRETS:
                if relative == secret or relative.startswith(secret + "/"):
                    opted_in = include_secrets or any(
                        (chosen == secret or chosen.startswith(secret + "/"))
                        and (relative == chosen or relative.startswith(chosen + "/")
                             or chosen.startswith(relative + "/"))
                        for chosen in explicit
                    )
                    if not opted_in:
                        warnings.append(f"Skipped secret path (opt-in required): {relative}")
                        return True
            return False

        selected = []
        for relative in sorted(set(paths)):
            if secret_excluded(relative) or is_excluded(relative):
                continue
            path = home / relative
            _assert_no_symlink_ancestors(path)
            if not os.path.lexists(path):
                warnings.append(f"Missing source path, skipped: {relative}")
            elif _kind(path.lstat().st_mode) == "special":
                warnings.append(f"Skipped special file: {relative}")
            elif not any(relative.startswith(parent + "/") for parent in selected):
                selected.append(relative)
        stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.partial-",
                                      dir=output.parent))
        payload = stage / "payload"
        payload.mkdir(mode=0o700)
        entries = {}

        def copy_entry(relative, recurse=True):
            if relative in entries or secret_excluded(relative) or is_excluded(relative):
                return
            # Fail at the offending name, not after copying everything else.
            _safe_path(relative)
            source_path, destination = home / relative, payload / relative
            _assert_no_symlink_ancestors(source_path)
            info = source_path.lstat()
            kind = _kind(info.st_mode)
            if kind == "special":
                warnings.append(f"Skipped special file: {relative}")
                return
            parent = PurePosixPath(relative).parent.as_posix()
            if parent != ".":
                copy_entry(parent, recurse=False)
            entry = {"path": relative, "type": kind,
                     "mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns}
            if kind == "directory":
                destination.mkdir(mode=0o700)
                entries[relative] = entry
                if recurse:
                    for child in sorted(source_path.iterdir()):
                        copy_entry(relative + "/" + child.name)
            elif kind == "symlink":
                entry["target"] = os.readlink(source_path)
                destination.symlink_to(entry["target"])
                if _fingerprint(source_path.lstat()) != _fingerprint(info):
                    raise BundleError(f"Source symlink changed: {source_path}")
                entries[relative] = entry
                os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns),
                         follow_symlinks=False)
            else:
                checksum = _copy_regular(source_path, destination, info)
                entry.update(size=info.st_size, sha256=checksum)
                entries[relative] = entry

        for relative in selected:
            copy_entry(relative)
        for pattern in excludes:
            paths_left_out = excluded[pattern]
            # Count each excluded tree once, not also its separately listed members.
            count = sum(not any(path.startswith(other + "/") for other in paths_left_out)
                        for path in paths_left_out)
            if count:
                note = (" (default; use --no-default-excludes to include)"
                        if pattern in defaults and pattern not in user_excludes else "")
                warnings.append(f"Excluded {count} path{'s' if count != 1 else ''} "
                                f"matching {pattern!r}{note}")
            elif pattern in user_excludes:
                warnings.append(f"Exclusion {pattern!r} matched nothing; check the path")
        # Apply directory times after copying children. Keep owner access during
        # assembly and verification; source directory modes live in the manifest.
        manifest = {
            "schema_version": 1, "tool_version": "1.0",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source, "packages": packages,
            "selected_paths": selected,
            "entries": [entries[key] for key in sorted(entries)],
            "warnings": warnings,
        }
        _write_json(stage / "manifest.json", manifest)
        verify_bundle(stage)
        if os.path.lexists(output):
            raise BundleError(f"Backup output appeared during copying: {output}")
        stage.rename(output)
        stage = None
        return manifest
    except BundleError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise BundleError(f"Could not create backup: {exc}") from exc
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def _load_manifest(bundle):
    _require_directory(bundle)
    path = bundle / "manifest.json"
    if _kind(path.lstat().st_mode) != "file":
        raise BundleError("manifest.json must be a regular file.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int:
        raise BundleError("Invalid manifest schema.")
    if manifest["schema_version"] != 1 or manifest.get("tool_version") != "1.0":
        raise BundleError("Unsupported bundle schema/tool version.")
    for key in ("source", "packages"):
        if not isinstance(manifest.get(key), dict):
            raise BundleError(f"Manifest {key} must be an object.")
    for key in ("entries", "selected_paths", "warnings"):
        if not isinstance(manifest.get(key), list):
            raise BundleError(f"Manifest {key} must be a list.")
    if not all(isinstance(value, str) for value in manifest["warnings"]):
        raise BundleError("Manifest warnings must contain strings.")
    created = manifest.get("created_at")
    if not isinstance(created, str):
        raise BundleError("Missing creation timestamp.")
    try:
        timestamp = datetime.fromisoformat(created)
        if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
            raise ValueError("not UTC")
    except ValueError as exc:
        raise BundleError("Invalid UTC creation timestamp.") from exc
    entries = {}
    for entry in manifest["entries"]:
        if not isinstance(entry, dict):
            raise BundleError("Manifest entries must be objects.")
        relative = _safe_path(entry.get("path"))
        kind = entry.get("type")
        if relative in entries or kind not in ("file", "directory", "symlink"):
            raise BundleError(f"Duplicate path or invalid type: {relative}")
        if (type(entry.get("mode")) is not int or not 0 <= entry["mode"] <= 0o7777
                or type(entry.get("mtime_ns")) is not int
                or not MIN_MTIME_NS <= entry["mtime_ns"] <= MAX_MTIME_NS):
            raise BundleError(f"Invalid file metadata: {relative}")
        if kind == "file":
            if (type(entry.get("size")) is not int or entry["size"] < 0
                    or not isinstance(entry.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise BundleError(f"Invalid checksum or size: {relative}")
        elif kind == "symlink":
            target = entry.get("target")
            if not isinstance(target, str) or not target or "\0" in target:
                raise BundleError(f"Invalid symlink target: {relative}")
        entries[relative] = entry
    for relative in entries:
        for parent in PurePosixPath(relative).parents:
            if str(parent) != "." and entries.get(str(parent), {}).get("type") != "directory":
                raise BundleError(f"Missing or non-directory ancestor: {parent}")
    selected = set()
    for relative in manifest["selected_paths"]:
        _safe_path(relative)
        if relative in selected or relative not in entries:
            raise BundleError(f"Invalid or duplicate selected path: {relative}")
        selected.add(relative)
    for relative in entries:
        if not any(relative == root or relative.startswith(root + "/")
                   or root.startswith(relative + "/") for root in selected):
            raise BundleError(f"Entry is outside selected paths: {relative}")
    return manifest, entries


def verify_bundle(bundle):
    """Check schema and the complete payload tree without following symlinks."""
    bundle = _absolute(bundle)
    try:
        manifest, entries = _load_manifest(bundle)
        payload = bundle / "payload"
        _require_directory(payload)
        actual = {}

        def walk(directory, prefix=""):
            for child in sorted(directory.iterdir()):
                relative = prefix + child.name
                info = child.lstat()
                kind = _kind(info.st_mode)
                entry = entries.get(relative)
                if entry is None or kind != entry["type"]:
                    raise BundleError(f"Unexpected payload path or type: {relative}")
                actual[relative] = True
                if kind == "directory":
                    walk(child, relative + "/")
                elif kind == "file":
                    if info.st_size != entry["size"] or _digest(child) != entry["sha256"]:
                        raise BundleError(f"Checksum/size mismatch: {relative}")
                elif kind == "symlink" and os.readlink(child) != entry["target"]:
                    raise BundleError(f"Symlink target mismatch: {relative}")

        walk(payload)
        if set(actual) != set(entries):
            raise BundleError(f"Missing payload entries: {sorted(set(entries) - set(actual))}")
        return manifest
    except BundleError:
        raise
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise BundleError(f"Invalid or unreadable bundle: {exc}") from exc


def _preflight(home, entries):
    _require_directory(home)
    _check_xdg_layout(home)
    for entry in entries:
        destination = home / entry["path"]
        _assert_no_symlink_ancestors(destination)
        if not os.path.lexists(destination):
            continue
        existing = _kind(destination.lstat().st_mode)
        incoming = entry["type"]
        if existing == "special":
            raise BundleError(f"Refusing special destination file: {destination}")
        if (incoming == "directory") != (existing == "directory"):
            raise BundleError(f"Directory/type conflict: {destination}")


def _update_journal(rollback, journal, name="journal.json"):
    temporary = rollback / ("journal-" + uuid.uuid4().hex + ".tmp")
    try:
        _write_json(temporary, journal)
        os.replace(temporary, rollback / name)
    finally:
        with contextlib.suppress(OSError):
            if os.path.lexists(temporary):
                temporary.unlink()
    _fsync_directory(rollback)


def _replaced_paths(home, entries):
    """Non-directory entries whose destination already exists, even as a dangling link."""
    return {e["path"] for e in entries
            if e["type"] != "directory" and os.path.lexists(home / e["path"])}


def _rewrite_home_in_temporary(temporary, entry, old, new):
    """Replace old-home references in a checksum-validated private temporary.

    Returns the number of references replaced. The changed content goes to a new
    private file in the same directory that is synced and then renamed over the
    temporary, so the caller applies mode and mtime afterwards as usual.
    """
    if not entry_is_rewritable(entry):
        return 0
    with open(temporary, "rb") as stream:
        data = stream.read()
    if not rewritable(entry, data):
        return 0
    changed, count = replace_home_references(data, old, new)
    if not count:
        return 0
    replacement = temporary.parent / (".fedora-migration-" + uuid.uuid4().hex)
    try:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(os.open(replacement, flags, 0o600), "wb") as writer:
            writer.write(changed)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(replacement, temporary)
    finally:
        with contextlib.suppress(OSError):
            if os.path.lexists(replacement):
                replacement.unlink()
    return count


def _validated_rewrite(rewrite_home):
    if rewrite_home is None:
        return None
    old, new = (os.fspath(value) for value in rewrite_home)
    if not all(isinstance(v, str) and os.path.isabs(v) and "\0" not in v for v in (old, new)):
        raise BundleError("Home rewrite needs two absolute paths.")
    old, new = old.rstrip("/"), new.rstrip("/")
    if not old or not new:
        raise BundleError("Home rewrite refuses the filesystem root.")
    return None if old == new else (old, new)


def _plan(bundle, home, manifest, backup_dir):
    """Validate destination and rollback paths without changing anything."""
    entries = manifest["entries"]
    _preflight(home, entries)
    summary = {
        "files": sum(e["type"] == "file" for e in entries),
        "directories": sum(e["type"] == "directory" for e in entries),
        "symlinks": sum(e["type"] == "symlink" for e in entries),
        "selected_paths": list(manifest["selected_paths"]),
        "backup_dir": None, "warnings": list(manifest["warnings"]),
        "rewritten_count": 0,
    }
    replaced = len(_replaced_paths(home, entries))
    summary["replace_count"] = replaced
    summary["new_count"] = summary["files"] + summary["symlinks"] - replaced
    candidate = (_absolute(backup_dir) if backup_dir is not None else
                 home / ".local/state/fedora-xfce-migration" /
                 ("restore-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                  + "-" + uuid.uuid4().hex[:12]))
    _assert_no_symlink_ancestors(candidate)
    if os.path.lexists(candidate):
        raise BundleError(f"Rollback directory must be a new path: {candidate}")
    if _within(candidate, bundle) or _within(bundle, candidate):
        raise BundleError("Rollback directory must not overlap the bundle.")
    for entry in entries:
        destination = home / entry["path"]
        if (_within(destination, candidate)
                or (entry["type"] != "directory" and _within(candidate, destination))):
            raise BundleError(f"Rollback path conflicts with restored path: {destination}")
        if _within(bundle, destination) and entry["type"] != "directory":
            raise BundleError(f"Restore would replace its bundle: {destination}")
        if _within(destination, bundle):
            raise BundleError(f"Restore destination overlaps its bundle: {destination}")
    return summary, candidate


def plan_restore(bundle, home, manifest, *, backup_dir=None):
    """Preview a restore using a manifest that verify_bundle() returned for bundle.

    This avoids re-reading the payload for a preview. restore_bundle() with
    dry_run=False always verifies the bundle again before changing anything.
    """
    bundle, home = _absolute(bundle), _absolute(home)
    try:
        return _plan(bundle, home, manifest, backup_dir)[0]
    except BundleError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise BundleError(f"Restore failed before copying files: {exc}") from exc


@_owner_accessible
def restore_bundle(bundle, home, *, dry_run=True, backup_dir=None, before_write=None,
                   rewrite_home=None):
    """Preview or overlay a validated bundle, retaining originals and a journal.

    rewrite_home=(old, new) replaces old-home path references with new in the
    small settings files report.entry_is_rewritable() selects. It happens only
    after each restored file's checksum has been validated, and the summary's
    rewritten_count says how many files changed. A preview never rewrites.

    before_write, if given, runs after verification and destination checks and
    immediately before the first change; an exception from it prevents all writes.
    """
    bundle, home = _absolute(bundle), _absolute(home)
    rollback = None
    staging = None
    journal = None
    try:
        rewrite_home = _validated_rewrite(rewrite_home)
        manifest = verify_bundle(bundle)
        entries = manifest["entries"]
        summary, candidate = _plan(bundle, home, manifest, backup_dir)
        if dry_run:
            return summary
        if before_write is not None:
            before_write()
        directories = sorted((e for e in entries if e["type"] == "directory"),
                             key=lambda e: (e["path"].count("/"), e["path"]))
        planned_replaced = _replaced_paths(home, entries)
        created = {e["path"] for e in directories
                   if not os.path.lexists(home / e["path"])}
        selected = set(manifest["selected_paths"])
        original_directories = []
        for entry in directories:
            if entry["path"] in selected and entry["path"] not in created:
                info = (home / entry["path"]).lstat()
                original_directories.append({"path": entry["path"],
                                             "mode": stat.S_IMODE(info.st_mode),
                                             "mtime_ns": info.st_mtime_ns})
        # Persist the full intent plan once before any selected path changes.
        # The plan identifies potentially changed paths after interruption;
        # previous/ contains the actual saved originals. Avoid rewriting growing
        # arrays per file, which makes profile restores quadratic in file count.
        journal = {
            "status": "in_progress", "kind": "intent_plan",
            "bundle": str(bundle), "home": str(home),
            "replaced": sorted(planned_replaced),
            "new": sorted(e["path"] for e in entries
                          if e["type"] != "directory" and e["path"] not in planned_replaced),
            "created_directories": sorted(created),
            "original_directories": original_directories,
            # What the restore may have changed in eligible files after their
            # checksum check, so a rollback can tell them from user edits.
            "rewrite_home": list(rewrite_home) if rewrite_home else None,
        }
        _private_parents(candidate)
        rollback = candidate
        previous = rollback / "previous"
        previous.mkdir(mode=0o700)
        # Recovery copies are assembled here and renamed into previous/ only
        # when complete, so previous/ never holds a partial original.
        staging = rollback / "staging"
        staging.mkdir(mode=0o700)
        _fsync_directory(rollback)
        summary["backup_dir"] = str(rollback)
        _update_journal(rollback, journal)
        for entry in directories:
            destination = home / entry["path"]
            _assert_no_symlink_ancestors(destination, include_leaf=True)
            if not destination.exists():
                destination.mkdir(mode=0o700)
        for entry in entries:
            if entry["type"] == "directory":
                continue
            relative = entry["path"]
            destination = home / relative
            _assert_no_symlink_ancestors(destination)
            _preflight(home, [entry])
            exists = os.path.lexists(destination)
            if exists != (relative in planned_replaced):
                raise BundleError(f"Destination changed after preflight: {destination}")
            if exists:
                _save_original(destination, previous / relative, staging)
            temporary = destination.parent / (".fedora-migration-" + uuid.uuid4().hex)
            try:
                if entry["type"] == "file":
                    # Stays 0600 through checksum validation: the payload copy's
                    # own mode may be wider than the manifest after a transfer.
                    _copy_regular(bundle / "payload" / relative, temporary,
                                  preserve_metadata=False)
                    if _digest(temporary) != entry["sha256"]:
                        raise BundleError(f"Payload changed during restore: {relative}")
                    if rewrite_home and _rewrite_home_in_temporary(
                            temporary, entry, *rewrite_home):
                        summary["rewritten_count"] += 1
                    temporary.chmod(entry["mode"])
                else:
                    temporary.symlink_to(entry["target"])
                os.utime(temporary, ns=(entry["mtime_ns"], entry["mtime_ns"]),
                         follow_symlinks=False)
                os.replace(temporary, destination)
            finally:
                with contextlib.suppress(OSError):
                    if os.path.lexists(temporary):
                        temporary.unlink()
        # Preserve selected directory roots and new directories. Existing
        # structural HOME parents and unrelated contents keep their metadata.
        for entry in reversed(directories):
            if entry["path"] in created or entry["path"] in selected:
                destination = home / entry["path"]
                destination.chmod(entry["mode"])
                os.utime(destination, ns=(entry["mtime_ns"], entry["mtime_ns"]))
        journal["status"] = "complete"
        _update_journal(rollback, journal)
        # Every file is restored; a leftover staging entry must not turn a
        # completed restore into a failure.
        try:
            staging.rmdir()
        except OSError as exc:
            summary["warnings"].append(f"Could not remove {staging}: {exc}")
        return summary
    except (BundleError, OSError, ValueError, TypeError, OverflowError, KeyboardInterrupt) as exc:
        if staging is not None:
            try:
                staging.rmdir()
            except OSError:
                pass
        if rollback is not None:
            if journal is not None:
                journal["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
                journal["error"] = str(exc) or "Interrupted by user"
                try:
                    _update_journal(rollback, journal)
                except OSError:
                    pass
            if isinstance(exc, KeyboardInterrupt):
                raise BundleInterrupted(
                    f"Restore interrupted. Retained rollback directory: {rollback}. "
                    "Read journal.json and inspect previous/ before retrying."
                ) from exc
            raise BundleError(
                f"Restore failed: {exc}. Retained rollback directory: {rollback}"
            ) from exc
        if isinstance(exc, BundleError):
            raise
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise BundleError(f"Restore failed before copying files: {exc}") from exc
