"""Undo a restore from its rollback directory without losing anything it can't prove.

A restore leaves journal.json (what it planned to replace, create and add) and
previous/ (complete copies of the originals it saved). Rollback puts those
originals back and removes what the restore added, but only where the
destination still looks like the restore left it. Anything else is a conflict
that is reported and, unless the caller opts in, left alone. Before any
overwrite or removal the current destination is saved under undone-<time>/, so a
rollback never loses data either.

The rollback directory and the destination home must be controlled by the user
and quiescent while this runs, like the restore itself.

Threat model: rollback assumes the home is quiescent, which the CLI checks (no
XFCE session, no browser, mail or Flatpak applications). It never overwrites or
deletes an entry it has not captured: a destination's parent is pinned by file
descriptor, walked from "/" without following symlinks; the current entry is
    captured by rename before anything is published (and retained beside the
    destination as well as copied out when undone-<time>/ is across filesystems); the
original is published with a hard link, which fails instead of replacing. A
concurrent writer can therefore at most make one path fail, with both versions
kept. Writes inside the rollback directory itself use ordinary paths: it is the
user's own directory, and a swapped ancestor there can misplace saved copies but
never delete anything outside it.
"""

import contextlib
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import uuid

from .bundle import (
    BundleError, BundleInterrupted, MIN_MTIME_NS, MAX_MTIME_NS,
    _absolute, _assert_no_symlink_ancestors,
    _digest, _fingerprint, _fsync_directory, _kind, _owner_accessible,
    _private_parents, _safe_path, _update_journal, _within,
    _within_secret, verify_bundle,
)
from .report import entry_is_rewritable, replace_home_references, rewritable

JOURNAL = "journal.json"
ROLLBACK_JOURNAL = "rollback-journal.json"
QUARANTINE_RECORDS = "quarantine-records"
RESTORE_STATUSES = ("in_progress", "complete", "failed", "interrupted")
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
# Two relative Linux paths, including JSON escaping, still fit this bounded
# record. Ordinary records are a few hundred bytes; never serialize the full
# growing action/recovery set for an individual capture.
MAX_QUARANTINE_RECORD_BYTES = 64 * 1024


def _read_json_file(path, what, *, max_bytes=MAX_JOURNAL_BYTES):
    """Read a regular, bounded JSON file without following a symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise BundleError(f"No {what} found: {path}") from None
    if _kind(info.st_mode) != "file":
        raise BundleError(f"The {what} must be a regular file: {path}")
    if info.st_size > max_bytes:
        raise BundleError(f"The {what} is implausibly large: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(f"The {what} is unreadable: {path}: {exc}") from exc


def _absolute_string(value, what):
    if not isinstance(value, str) or not os.path.isabs(value) or "\0" in value:
        raise BundleError(f"Invalid journal {what}: {value!r}")
    return value


def _path_list(journal, key):
    values = journal.get(key)
    if not isinstance(values, list):
        raise BundleError(f"Invalid journal: {key} must be a list.")
    for value in values:
        _safe_path(value)
    if len(set(values)) != len(values):
        raise BundleError(f"Invalid journal: duplicate path in {key}.")
    return values


def _load_journal(rollback):
    """Validate journal.json's shape and return it with its rewrite pair."""
    journal = _read_json_file(rollback / JOURNAL, "restore journal")
    if not isinstance(journal, dict) or journal.get("kind") != "intent_plan":
        raise BundleError("This is not a restore journal this tool can roll back "
                          "(expected kind intent_plan).")
    if journal.get("status") not in RESTORE_STATUSES:
        raise BundleError(f"Invalid journal status: {journal.get('status')!r}")
    _absolute_string(journal.get("home"), "home")
    _absolute_string(journal.get("bundle"), "bundle")
    replaced = _path_list(journal, "replaced")
    new = _path_list(journal, "new")
    directories = _path_list(journal, "created_directories")
    if set(replaced) & set(new) or set(directories) & (set(replaced) | set(new)):
        raise BundleError("Invalid journal: a path is listed in more than one group.")
    if "original_directories" in journal:
        metadata = journal["original_directories"]
        if not isinstance(metadata, list):
            raise BundleError("Invalid journal: original_directories must be a list.")
        seen = set(replaced) | set(new) | set(directories)
        for item in metadata:
            if not isinstance(item, dict):
                raise BundleError("Invalid journal: original directory metadata must be an object.")
            path = _safe_path(item.get("path"))
            if path in seen:
                raise BundleError(f"Invalid journal: duplicate original directory path: {path}")
            seen.add(path)
            if (type(item.get("mode")) is not int or not 0 <= item["mode"] <= 0o7777
                    or type(item.get("mtime_ns")) is not int
                    or not MIN_MTIME_NS <= item["mtime_ns"] <= MAX_MTIME_NS):
                raise BundleError(f"Invalid journal: original directory metadata for {path}")
    rewrite = journal.get("rewrite_home")
    if rewrite is not None:
        if (not isinstance(rewrite, list) or len(rewrite) != 2
                or not all(isinstance(value, str) for value in rewrite)):
            raise BundleError("Invalid journal: rewrite_home must be null or two strings.")
        # Historical restores allowed names that the new-write validator now
        # refuses across XML/desktop/shell formats. This pair only reproduces
        # their already-written bytes for comparison; it never authorizes a
        # new home rewrite. Preserve the former structural checks here.
        rewrite = tuple(_absolute_string(value, "rewrite_home path").rstrip("/")
                        for value in rewrite)
        if not all(rewrite):
            raise BundleError("Journal home rewrite refuses the filesystem root.")
        if rewrite[0] == rewrite[1]:
            rewrite = None
    return journal, rewrite


