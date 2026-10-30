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

from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import stat
import uuid

from .bundle import (
    BundleError, BundleInterrupted, _absolute, _assert_no_symlink_ancestors,
    _copy_regular, _digest, _fsync_directory, _kind, _owner_accessible,
    _private_parents, _safe_path, _save_original, _update_journal,
    _validated_rewrite, _within, _within_secret, verify_bundle,
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
    """Whether two existing paths hold identical regular-file bytes or link targets."""
    current_info, saved_info = current.lstat(), saved.lstat()
    if stat.S_ISLNK(current_info.st_mode) and stat.S_ISLNK(saved_info.st_mode):
        return os.readlink(current) == os.readlink(saved)
    if stat.S_ISREG(current_info.st_mode) and stat.S_ISREG(saved_info.st_mode):
        return (current_info.st_size == saved_info.st_size
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


def _put_back(saved, destination):
    """Copy a saved original next to its destination, then atomically replace it."""
    if not os.path.lexists(saved):
        raise BundleError(f"Saved original disappeared: {saved}")
    _private_parents(destination.parent)
    temporary = destination.parent / (".fedora-migration-" + uuid.uuid4().hex)
    try:
        if saved.is_symlink():
            info = saved.lstat()
            temporary.symlink_to(os.readlink(saved))
            os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns), follow_symlinks=False)
        else:
            _copy_regular(saved, temporary)
        os.replace(temporary, destination)
    finally:
        try:
            if os.path.lexists(temporary):
                temporary.unlink()
        except OSError:
            pass
    _fsync_directory(destination.parent)


def _remove_empty_directory(path):
    """rmdir path if it is an empty real directory; return a reason when kept."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return "is no longer a directory"
    try:
        path.rmdir()
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
            return "not empty"
        raise
    _fsync_directory(path.parent)
    return None


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
            destination = home / relative
            _assert_no_symlink_ancestors(destination)
            exists = os.path.lexists(destination)
            if exists and _kind(destination.lstat().st_mode) not in ("file", "symlink"):
                raise BundleError(f"Destination is no longer a file or symlink: {destination}")
            if exists:
                _save_original(destination, undone / relative, staging)
            if action == "put_back":
                _put_back(rollback / "previous" / relative, destination)
            elif exists:
                destination.unlink()
                _fsync_directory(destination.parent)
        # Directories that were not removable at plan time because they are no
        # longer directories; the loop below reports the rest as it finds them.
        removed_directories = []
        kept = [item for item in plan["kept_directories"]
                if item["path"] not in plan["directories"]]
        for relative in sorted(plan["directories"],
                               key=lambda value: (-value.count("/"), value)):
            path = home / relative
            _assert_no_symlink_ancestors(path)
            reason = _remove_empty_directory(path)
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
                 if os.path.lexists(undone) else "No current versions had been saved.")
        if interrupted:
            raise BundleInterrupted(
                f"Rollback interrupted. {saved} Retained rollback directory: {rollback}. "
                f"Read {ROLLBACK_JOURNAL} before running rollback again."
            ) from exc
        raise BundleError(
            f"Rollback failed: {exc}. {saved} Retained rollback directory: {rollback}"
        ) from exc
