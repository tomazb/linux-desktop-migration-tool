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
    BundleError, BundleInterrupted, _absolute, _assert_no_symlink_ancestors,
    _digest, _fingerprint, _fsync_directory, _kind, _owner_accessible,
    _private_parents, _safe_path, _update_journal, _validated_rewrite, _within,
    _within_secret, verify_bundle,
)
from .report import entry_is_rewritable, replace_home_references, rewritable

JOURNAL = "journal.json"
ROLLBACK_JOURNAL = "rollback-journal.json"
RESTORE_STATUSES = ("in_progress", "complete", "failed", "interrupted")
MAX_JOURNAL_BYTES = 64 * 1024 * 1024


def _read_json_file(path, what):
    """Read a regular, bounded JSON file without following a symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise BundleError(f"No {what} found: {path}") from None
    if _kind(info.st_mode) != "file":
        raise BundleError(f"The {what} must be a regular file: {path}")
    if info.st_size > MAX_JOURNAL_BYTES:
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
    rewrite = journal.get("rewrite_home")
    if rewrite is not None:
        if (not isinstance(rewrite, list) or len(rewrite) != 2
                or not all(isinstance(value, str) for value in rewrite)):
            raise BundleError("Invalid journal: rewrite_home must be null or two strings.")
        rewrite = _validated_rewrite(rewrite)
    return journal, rewrite


def _refuse_if_rolled_back(rollback):
    path = rollback / ROLLBACK_JOURNAL
    if not os.path.lexists(path):
        return
    previous = _read_json_file(path, "rollback journal")
    status = previous.get("status") if isinstance(previous, dict) else None
    if status == "complete":
        raise BundleError(f"This restore was already rolled back (see {path}).")
    if not isinstance(status, str):
        raise BundleError(f"The rollback journal is unreadable: {path}")


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
    if _absolute(journal["home"]) != home:
        raise BundleError(
            f"This rollback directory belongs to home {journal['home']}, not {home}. "
            "Refusing to change another home.")
    _refuse_if_rolled_back(rollback)
    bundle_path = _absolute(bundle if bundle is not None else journal["bundle"])
    entries, problem = _verified_manifest(bundle_path)
    previous = rollback / "previous"

    put_back, remove, unchanged, absent, conflicts = [], [], [], [], []
    everything = journal["replaced"] + journal["new"]
    for relative in everything:
        destination = home / relative
        if _within(destination, rollback) or _within(destination, bundle_path):
            raise BundleError(
                f"Journal path {relative} lies inside the rollback directory or the bundle.")
        _assert_no_symlink_ancestors(destination)
    for relative in journal["created_directories"]:
        # A created directory may legitimately contain the rollback directory
        # (the restore made it first), so only the other three overlaps are refused.
        directory = home / relative
        if (_within(directory, rollback) or _within(directory, bundle_path)
                or _within(bundle_path, directory)):
            raise BundleError(
                f"Journal directory {relative} overlaps the rollback directory or the bundle.")

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
    return {
        "rollback_dir": str(rollback), "home": str(home), "status": journal["status"],
        "bundle": str(bundle_path), "verified": problem is None, "bundle_problem": problem,
        "rewrite_home": rewrite, "include_changed": include_changed,
        "put_back": put_back, "remove": remove, "unchanged": sorted(unchanged),
        "absent": sorted(absent), "conflicts": conflicts,
        "directories": existing, "remove_directories": removable, "kept_directories": kept,
        "secrets": any(_within_secret(path) for path in everything + journal["created_directories"]),
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
    """Open the directory holding relative, one component at a time, never following links.

    Returns a descriptor, or None if a parent is missing and create is false.
    The caller closes it. Every later operation uses names relative to it, so
    swapping a path component for a symlink afterwards cannot redirect them.
    """
    descriptor = os.open(home, _DIRECTORY_FLAGS)
    try:
        for name in PurePosixPath(relative).parent.parts:
            try:
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                os.mkdir(name, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise BundleError(
                        f"Refusing symlink or non-directory ancestor {name!r} of {home / relative}"
                    ) from exc
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise


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


def _entry_fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


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


def _copy_entry_out(descriptor, leaf, destination, staging):
    """Copy the entry named leaf to destination (staged, synced, never partial).

    Used when the entry cannot be renamed into place (another filesystem).
    Returns the copied entry's fingerprint so the caller can detect a change.
    """
    temporary = staging / uuid.uuid4().hex
    try:
        info = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            temporary.symlink_to(os.readlink(leaf, dir_fd=descriptor))
            os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)
            copied = info
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
                    raise BundleError(f"{leaf} changed while it was being saved; "
                                      "nothing was removed.")
                copied = before
        _private_parents(destination.parent)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        return _entry_fingerprint(copied)
    finally:
        with contextlib.suppress(OSError):
            if os.path.lexists(temporary):
                temporary.unlink()


def _capture(descriptor, leaf, destination, staging):
    """Move the current entry into the undone directory; return (pending, moved).

    A rename moves the exact entry, so no edit can fall between saving and
    removing it. Across filesystems that is impossible: the entry is copied
    instead and pending holds its fingerprint, to be re-checked before the
    caller removes or replaces it.
    """
    if os.path.lexists(destination):
        raise BundleError(f"Undone copy already exists: {destination}")
    _private_parents(destination.parent)
    try:
        os.rename(leaf, destination, src_dir_fd=descriptor)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        return _copy_entry_out(descriptor, leaf, destination, staging), False
    _fsync_directory(destination.parent)
    os.fsync(descriptor)
    return None, True


def _assert_unchanged(descriptor, leaf, pending):
    try:
        current = _entry_fingerprint(os.stat(leaf, dir_fd=descriptor, follow_symlinks=False))
    except FileNotFoundError:
        current = None
    if current != pending:
        raise BundleError(f"{leaf} changed after it was saved; nothing was removed. "
                          "Run rollback again to review it.")


def _apply_action(home, relative, action, rollback, undone, staging):
    """Put back or remove one path, saving the current entry under undone/ first."""
    destination = home / relative
    leaf = destination.name
    descriptor = _pin(home, relative, create=action == "put_back")
    if descriptor is None:
        return  # the parent is gone, so there is nothing to remove
    staged = None
    captured = False
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
        pending = None
        if info is not None:
            pending, captured = _capture(descriptor, leaf, undone / relative, staging)
        if pending is not None:
            _assert_unchanged(descriptor, leaf, pending)
        if action == "put_back":
            os.replace(staged, leaf, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            staged = None
        elif pending is not None:
            os.unlink(leaf, dir_fd=descriptor)
        os.fsync(descriptor)
    except BaseException:
        if captured and action == "put_back":
            # Best effort: leave the user's entry where it was rather than only in undone/.
            try:
                os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                with contextlib.suppress(OSError):
                    os.rename(undone / relative, leaf, dst_dir_fd=descriptor)
            except OSError:
                pass
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
    }
    started = False
    try:
        _update_journal(rollback, journal, ROLLBACK_JOURNAL)
        started = True
        undone.mkdir(mode=0o700)
        staging.mkdir(mode=0o700)
        _fsync_directory(rollback)
        for relative, action in actions:
            _assert_no_symlink_ancestors(home / relative)
            _apply_action(home, relative, action, rollback, undone, staging)
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
        }
    except (BundleError, OSError, ValueError, TypeError, OverflowError,
            KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
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
        if interrupted:
            raise BundleInterrupted(
                f"Rollback interrupted. {saved} Retained rollback directory: {rollback}. "
                f"Read {ROLLBACK_JOURNAL} before running rollback again."
            ) from exc
        raise BundleError(
            f"Rollback failed: {exc}. {saved} Retained rollback directory: {rollback}"
        ) from exc