def _refuse_if_rolled_back(rollback):
    path = rollback / ROLLBACK_JOURNAL
    if not os.path.lexists(path):
        return {}
    previous = _read_json_file(path, "rollback journal")
    status = previous.get("status") if isinstance(previous, dict) else None
    if status == "complete":
        raise BundleError(f"This restore was already rolled back (see {path}).")
    if not isinstance(status, str):
        raise BundleError(f"The rollback journal is unreadable: {path}")
    return previous


def _retained_quarantines(previous, home):
    """Validate recovery references before carrying them into a retry's journal."""
    records = previous.get("retained_quarantines", [])
    if not isinstance(records, list):
        raise BundleError("Invalid rollback journal: retained_quarantines must be a list.")
    if records and previous.get("home") != str(home):
        raise BundleError("Invalid rollback journal: quarantines belong to another home.")
    seen = set()
    for item in records:
        if not isinstance(item, dict):
            raise BundleError("Invalid rollback journal: quarantine reference must be an object.")
        source = PurePosixPath(_safe_path(item.get("path")))
        path = PurePosixPath(_safe_path(item.get("quarantine")))
        name = path.parent.name
        suffix = name.removeprefix(".fedora-migration-quarantine-")
        if (path.name != "entry" or path.parent.parent != source.parent
                or not name.startswith(".fedora-migration-quarantine-")
                or len(suffix) != 32 or any(c not in "0123456789abcdef" for c in suffix)
                or str(path) in seen):
            raise BundleError(f"Invalid rollback journal quarantine: {path}")
        seen.add(str(path))
    return records


def _load_quarantine_records(rollback, previous, home):
    """Merge bounded durable capture records with an older or lagging summary."""
    records = list(_retained_quarantines(previous, home))
    directory = rollback / QUARANTINE_RECORDS
    _assert_no_symlink_ancestors(directory, include_leaf=True)
    if not os.path.lexists(directory):
        return records
    if not directory.is_dir():
        raise BundleError(f"Quarantine recovery records must be a directory: {directory}")
    by_path = {item["quarantine"]: item for item in records}
    for path in sorted(directory.iterdir()):
        # A crash before an atomic record publication may leave a partial temp;
        # the capture is not allowed to rename its source until publication ends.
        if path.name.startswith("journal-") and path.suffix == ".tmp":
            continue
        identifier = path.stem
        if (path.suffix != ".json" or len(identifier) != 32
                or any(c not in "0123456789abcdef" for c in identifier)):
            raise BundleError(f"Invalid quarantine recovery record filename: {path}")
        record = _read_json_file(path, "quarantine recovery record",
                                 max_bytes=MAX_QUARANTINE_RECORD_BYTES)
        _retained_quarantines({"home": previous.get("home", str(home)),
                               "retained_quarantines": [record]}, home)
        if PurePosixPath(record["quarantine"]).parent.name != (
                ".fedora-migration-quarantine-" + identifier):
            raise BundleError(f"Quarantine recovery record does not match its filename: {path}")
        earlier = by_path.get(record["quarantine"])
        if earlier is not None:
            if earlier != record:
                raise BundleError(f"Conflicting quarantine recovery records: {path}")
        else:
            records.append(record)
            by_path[record["quarantine"]] = record
    return records


def _verified_manifest(bundle):
    """Return (entries by path, None) or (None, why the bundle can't be used)."""
    try:
        manifest = verify_bundle(bundle)
    except (BundleError, OSError) as exc:
        return None, str(exc)
    return {entry["path"]: entry for entry in manifest["entries"]}, None


