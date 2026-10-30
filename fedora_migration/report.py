"""Read-only checks of a bundle against the destination, for the restore preview.

Everything here only reads bundle payload files as bytes and inspects the
destination; nothing is written and no bundled program is ever executed.
"""

import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import xml.etree.ElementTree as ElementTree

# Larger files are not scanned or parsed.
MAX_SCAN_BYTES = 1024 * 1024

# Never looked into, even where a pattern below would otherwise match.
NEVER_SCANNED = (".mozilla", ".thunderbird", ".var")

# Home-relative files that may hold absolute paths and can safely be rewritten.
SCANNED_FILES = (
    ".gtkrc-2.0", ".config/gtk-3.0/bookmarks", ".config/gtk-4.0/bookmarks",
    ".config/user-dirs.dirs", ".config/mimeapps.list",
)

PANEL_XML = ".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"
# Directories that hold <type>.desktop for each panel plugin, besides the
# destination user's own ~/.local/share/xfce4/panel/plugins.
PANEL_PLUGIN_DIRS = (
    "/usr/share/xfce4/panel/plugins",
    "/usr/local/share/xfce4/panel/plugins",
)
USER_PLUGIN_DIR = ".local/share/xfce4/panel/plugins"

AUTOSTART_DIR = ".config/autostart"
# Launchers are settings only in these places; elsewhere a .desktop file can be
# user data, such as a file in a software project.
DESKTOP_FILE_ROOTS = (".config", ".local/share/applications")
DESKTOP_LAUNCHER_DIR = "Desktop"
# Longest value shown in a finding; longer ones are truncated.
MAX_SHOWN = 120


def clip(value, limit=MAX_SHOWN):
    """Shorten a value from bundle data before it is shown."""
    value = str(value)
    return value if len(value) <= limit else value[:limit] + "..."


def _within(relative, root):
    return relative == root or relative.startswith(root + "/")


def is_rewrite_candidate(relative):
    """Whether a home-relative path may be scanned for, and rewritten with, the old home."""
    if any(_within(relative, root) for root in NEVER_SCANNED):
        return False
    path = PurePosixPath(relative)
    if relative in SCANNED_FILES:
        return True
    if path.suffix == ".desktop":
        return (any(_within(relative, root) for root in DESKTOP_FILE_ROOTS)
                or path.parent.as_posix() == DESKTOP_LAUNCHER_DIR)
    if path.suffix == ".xml" and relative.startswith(".config/xfce4/"):
        return True
    return path.parent.as_posix() == ".config/Thunar"


def entry_is_rewritable(entry):
    """Whether a manifest entry is a regular, small file in the scanned set."""
    return (entry["type"] == "file" and entry["size"] <= MAX_SCAN_BYTES
            and is_rewrite_candidate(entry["path"]))


def _reference_pattern(old):
    # Not preceded by a name byte (that would be a deeper path ending in the old
    # home), and not followed by one (/home/alice2 is another user's home).
    return re.compile(rb"(?<![A-Za-z0-9._-])" + re.escape(old) + rb"(?![A-Za-z0-9._-])")


def _normalised(old):
    old = os.fsencode(old).rstrip(b"/")
    return old or None


def count_home_references(data, old):
    """Count references to the old home path in bytes."""
    old = _normalised(old)
    return 0 if old is None else len(_reference_pattern(old).findall(data))


def replace_home_references(data, old, new):
    """Replace each reference's old-home prefix with new; return (bytes, count)."""
    old = _normalised(old)
    if old is None:
        return data, 0
    new = os.fsencode(new).rstrip(b"/")
    return _reference_pattern(old).subn(lambda _match: new, data)