def _same_saved_content(current, saved):
    """Whether current is already the saved original: content, mode and mtime.

    Equal bytes alone are not enough: a put-back also restores the original's
    mode and timestamp, so those must match too.
    """
    current_info, saved_info = current.lstat(), saved.lstat()
    if current_info.st_mtime_ns != saved_info.st_mtime_ns:
        return False
    if stat.S_ISLNK(current_info.st_mode) and stat.S_ISLNK(saved_info.st_mode):
        return os.readlink(current) == os.readlink(saved)
    if stat.S_ISREG(current_info.st_mode) and stat.S_ISREG(saved_info.st_mode):
        return (stat.S_IMODE(current_info.st_mode) == stat.S_IMODE(saved_info.st_mode)
                and current_info.st_size == saved_info.st_size
                and _digest(current) == _digest(saved))
    return False


def _is_as_restored(destination, info, entry, bundle, rewrite):
    """Whether an existing destination holds what the restore wrote for entry."""
    if entry["type"] == "symlink":
        return stat.S_ISLNK(info.st_mode) and os.readlink(destination) == entry["target"]
    if entry["type"] != "file" or not stat.S_ISREG(info.st_mode):
        return False
    if rewrite is not None and entry_is_rewritable(entry):
        payload = (bundle / "payload" / entry["path"]).read_bytes()
        if rewritable(entry, payload):
            expected = replace_home_references(payload, *rewrite)[0]
            return info.st_size == len(expected) and destination.read_bytes() == expected
    return info.st_size == entry["size"] and _digest(destination) == entry["sha256"]


def _conflict(path, action, reason, blocked=False):
    return {"path": path, "action": action, "reason": reason, "blocked": blocked}