def _read_payload(bundle, relative, limit=MAX_SCAN_BYTES):
    """Read a payload regular file as bytes; None if it is not one or exceeds limit."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(Path(bundle) / "payload" / relative, flags)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        data = stream.read(limit + 1)
    return data if len(data) <= limit else None


def find_home_references(bundle, manifest, old_home):
    """List (path, occurrences) for scanned bundle files that name the old home."""
    if _normalised(old_home) is None:
        return []
    found = []
    for entry in manifest["entries"]:
        if not entry_is_rewritable(entry):
            continue
        data = _read_payload(bundle, entry["path"])
        if data is None or _is_binary_thunar(entry["path"], data):
            continue
        count = count_home_references(data, old_home)
        if count:
            found.append((entry["path"], count))
    return sorted(found)


def _is_binary_thunar(relative, data):
    return relative.startswith(".config/Thunar/") and b"\0" in data


def rewritable(entry, data):
    """Whether the checksummed content of an eligible entry may be rewritten."""
    return not _is_binary_thunar(entry["path"], data)


def _file_entries(manifest):
    return {e["path"]: e for e in manifest["entries"] if e["type"] == "file"}


def check_panel_plugins(bundle, manifest, home):
    """Find panel plugin types the bundle's panel uses that are not installed here."""
    result = {"missing": [], "warnings": []}
    entry = _file_entries(manifest).get(PANEL_XML)
    if entry is None:
        return result
    data = _read_payload(bundle, PANEL_XML)
    if data is None:
        result["warnings"].append(
            f"Could not check panel plugins: {PANEL_XML} is not a regular file of "
            f"at most {MAX_SCAN_BYTES // 1024 // 1024} MiB.")
        return result
    # ElementTree never fetches external entities, but do not expand internal
    # ones from a file we only need a few attributes from. Requiring plain
    # ASCII-compatible text also rules out UTF-16 hiding the declaration.
    try:
        data.decode("utf-8")
        plain = b"\0" not in data
    except UnicodeDecodeError:
        plain = False
    if not plain or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        result["warnings"].append(
            f"Could not check panel plugins: {PANEL_XML} is not plain UTF-8 XML "
            "without DOCTYPE or entity declarations.")
        return result
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, LookupError, ValueError) as exc:
        result["warnings"].append(
            f"Could not check panel plugins: {PANEL_XML} is not valid XML ({exc}).")
        return result
    directories = [*PANEL_PLUGIN_DIRS, str(Path(home) / USER_PLUGIN_DIR)]
    seen = set()
    # plugin-N properties are direct children of a "plugins" property; looking
    # only at direct children keeps nested input linear.
    for plugins in (p for p in root.iter("property") if p.get("name") == "plugins"):
        for prop in plugins:
            kind = prop.get("value")
            if (prop.tag != "property"
                    or not re.fullmatch(r"plugin-\d+", prop.get("name", ""))
                    or prop.get("type") != "string" or not kind or kind in seen):
                continue
            seen.add(kind)
            if len(kind) > MAX_SHOWN or not re.fullmatch(r"[A-Za-z0-9._+-]+", kind):
                result["warnings"].append(f"Ignored unusual panel plugin type {clip(kind)!r}.")
            elif not any(os.path.exists(os.path.join(d, kind + ".desktop"))
                         for d in directories):
                result["missing"].append(kind)
    return result


def _desktop_entry(text):
    """Return Exec, Hidden and TryExec from the [Desktop Entry] group only."""
    values = {}
    in_group = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_group = line == "[Desktop Entry]"
        elif in_group and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            key = key.strip()
            if key in ("Exec", "Hidden", "TryExec") and key not in values:
                values[key] = value.strip()
    return values


def _program(values):
    """The program an entry starts: TryExec, else the first real Exec token.

    Raises ValueError for an Exec that cannot be split; returns None if there is none.
    """
    if values.get("TryExec"):
        return values["TryExec"]
    tokens = shlex.split(values.get("Exec", ""))
    if tokens and os.path.basename(tokens[0]) == "env":
        tokens.pop(0)
        # Skip env's options; these take a separate argument.
        while tokens and tokens[0].startswith("-") and tokens[0] != "-":
            option = tokens.pop(0)
            if option == "--":
                break
            if option in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string") and tokens:
                tokens.pop(0)
    while tokens and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    return tokens[0] if tokens else None


def _program_available(program, provided):
    if os.path.isabs(program):
        return (os.path.isfile(program) and os.access(program, os.X_OK)) or program in provided
    return shutil.which(program) is not None


def check_autostart(bundle, manifest, home, source_home=None, rewrite=False):
    """Find autostart entries whose program is not available; nothing is executed.

    A program counts as available if it is executable now, or if the bundle
    restores an executable file to that destination path. With rewrite=True a
    path under the old home is checked where --rewrite-home will point it.
    """
    result = {"missing": [], "warnings": []}
    files = _file_entries(manifest)
    destination = str(home).rstrip("/") + "/"
    provided = {destination + relative for relative, entry in files.items()
                if entry["mode"] & 0o111}
    old_prefix = (str(source_home).rstrip("/") + "/"
                  if rewrite and source_home and str(source_home).rstrip("/") else None)
    for relative in sorted(files):
        path = PurePosixPath(relative)
        if path.parent.as_posix() != AUTOSTART_DIR or path.suffix != ".desktop":
            continue
        data = _read_payload(bundle, relative)
        if data is None:
            continue
        values = _desktop_entry(data.decode("utf-8", errors="replace"))
        if values.get("Hidden", "").lower() == "true":
            continue
        try:
            program = _program(values)
        except ValueError:
            result["warnings"].append(f"{relative}: unparseable Exec")
            continue
        effective = program
        if program and old_prefix and program.startswith(old_prefix):
            effective = destination + program[len(old_prefix):]
        if program and not _program_available(effective, provided):
            result["missing"].append((relative, clip(program)))
    return result