def _directory_forecast(home, directories, removals):
    """Which created directories would be empty (and so removed) after the file removals."""
    existing, kept, removed = [], [], []
    gone = set(removals)
    for relative in sorted(directories, key=lambda value: (-value.count("/"), value)):
        path = home / relative
        _assert_no_symlink_ancestors(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            kept.append({"path": relative, "reason": "is no longer a directory"})
            continue
        existing.append(relative)
        children = [f"{relative}/{name}" for name in os.listdir(path)]
        if all(child in gone for child in children):
            removed.append(relative)
            gone.add(relative)
        else:
            kept.append({"path": relative, "reason": "not empty"})
    return existing, removed, kept


def _plan_rollback(rollback_dir, home, bundle, include_changed):
    rollback, home = _absolute(rollback_dir), _absolute(home)
    _assert_no_symlink_ancestors(rollback, include_leaf=True)
    if not rollback.is_dir():
        raise BundleError(f"Rollback directory does not exist: {rollback}")
    journal, rewrite = _load_journal(rollback)
    original_directories = journal.get("original_directories", [])
    warnings = ([] if "original_directories" in journal else [
        "This older restore journal has no original directory metadata; existing selected "
        "directory permissions and timestamps cannot be restored. Check them by hand."])
    if _absolute(journal["home"]) != home:
        raise BundleError(
            f"This rollback directory belongs to home {journal['home']}, not {home}. "
            "Refusing to change another home.")
    previous_rollback = _refuse_if_rolled_back(rollback)
    retained_quarantines = _load_quarantine_records(rollback, previous_rollback, home)
    bundle_path = _absolute(bundle if bundle is not None else journal["bundle"])
    entries, problem = _verified_manifest(bundle_path)
    previous = rollback / "previous"

    put_back, remove, unchanged, absent, conflicts = [], [], [], [], []
    everything = journal["replaced"] + journal["new"]
    # Compare canonical as well as lexical paths, so an alias of the bundle or
    # the rollback directory cannot hide an overlap.
    rollbacks = {rollback, Path(os.path.realpath(rollback))}
    bundles = {bundle_path, Path(os.path.realpath(bundle_path))}

    def canonical(destination):
        return Path(os.path.realpath(destination.parent)) / destination.name

    for relative in everything:
        destination = home / relative
        for candidate in (destination, canonical(destination)):
            if any(_within(candidate, protected) for protected in rollbacks | bundles):
                raise BundleError(
                    f"Journal path {relative} lies inside the rollback directory or the bundle.")
        _assert_no_symlink_ancestors(destination)
    original_paths = {item["path"] for item in original_directories}
    for relative in journal["created_directories"] + sorted(original_paths):
        # A created directory may legitimately contain the rollback directory
        # (the restore made it first), so only the other overlaps are refused.
        directory = home / relative
        for candidate in (directory, canonical(directory)):
            if any(_within(candidate, protected) for protected in rollbacks | bundles) or any(
                    _within(protected, candidate) for protected in bundles):
                raise BundleError(
                    f"Journal directory {relative} overlaps the rollback directory or the bundle.")
        if relative in original_paths:
            _assert_no_symlink_ancestors(directory, include_leaf=True)
            if os.path.lexists(directory) and not directory.is_dir():
                raise BundleError(f"Original directory is no longer a directory: {directory}")

    for relative in journal["replaced"]:
        saved = previous / relative
        _assert_no_symlink_ancestors(saved)
        if not os.path.lexists(saved):
            # The restore never got as far as replacing this file.
            unchanged.append(relative)
            continue
        if _kind(saved.lstat().st_mode) not in ("file", "symlink"):
            raise BundleError(f"Saved original is not a file or symlink: {saved}")
        state = _classify(home / relative, relative, entries, problem, bundle_path, rewrite,
                          "put_back", saved=saved)
        if state == "missing" or state == "restored":
            put_back.append(relative)
        elif state == "same":
            unchanged.append(relative)
        else:
            conflicts.append(_conflict(relative, "put_back", *state))
    for relative in journal["new"]:
        state = _classify(home / relative, relative, entries, problem, bundle_path, rewrite,
                          "remove")
        if state == "missing":
            absent.append(relative)
        elif state == "restored":
            remove.append(relative)
        else:
            conflicts.append(_conflict(relative, "remove", *state))

    if include_changed:
        for item in conflicts:
            if not item["blocked"]:
                (put_back if item["action"] == "put_back" else remove).append(item["path"])
    put_back.sort()
    remove.sort()
    existing, removable, kept = _directory_forecast(home, journal["created_directories"], remove)
    restore_metadata = []
    for original in original_directories:
        relative = original["path"]
        reason = None
        try:
            info = (home / relative).lstat()
        except FileNotFoundError:
            pass  # recreate an original empty directory, like a missing saved file
        else:
            current = (stat.S_IMODE(info.st_mode), info.st_mtime_ns)
            if current != (original["mode"], original["mtime_ns"]):
                if entries is None:
                    reason = f"cannot verify directory metadata: the bundle is unavailable ({problem})"
                else:
                    entry = entries.get(relative)
                    if (entry is None or entry["type"] != "directory"
                            or current != (entry["mode"], entry["mtime_ns"])):
                        reason = ("directory metadata differs from what the restore wrote "
                                  "(changed since the restore)")
        if reason:
            conflicts.append(_conflict(relative, "restore_directory_metadata", reason))
        if reason is None or include_changed:
            restore_metadata.append(original)
    return {
        "rollback_dir": str(rollback), "home": str(home), "status": journal["status"],
        "bundle": str(bundle_path), "verified": problem is None, "bundle_problem": problem,
        "rewrite_home": rewrite, "include_changed": include_changed,
        "put_back": put_back, "remove": remove, "unchanged": sorted(unchanged),
        "absent": sorted(absent), "conflicts": conflicts,
        "directories": existing, "remove_directories": removable, "kept_directories": kept,
        "original_directories": original_directories, "warnings": warnings,
        "restore_directory_metadata": restore_metadata,
        "retained_quarantines": retained_quarantines,
        "secrets": any(_within_secret(path) for path in
                       everything + journal["created_directories"] + sorted(original_paths)),
    }


def _classify(destination, relative, entries, problem, bundle, rewrite, action, saved=None):
    """Return "missing", "restored", "same", or (reason, blocked) for a conflict."""
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return "missing"
    kind = _kind(info.st_mode)
    if kind not in ("file", "symlink"):
        return (f"is now a {kind}; rollback never replaces or removes it", True)
    if saved is not None and _same_saved_content(destination, saved):
        return "same"
    if entries is None:
        return (f"cannot verify: the bundle is unavailable ({problem})", False)
    entry = entries.get(relative)
    if entry is None:
        return ("cannot verify: the path is not in the bundle", False)
    if _is_as_restored(destination, info, entry, bundle, rewrite):
        return "restored"
    return ("differs from what the restore wrote (changed since the restore)", False)


def plan_rollback(rollback_dir, home, *, bundle=None, include_changed=False):
    """Validate a rollback directory and classify every path; changes nothing."""
    try:
        return _plan_rollback(rollback_dir, home, bundle, include_changed)
    except BundleError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise BundleError(f"Rollback failed before changing files: {exc}") from exc


_DIRECTORY_FLAGS = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0))
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)


def _open_parent(home, relative, *, create=False):
    """Open the directory holding relative, walking from "/" without following links.

    home must be a real absolute path (no symlink in any component). Returns a
    descriptor, or None if a parent below home is missing and create is false.
    The caller closes it. Every later operation uses names relative to it, so
    swapping any path component for a symlink afterwards cannot redirect them.
    """
    home = Path(home)
    if not home.is_absolute() or os.path.realpath(home) != str(home):
        raise BundleError(
            f"The home directory must be an absolute path without symlinks: {home}")
    names = home.parts[1:] + PurePosixPath(relative).parent.parts
    below_home = len(home.parts) - 1
    owned = [os.open("/", _DIRECTORY_FLAGS)]
    try:
        for index, name in enumerate(names):
            try:
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=owned[0])
            except FileNotFoundError:
                if index < below_home:
                    raise BundleError(f"The home directory does not exist: {home}") from None
                if not create:
                    return None
                os.mkdir(name, 0o700, dir_fd=owned[0])
                os.fsync(owned[0])
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=owned[0])
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise BundleError(
                        f"Refusing symlink or non-directory ancestor {name!r} of "
                        f"{home / relative}") from exc
                raise
            owned.append(child)
            # Ownership of the parent ends before it is closed, so an interrupt
            # around the close can neither leak the child nor close a stale number.
            os.close(owned.pop(0))
        return owned.pop()
    finally:
        for descriptor in owned:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _pin(home, relative, *, create=False):
    """Pin relative's parent directory and confirm the path still names that directory."""
    descriptor = _open_parent(home, relative, create=create)
    if descriptor is None:
        return None
    try:
        pinned = os.fstat(descriptor)
        named = os.stat((home / relative).parent, follow_symlinks=False)
        if (not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)):
            raise BundleError(f"A parent directory of {home / relative} changed during rollback.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stage_saved(saved, descriptor):
    """Copy a saved original to a private temporary inside the pinned directory."""
    name = ".fedora-migration-" + uuid.uuid4().hex
    try:
        if saved.is_symlink():
            info = saved.lstat()
            os.symlink(os.readlink(saved), name, dir_fd=descriptor)
            os.utime(name, ns=(info.st_atime_ns, info.st_mtime_ns), dir_fd=descriptor,
                     follow_symlinks=False)
        else:
            with os.fdopen(os.open(saved, _FILE_FLAGS), "rb") as reader:
                before = os.fstat(reader.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise BundleError(f"Saved original is not a regular file: {saved}")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(
                    os, "O_CLOEXEC", 0)
                with os.fdopen(os.open(name, flags, 0o600, dir_fd=descriptor), "wb") as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                    writer.flush()
                    os.fchmod(writer.fileno(), stat.S_IMODE(before.st_mode))
                    os.utime(writer.fileno(), ns=(before.st_atime_ns, before.st_mtime_ns))
                    os.fsync(writer.fileno())
                if _fingerprint(before) != _fingerprint(os.fstat(reader.fileno())):
                    raise BundleError(f"Saved original changed while copying: {saved}")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=descriptor)
        raise
    return name


def _link_no_replace(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
    """Hard-link source to destination; fail instead of replacing an existing entry.

    Python has no renameat2(RENAME_NOREPLACE). linkat fails with EEXIST where
    rename would silently overwrite, and with follow_symlinks=False it links a
    symlink itself. Raises FileExistsError if destination exists, BundleError if
    the filesystem cannot hard-link (never falling back to a replacing rename).
    """
    try:
        os.link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
                follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV):
            raise BundleError(
                f"This filesystem cannot hard-link {destination!r} ({exc.strerror}), which "
                "rollback needs to publish a file without overwriting; restore this path "
                "by hand from previous/.") from exc
        raise


def _copy_entry_out(descriptor, leaf, destination, staging):
    """Copy the entry named leaf to destination (staged, synced, never partial)."""
    temporary = staging / uuid.uuid4().hex
    try:
        info = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            temporary.symlink_to(os.readlink(leaf, dir_fd=descriptor))
            os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)
        else:
            with os.fdopen(os.open(leaf, _FILE_FLAGS, dir_fd=descriptor), "rb") as reader:
                before = os.fstat(reader.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise BundleError(f"Not a regular file: {leaf}")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(
                    os, "O_CLOEXEC", 0)
                with os.fdopen(os.open(temporary, flags, 0o600), "wb") as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                    writer.flush()
                    os.fchmod(writer.fileno(), stat.S_IMODE(before.st_mode))
                    os.utime(writer.fileno(), ns=(before.st_atime_ns, before.st_mtime_ns))
                    os.fsync(writer.fileno())
                if _fingerprint(before) != _fingerprint(os.fstat(reader.fileno())):
                    raise BundleError(f"{leaf} changed while it was being saved.")
        _private_parents(destination.parent)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        with contextlib.suppress(OSError):
            if os.path.lexists(temporary):
                temporary.unlink()


def _restore_by_link(source, leaf, where, *, src_dir_fd, dst_dir_fd):
    """Link a captured entry back to leaf without replacing; name where it is otherwise kept."""
    try:
        _link_no_replace(source, leaf, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
    except FileExistsError:
        raise BundleError(
            f"{leaf} was created by someone else while rolling back and was left untouched; "
            f"the entry that was there before is kept in {where}.") from None
    os.unlink(source, dir_fd=src_dir_fd)


def _capture_across_devices(descriptor, leaf, destination, staging, on_quarantine=None):
    """Capture by quarantine when undone/ is on another filesystem.

    The entry is renamed (atomically, same filesystem) into a private directory
    next to it, then copied out. Keep the captured inode: a writer with an open
    descriptor can still edit it after the copy, even after rollback returns.
    on_quarantine journals its location before the rename. Only returning that
    same inode to leaf permits removing its recovery reference.
    """
    name = ".fedora-migration-quarantine-" + uuid.uuid4().hex
    os.mkdir(name, 0o700, dir_fd=descriptor)
    where = f"{name}/entry beside {leaf}"
    quarantine = None
    release = None
    moved = False

    def restore():
        try:
            recovery = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                _restore_by_link("entry", leaf, where, src_dir_fd=recovery, dst_dir_fd=descriptor)
            finally:
                os.close(recovery)
            os.rmdir(name, dir_fd=descriptor)
            os.fsync(descriptor)
        except (BundleError, OSError) as exc:
            raise BundleError(f"{exc}; the captured original is retained at {where}.") from exc
        if release is not None:
            release()

    try:
        quarantine = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
        if on_quarantine is not None:
            where, release = on_quarantine(name)
        os.rename(leaf, "entry", src_dir_fd=descriptor, dst_dir_fd=quarantine)
        moved = True
        os.fsync(quarantine)
        os.fsync(descriptor)
        try:
            _copy_entry_out(quarantine, "entry", destination, staging)
        except BaseException as exc:
            try:
                restore()
            except (BundleError, OSError) as problem:
                if not isinstance(exc, KeyboardInterrupt):
                    raise BundleError(f"{exc}; {problem}") from exc
            raise
    except BaseException:
        if not moved:
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=descriptor)
            if release is not None:
                release()
        raise
    finally:
        if quarantine is not None:
            os.close(quarantine)
    return restore, lambda: None


def _capture(descriptor, leaf, destination, staging, on_quarantine=None):
    """Move the current entry into the undone directory; return (restore, finish).

    A rename moves the exact entry, so no edit can fall between saving and
    removing it. Across filesystems a quarantine does the same. restore() puts
    the entry back without replacing anything; finish() completes the capture.
    """
    if os.path.lexists(destination):
        raise BundleError(f"Undone copy already exists: {destination}")
    _private_parents(destination.parent)
    try:
        os.rename(leaf, destination, src_dir_fd=descriptor)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        return _capture_across_devices(descriptor, leaf, destination, staging, on_quarantine)
    _fsync_directory(destination.parent)
    os.fsync(descriptor)
    return (lambda: _restore_by_link(os.fspath(destination), leaf, destination,
                                     src_dir_fd=None, dst_dir_fd=descriptor),
            lambda: None)


def _check_hard_links(descriptor, staged, destination):
    """Refuse a path early, before touching it, if hard links are unavailable."""
    probe = ".fedora-migration-" + uuid.uuid4().hex
    _link_no_replace(staged, probe, src_dir_fd=descriptor, dst_dir_fd=descriptor)
    os.unlink(probe, dir_fd=descriptor)


def _apply_action(home, relative, action, rollback, undone, staging, retain_quarantine=None):
    """Put back or remove one path, saving the current entry under undone/ first."""
    destination = home / relative
    leaf = destination.name
    descriptor = _pin(home, relative, create=action == "put_back")
    if descriptor is None:
        return  # the parent is gone, so there is nothing to remove
    staged = None
    restore = None
    try:
        try:
            info = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and _kind(info.st_mode) not in ("file", "symlink"):
            raise BundleError(f"Destination is no longer a file or symlink: {destination}")
        saved = rollback / "previous" / relative
        if action == "put_back":
            if not os.path.lexists(saved):
                raise BundleError(f"Saved original disappeared: {saved}")
            staged = _stage_saved(saved, descriptor)
            _check_hard_links(descriptor, staged, destination)
        finish = None
        if info is not None:
            callback = ((lambda name: retain_quarantine(relative, name))
                        if retain_quarantine is not None else None)
            restore, finish = _capture(descriptor, leaf, undone / relative, staging, callback)
        if action == "put_back":
            try:
                _link_no_replace(staged, leaf, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            except FileExistsError:
                raise BundleError(
                    f"{destination} was created by someone else while rolling back and was "
                    "left untouched; the entry that was there before is kept in "
                    f"{undone / relative if restore else 'nowhere (it did not exist)'}.") from None
            os.unlink(staged, dir_fd=descriptor)
            staged = None
        if finish is not None:
            finish()
            restore = None
        os.fsync(descriptor)
    except BaseException as exc:
        if restore is not None and action == "put_back":
            # Leave the user's entry where it was, never over anything new.
            try:
                restore()
            except (BundleError, OSError) as problem:
                if not isinstance(exc, KeyboardInterrupt) and str(problem) not in str(exc):
                    raise BundleError(f"{exc} Also: {problem}") from exc
        raise
    finally:
        if staged is not None:
            with contextlib.suppress(OSError):
                os.unlink(staged, dir_fd=descriptor)
        os.close(descriptor)


def _holds_copies(directory):
    """Whether directory holds any saved file or symlink, not just empty folders."""
    for root, folders, names in os.walk(directory):
        if names or any(os.path.islink(os.path.join(root, name)) for name in folders):
            return True
    return False


def _open_directory(home, relative, *, create=False, identity=None):
    """Open a managed directory itself without following a replaced parent or leaf."""
    parent = _pin(home, relative, create=create)
    if parent is None:
        return None
    try:
        leaf = PurePosixPath(relative).name
        try:
            descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                return None
            os.mkdir(leaf, 0o700, dir_fd=parent)
            os.fsync(parent)
            descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise BundleError(f"Refusing symlink or non-directory: {home / relative}") from exc
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise BundleError(
                    f"Cannot open managed directory {home / relative}; grant owner read and "
                    "execute access by hand before retrying rollback.") from exc
            raise
        try:
            info = os.fstat(descriptor)
            if identity is not None and (info.st_dev, info.st_ino) != identity:
                raise BundleError(f"Managed directory changed during rollback: {home / relative}")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(parent)


def _prepare_directory_access(home, paths, changes):
    """Temporarily grant owner access so restored read-only directories can be undone."""
    for relative in sorted(set(paths), key=lambda value: (value.count("/"), value)):
        descriptor = _open_directory(home, relative)
        if descriptor is None:
            continue
        try:
            info = os.fstat(descriptor)
            mode = stat.S_IMODE(info.st_mode)
            if mode & 0o700 != 0o700:
                changes.append((relative, mode, (info.st_dev, info.st_ino)))
                os.fchmod(descriptor, mode | 0o700)
                os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _restore_directory_access(home, changes, preserve=()):
    """Reset temporary modes, refusing to chmod an entry substituted in the meantime."""
    for item in list(reversed(changes)):
        relative, mode, identity = item
        if relative in preserve:
            continue
        descriptor = _open_directory(home, relative, identity=identity)
        if descriptor is not None:
            try:
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        changes.remove(item)


def _restore_directory_metadata(home, metadata):
    """Restore original selected-root metadata after all directory-entry changes."""
    for item in sorted(metadata, key=lambda value: (-value["path"].count("/"), value["path"])):
        descriptor = _open_directory(home, item["path"], create=True)
        try:
            info = os.fstat(descriptor)
            os.fchmod(descriptor, item["mode"])
            os.utime(descriptor, ns=(info.st_atime_ns, item["mtime_ns"]))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _remove_empty_directory(home, relative):
    """rmdir a created directory if it is an empty real directory; return why it was kept."""
    descriptor = _pin(home, relative)
    if descriptor is None:
        return None
    leaf = (home / relative).name
    try:
        try:
            info = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            return "is no longer a directory"
        try:
            os.rmdir(leaf, dir_fd=descriptor)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return "not empty"
            raise
        os.fsync(descriptor)
        return None
    finally:
        os.close(descriptor)


@_owner_accessible
def apply_rollback(rollback_dir, home, *, bundle=None, include_changed=False,
                   before_write=None):
    """Roll a restore back, saving what it replaces under undone-<time>/.

    before_write, if given, runs after validation and immediately before the
    first change; an exception from it prevents all writes. Returns what was
    done; conflicts that were left alone (and any kept directories) are listed.
    """
    plan = plan_rollback(rollback_dir, home, bundle=bundle, include_changed=include_changed)
    rollback, home = _absolute(rollback_dir), _absolute(home)
    if before_write is not None:
        before_write()
    actions = sorted([(path, "put_back") for path in plan["put_back"]]
                     + [(path, "remove") for path in plan["remove"]])
    skipped = [item for item in plan["conflicts"]
               if item["blocked"] or not include_changed]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    undone = rollback / ("undone-" + stamp)
    if os.path.lexists(undone):
        undone = rollback / ("undone-" + stamp + "-" + uuid.uuid4().hex[:8])
    staging = rollback / ("rollback-staging-" + uuid.uuid4().hex[:12])
    journal = {
        "status": "in_progress", "kind": "intent_plan",
        "rollback_of": str(rollback / JOURNAL),
        "bundle": plan["bundle"], "home": str(home), "include_changed": include_changed,
        "undone_dir": str(undone),
        "put_back": list(plan["put_back"]), "remove": list(plan["remove"]),
        "unchanged": plan["unchanged"],
        "skipped_conflicts": [{"path": item["path"], "reason": item["reason"]}
                              for item in skipped],
        "directories": list(plan["directories"]),
        "original_directories": plan["original_directories"], "warnings": plan["warnings"],
        "restore_directory_metadata": plan["restore_directory_metadata"],
        "retained_quarantines": list(plan["retained_quarantines"]),
    }
    record_directory = rollback / QUARANTINE_RECORDS
    records_ready = False

    def retain_quarantine(relative, name):
        nonlocal records_ready
        path = (PurePosixPath(relative).parent / name / "entry").as_posix()
        record = {"path": relative, "quarantine": path}
        size = len(json.dumps(record, indent=2, ensure_ascii=True).encode("utf-8")) + 1
        if size > MAX_QUARANTINE_RECORD_BYTES:
            raise BundleError("Quarantine recovery reference is too large; no entry was moved.")
        if not records_ready:
            _assert_no_symlink_ancestors(record_directory, include_leaf=True)
            _private_parents(record_directory)
            _assert_no_symlink_ancestors(record_directory, include_leaf=True)
            if not record_directory.is_dir():
                raise BundleError(f"Quarantine recovery records must be a directory: {record_directory}")
            records_ready = True
        filename = name.removeprefix(".fedora-migration-quarantine-") + ".json"
        # This small atomic write fsyncs both the record and its directory before
        # the caller may rename an entry. The full summary can lag after a crash.
        _update_journal(record_directory, record, filename)
        journal["retained_quarantines"].append(record)

        def release():
            (record_directory / filename).unlink()
            _fsync_directory(record_directory)
            journal["retained_quarantines"].remove(record)

        return str(home / path), release

    started = False
    access_changes = []
    try:
        _update_journal(rollback, journal, ROLLBACK_JOURNAL)
        started = True
        undone.mkdir(mode=0o700)
        staging.mkdir(mode=0o700)
        _fsync_directory(rollback)
        _prepare_directory_access(home, plan["directories"] + [
            item["path"] for item in plan["original_directories"]], access_changes)
        for relative, action in actions:
            _assert_no_symlink_ancestors(home / relative)
            _apply_action(home, relative, action, rollback, undone, staging, retain_quarantine)
        # Directories that were not removable at plan time because they are no
        # longer directories; the loop below reports the rest as it finds them.
        removed_directories = []
        kept = [item for item in plan["kept_directories"]
                if item["path"] not in plan["directories"]]
        for relative in sorted(plan["directories"],
                               key=lambda value: (-value.count("/"), value)):
            path = home / relative
            _assert_no_symlink_ancestors(path)
            reason = _remove_empty_directory(home, relative)
            if reason:
                kept.append({"path": relative, "reason": reason})
            elif not os.path.lexists(path):
                removed_directories.append(relative)
        original_paths = {item["path"] for item in plan["restore_directory_metadata"]}
        _restore_directory_access(home, access_changes, preserve=original_paths)
        _restore_directory_metadata(home, plan["restore_directory_metadata"])
        access_changes.clear()  # remaining original-root grants now have their final modes
        staging.rmdir()
        used = any(undone.iterdir())
        if not used:
            undone.rmdir()
        journal["undone_dir"] = str(undone) if used else None
        journal["status"] = "partial" if skipped else "complete"
        _update_journal(rollback, journal, ROLLBACK_JOURNAL)
        return {
            "status": journal["status"], "undone_dir": journal["undone_dir"],
            "put_back": [path for path, action in actions if action == "put_back"],
            "removed": [path for path, action in actions if action == "remove"],
            "unchanged": plan["unchanged"], "conflicts": plan["conflicts"],
            "skipped": skipped, "removed_directories": removed_directories, "kept_directories": kept,
            "restored_directories": [item["path"] for item in plan["restore_directory_metadata"]],
            "warnings": plan["warnings"],
            "retained_quarantines": journal["retained_quarantines"],
        }
    except (BundleError, OSError, ValueError, TypeError, OverflowError,
            KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        try:
            _restore_directory_access(home, access_changes)
        except (BundleError, OSError) as problem:
            exc = BundleError(f"{exc}. Could not reset temporary directory access: {problem}")
        try:
            staging.rmdir()
        except OSError:
            pass
        if started:
            journal["status"] = "interrupted" if interrupted else "failed"
            journal["error"] = str(exc) or "Interrupted by user"
            try:
                _update_journal(rollback, journal, ROLLBACK_JOURNAL)
            except OSError:
                pass
        saved = (f"Current versions saved before replacement are in {undone}."
                 if _holds_copies(undone) else "No current versions had been saved.")
        if journal["retained_quarantines"]:
            locations = ", ".join(str(home / item["quarantine"])
                                  for item in journal["retained_quarantines"][:3])
            saved += (f" Captured cross-filesystem originals remain at {locations}; "
                      f"recovery locations are recorded in {record_directory} and "
                      f"{rollback / ROLLBACK_JOURNAL} (the summary may lag after a failure).")
        if interrupted:
            raise BundleInterrupted(
                f"Rollback interrupted. {saved} Retained rollback directory: {rollback}. "
                f"Read {ROLLBACK_JOURNAL} before running rollback again."
            ) from exc
        raise BundleError(
            f"Rollback failed: {exc}. {saved} Retained rollback directory: {rollback}"
        ) from exc
