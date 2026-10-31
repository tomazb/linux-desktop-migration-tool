#!/usr/bin/env python3
"""Automate the documented two-VM Fedora 43 -> 44 rehearsal on a libvirt/KVM host.

By default this only PRINTS what it would do (dry run). Pass --execute to run it.
It needs a host with /dev/kvm and libvirt; see docs/FEDORA_43_TO_44.md. The logic is
split so it can be unit tested without VMs: pure builders (URLs, checksum parsing,
cloud-init rendering, ssh command lines, the step plan, report rendering) and a
small Runner that either prints or executes the steps. Standard library only.

The Fedora CHECKSUM files are NOT verified with GPG (out of scope): images are
checked against SHA-256 values fetched over HTTPS only.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import uuid

IMAGE_INDEX_URL = ("https://download.fedoraproject.org/pub/fedora/linux/releases/"
                   "{release}/Cloud/x86_64/images/")
IMAGE_NAME = "Fedora-Cloud-Base-Generic-{release}-{build}.x86_64.qcow2"
CHECKSUM_NAME = "Fedora-Cloud-{release}-{build}-x86_64-CHECKSUM"
IMAGE_NAME_PATTERN = r"Fedora-Cloud-Base-Generic-{release}-([0-9]+(?:\.[0-9]+)*)\.x86_64\.qcow2"

BASE_TOOLS = ("virt-install", "virsh", "qemu-img", "ssh", "scp", "ssh-keygen", "curl", "sha256sum")
SEED_TOOLS = ("cloud-localds", "genisoimage", "xorriso")
KVM_DEVICE = "/dev/kvm"

DEFAULT_WORKDIR = "~/.cache/fedora-migration-rehearsal"
SSH_HOST = "127.0.0.1"
REMOTE_REPO = "$HOME/fedora-migration"
BUNDLE_DIR = "/var/tmp/bundle"
ROLLBACK_DIR = "/var/tmp/rollback-1"
EXPECTED_FILE = "/var/tmp/fmr-expected.txt"
CHECKSUM_GPG_NOTE = ("Note: the Fedora CHECKSUM files are not GPG-verified (out of scope); images are "
                     "checked against SHA-256 values fetched over HTTPS only.")
FLATPAK_REMOTE_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"
FLATPAK_APP = "org.gnome.Calculator"
EXTRA_RPM = "cowsay"

SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes",
               "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10")
SSH_PORT_BASE = 22200
USER_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,30}")
RUN_ID_PATTERN = re.compile(r"[a-z0-9]{1,16}")
HOSTNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
PUBLIC_KEY_PATTERN = re.compile(r"ssh-(?:ed25519|rsa) [A-Za-z0-9+/=]+(?: [A-Za-z0-9@._-]+)?")
SHA256_LINE = re.compile(r"SHA256 \((.+)\) = ([0-9a-f]{64})")
# Directories never copied into the VMs with the checkout.
TARBALL_EXCLUDES = {".git", "__pycache__", ".claude", "graphify-out", ".pytest_cache"}

# Files compared byte for byte, by mode and by mtime; relative to the home directory.
SAMPLE_FILES = (
    "Documents/fmr-note.txt",
    "Pictures/fmr-pic.dat",
    ".rehearsal-extra/sub/data.txt",
    ".local/bin/fmr-hello",
)
PANEL_XML = ".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"
DISPLAYS_XML = ".config/xfce4/xfconf/xfce-perchannel-xml/displays.xml"
LAUNCHER = ".local/share/applications/fmr-rehearsal.desktop"


class RehearsalError(RuntimeError):
    """A refusal or failure that should be shown to the user without a traceback."""


# --------------------------------------------------------------------------- images

def index_url(release):
    return IMAGE_INDEX_URL.format(release=int(release))


def image_name(release, build):
    return IMAGE_NAME.format(release=int(release), build=build)


def checksum_name(release, build):
    return CHECKSUM_NAME.format(release=int(release), build=build)


def image_url(release, build):
    return index_url(release) + image_name(release, build)


def checksum_url(release, build):
    return index_url(release) + checksum_name(release, build)


def discover_build(index_html, release):
    """The newest build id (for example '1.6') of the Cloud Base Generic image in an index page."""
    pattern = re.compile(IMAGE_NAME_PATTERN.format(release=int(release)))
    builds = {match.group(1) for match in pattern.finditer(index_html)}
    if not builds:
        raise RehearsalError(f"No Fedora {release} Cloud Base Generic image found in the image index; "
                             "pass --source-image/--dest-image or --source-build/--dest-build.")
    return max(builds, key=lambda build: tuple(int(part) for part in build.split(".")))


def parse_checksum(text):
    """Map file name to SHA-256 from a Fedora CHECKSUM file (BSD-style lines).

    Comment lines and the PGP clear-signing armor are ignored; the signature itself
    is not checked.
    """
    result = {}
    for line in text.splitlines():
        match = SHA256_LINE.fullmatch(line.strip())
        if not match:
            continue
        name, digest = match.groups()
        if name in result and result[name] != digest:
            raise RehearsalError(f"The CHECKSUM file lists conflicting digests for {name}.")
        result[name] = digest
    if not result:
        raise RehearsalError("The CHECKSUM file contains no 'SHA256 (file) = digest' lines.")
    return result


def expected_sha256(parsed, filename):
    try:
        return parsed[filename]
    except KeyError:
        raise RehearsalError(f"{filename} is not listed in the CHECKSUM file.") from None


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_image(path, expected):
    actual = file_sha256(path)
    if actual != expected:
        raise RehearsalError(f"SHA-256 mismatch for {path}: expected {expected}, got {actual}.")


# --------------------------------------------------------------------------- cloud-init

def render_user_data(user, public_key, hostname):
    """NoCloud user-data creating one passwordless-sudo user reachable by the given public key."""
    if not USER_PATTERN.fullmatch(user):
        raise RehearsalError(f"Unsafe user name: {user!r}")
    if not HOSTNAME_PATTERN.fullmatch(hostname):
        raise RehearsalError(f"Unsafe host name: {hostname!r}")
    if not PUBLIC_KEY_PATTERN.fullmatch(public_key.strip()):
        raise RehearsalError("The SSH key is not a single-line ssh-ed25519 or ssh-rsa public key.")
    # JSON strings are valid YAML scalars, so values need no further escaping.
    return "\n".join([
        "#cloud-config",
        f"hostname: {hostname}",
        "users:",
        f"  - name: {user}",
        "    gecos: Migration rehearsal user",
        "    shell: /bin/bash",
        "    groups: [wheel]",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    lock_passwd: true",
        "    ssh_authorized_keys:",
        f"      - {json.dumps(public_key.strip())}",
        "ssh_pwauth: false",
        "",
    ])


def render_meta_data(instance_id, hostname):
    for value in (instance_id, hostname):
        if not HOSTNAME_PATTERN.fullmatch(value):
            raise RehearsalError(f"Unsafe cloud-init identifier: {value!r}")
    return f"instance-id: {instance_id}\nlocal-hostname: {hostname}\n"


def seed_command(tool, iso, user_data, meta_data):
    iso, user_data, meta_data = (str(item) for item in (iso, user_data, meta_data))
    if tool == "cloud-localds":
        return ["cloud-localds", iso, user_data, meta_data]
    if tool == "genisoimage":
        return ["genisoimage", "-output", iso, "-volid", "cidata", "-joliet", "-rock", user_data, meta_data]
    if tool == "xorriso":
        return ["xorriso", "-as", "mkisofs", "-o", iso, "-V", "cidata", "-J", "-r", user_data, meta_data]
    raise RehearsalError(f"Unsupported seed tool: {tool}")


# --------------------------------------------------------------------------- configuration

def run_id_type(value):
    if not RUN_ID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("must be 1-16 characters of a-z and 0-9")
    return value


def user_type(value):
    if not USER_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a plain lower-case Unix user name")
    return value


def default_run_id():
    return time.strftime("%y%m%d%H%M%S")


@dataclass(frozen=True)
class Config:
    workdir: Path
    run_id: str
    execute: bool = False
    keep: bool = False
    connect: str = "qemu:///session"
    source_release: int = 43
    dest_release: int = 44
    source_image: Path | None = None
    dest_image: Path | None = None
    source_build: str | None = None
    dest_build: str | None = None
    source_user: str = "alice"
    dest_user: str = "bob"
    with_flatpak: bool = False
    vcpus: int = 2
    memory_mib: int = 4096
    disk_gib: int = 20
    osinfo: str = "fedora-unknown"
    ssh_port_base: int = SSH_PORT_BASE
    ssh_timeout: int = 300
    repo_root: Path = Path(__file__).resolve().parents[1]
    uuids: tuple = field(default_factory=lambda: (str(uuid.uuid4()), str(uuid.uuid4())))

    @property
    def run_dir(self):
        return self.workdir / "runs" / self.run_id

    @property
    def images_dir(self):
        return self.workdir / "images"

    @property
    def key_path(self):
        return self.run_dir / "id_ed25519"

    def domain_name(self, label):
        return f"fmr-{label}-{self.run_id}"

    def domain_names(self):
        return (self.domain_name("src"), self.domain_name("dst"))

    def ssh_port(self, label):
        return self.ssh_port_base + (0 if label == "src" else 1)

    def release(self, label):
        return self.source_release if label == "src" else self.dest_release

    def user(self, label):
        return self.source_user if label == "src" else self.dest_user

    def build(self, label):
        return self.source_build if label == "src" else self.dest_build

    def image_override(self, label):
        return self.source_image if label == "src" else self.dest_image

    def domain_uuid(self, label):
        """The UUID this run gives the VM; it proves ownership before anything is destroyed."""
        return self.uuids[0 if label == "src" else 1]

    def pinned_base_image(self, label):
        """The immutable, build-specific backing image if it is known before the run, else None."""
        override = self.image_override(label)
        if override:
            return override
        if self.build(label):
            return self.images_dir / image_name(self.release(label), self.build(label))
        return None

    def placeholder_base_image(self, label):
        return self.images_dir / image_name(self.release(label), "<build>")

    def target(self, label):
        return Target(label=label, user=self.user(label), port=self.ssh_port(label), key=self.key_path)


@dataclass(frozen=True)
class Target:
    label: str
    user: str
    port: int
    key: Path


def build_parser():
    parser = argparse.ArgumentParser(
        description="Automate the two-VM Fedora migration rehearsal with libvirt/KVM. "
                    "Without --execute this only prints what it would do.",
        epilog=CHECKSUM_GPG_NOTE)
    parser.add_argument("--execute", action="store_true",
                        help="actually create VMs and run the rehearsal (default: dry run, prints commands only)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan without running it (the default)")
    parser.add_argument("--workdir", default=DEFAULT_WORKDIR, help="images, overlays, keys and reports live here")
    parser.add_argument("--run-id", type=run_id_type, default=None,
                        help="identifier used in VM names fmr-src-ID / fmr-dst-ID (default: timestamp)")
    parser.add_argument("--keep", action="store_true", help="keep the VMs and overlays after the run")
    parser.add_argument("--connect", default="qemu:///session", help="libvirt URI (default: %(default)s)")
    parser.add_argument("--source-release", type=int, default=43)
    parser.add_argument("--dest-release", type=int, default=44)
    parser.add_argument("--source-image", type=Path, help="local Fedora Cloud qcow2 for the source (skips download, no checksum)")
    parser.add_argument("--dest-image", type=Path, help="local Fedora Cloud qcow2 for the destination (skips download, no checksum)")
    parser.add_argument("--source-build", help="image build id such as 1.6 (default: newest listed)")
    parser.add_argument("--dest-build", help="image build id such as 1.7 (default: newest listed)")
    parser.add_argument("--source-user", type=user_type, default="alice")
    parser.add_argument("--dest-user", type=user_type, default="bob")
    parser.add_argument("--with-flatpak", action="store_true", help="also install a user Flatpak from flathub on the source")
    parser.add_argument("--vcpus", type=int, default=2)
    parser.add_argument("--memory-mib", type=int, default=4096)
    parser.add_argument("--disk-gib", type=int, default=20)
    parser.add_argument("--osinfo", default="fedora-unknown", help="virt-install --osinfo value (default: %(default)s)")
    parser.add_argument("--ssh-port-base", type=int, default=SSH_PORT_BASE,
                        help="host loopback ports BASE (source) and BASE+1 (destination) forward to guest SSH")
    parser.add_argument("--ssh-timeout", type=int, default=300, help="seconds to wait for each VM's SSH")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1],
                        help="checkout to copy into the VMs (default: this one)")
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.execute and args.dry_run:
        parser.error("--execute and --dry-run are mutually exclusive")
    workdir = Path(args.workdir).expanduser().absolute()
    if "," in str(workdir) or "\n" in str(workdir):
        parser.error("--workdir must not contain commas or newlines (virt-install key=value syntax)")
    for name in ("vcpus", "memory_mib", "disk_gib", "ssh_timeout"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if not 1024 <= args.ssh_port_base <= 65534:
        parser.error("--ssh-port-base must be between 1024 and 65534")

    def local(path):
        return path.expanduser().absolute() if path else None

    return Config(
        workdir=workdir, run_id=args.run_id or default_run_id(), execute=args.execute, keep=args.keep,
        connect=args.connect, source_release=args.source_release, dest_release=args.dest_release,
        source_image=local(args.source_image), dest_image=local(args.dest_image),
        source_build=args.source_build, dest_build=args.dest_build,
        source_user=args.source_user, dest_user=args.dest_user, with_flatpak=args.with_flatpak,
        vcpus=args.vcpus, memory_mib=args.memory_mib, disk_gib=args.disk_gib, osinfo=args.osinfo,
        ssh_port_base=args.ssh_port_base, ssh_timeout=args.ssh_timeout,
        repo_root=args.repo.expanduser().absolute(),
    )


# --------------------------------------------------------------------------- remote commands

def _remote_text(command):
    return shlex.join(command) if isinstance(command, (list, tuple)) else command


def ssh_argv(target, command):
    """ssh command line running COMMAND (an argv list, or ready-made shell text) on the VM."""
    return ["ssh", "-i", str(target.key), "-p", str(target.port), *SSH_OPTIONS,
            f"{target.user}@{SSH_HOST}", "--", _remote_text(command)]


def scp_argv(target, local, remote):
    return ["scp", "-i", str(target.key), "-P", str(target.port), *SSH_OPTIONS,
            str(local), f"{target.user}@{SSH_HOST}:{remote}"]


def bash_script(script):
    """Remote command text running a multi-line script in bash."""
    return "bash -c " + shlex.quote(script)


def tool_command(*args):
    """Remote command text running fedora-migration.py from the copied checkout."""
    return f"cd {REMOTE_REPO} && python3 fedora-migration.py {shlex.join(args)}"


# --------------------------------------------------------------------------- steps and runner

class RunState:
    """What this run owns and learned: used to keep cleanup from touching anything else."""

    def __init__(self):
        self.owns_run_dir = False
        self.unconfirmed_domains = set()
        self.base_images = {}


@dataclass
class Step:
    id: str
    description: str
    argv: list | None = None
    action: object = None
    kind: str = "setup"          # setup, tool, check or cleanup; setup/tool failures abort the run
    vm: str | None = None
    expect: str = "zero"         # zero, nonzero or any
    expect_output: tuple = ()
    stdin_file: Path | None = None
    stdout_file: Path | None = None
    creates_domain: str | None = None
    wait: float = 0
    timeout: float | None = None
    record: bool = False
    argv_from: object = None     # computes the real argv at run time (argv is the dry-run view)


@dataclass
class StepResult:
    id: str
    description: str
    kind: str
    vm: str | None
    command: str
    status: str                   # passed, failed, skipped, dry-run
    returncode: int | None
    detail: str
    output_tail: str
    duration: float


def display_command(step):
    if step.argv is None:
        return f"(python) {step.description}"
    text = shlex.join(step.argv)
    if step.stdin_file:
        text += f" < {shlex.quote(str(step.stdin_file))}"
    if step.stdout_file:
        text += f" > {shlex.quote(str(step.stdout_file))}"
    return text


def resolve_argv(step):
    return step.argv_from() if step.argv_from else step.argv


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _tail(text, lines=40, chars=4000):
    return "\n".join(text.splitlines()[-lines:])[-chars:]


class Runner:
    """Prints steps (dry run) or executes them, collecting results."""

    def __init__(self, *, dry_run, run=None, out=None, sleep=None, clock=None, interval=5):
        self.dry_run = dry_run
        self._run = run
        self._out = out
        self._sleep = sleep
        self._clock = clock
        self.interval = interval
        self.results = []
        self.records = {}
        self.created_domains = []     # virt-install succeeded
        self.attempted_domains = []   # virt-install was started; cleanup verifies the UUID first

    def out(self, text):
        (self._out or sys.stdout.write)(text)

    def run(self, argv, **kwargs):
        return (self._run or subprocess.run)(argv, **kwargs)

    def sleep(self, seconds):
        (self._sleep or time.sleep)(seconds)

    def clock(self):
        return (self._clock or time.monotonic)()

    def execute(self, step):
        command = display_command(step)
        self.out(f"==> {step.id}: {step.description}\n    $ {command}\n")
        if self.dry_run:
            return self._finish(step, command, "dry-run", None, "", "", 0.0)
        if step.creates_domain and step.creates_domain not in self.attempted_domains:
            self.attempted_domains.append(step.creates_domain)
        started = self.clock()
        try:
            if step.argv is None:
                detail = str(step.action() or "")
                result = self._finish(step, command, "passed", None, detail, "", self.clock() - started)
            else:
                result = self._execute_argv(step, command, started)
        except Exception as error:
            result = self._finish(step, command, "failed", None, f"{type(error).__name__}: {error}", "",
                                  self.clock() - started)
        if (step.creates_domain and result.status == "passed"
                and step.creates_domain not in self.created_domains):
            self.created_domains.append(step.creates_domain)
        self.out(f"    -> {result.status}" + (f": {result.detail}" if result.detail else "") + "\n")
        return result

    def _once(self, step):
        stdin = open(step.stdin_file, "rb") if step.stdin_file else None
        stdout = open(step.stdout_file, "wb") if step.stdout_file else None
        try:
            return self.run(resolve_argv(step), stdin=stdin or subprocess.DEVNULL, stdout=stdout if stdout else subprocess.PIPE,
                            stderr=subprocess.PIPE, text=stdout is None, errors=None if stdout else "replace",
                            timeout=step.timeout, check=False)
        finally:
            for handle in (stdin, stdout):
                if handle:
                    handle.close()

    def _execute_argv(self, step, command, started):
        deadline = self.clock() + step.wait
        while True:
            completed = self._once(step)
            if completed.returncode == 0 or step.wait <= 0 or self.clock() >= deadline:
                break
            self.sleep(self.interval)
        code = completed.returncode
        output = _text(completed.stdout) + _text(completed.stderr)
        problems = []
        if step.expect == "zero" and code != 0:
            problems.append(f"expected exit 0, got {code}")
        elif step.expect == "nonzero" and code == 0:
            problems.append("expected a nonzero exit, got 0")
        for needle in step.expect_output:
            if needle not in output:
                problems.append(f"output lacks {needle!r}")
        detail = "; ".join(problems)
        if step.expect == "any" and code != 0:
            detail = f"exit {code} (ignored)"
        return self._finish(step, command, "failed" if problems else "passed", code, detail,
                            output, self.clock() - started)

    def _finish(self, step, command, status, code, detail, output, duration):
        if step.record and output:
            self.records[step.id] = output.strip()
        result = StepResult(id=step.id, description=step.description, kind=step.kind, vm=step.vm,
                            command=command, status=status, returncode=code, detail=detail,
                            output_tail=_tail(output), duration=round(duration, 2))
        self.results.append(result)
        return result


def run_plan(steps, runner):
    """Run steps in order; a failed setup or tool step skips the rest, a failed check does not."""
    results = []
    aborted = False
    for step in steps:
        if aborted:
            skipped = StepResult(id=step.id, description=step.description, kind=step.kind, vm=step.vm,
                                 command=display_command(step), status="skipped", returncode=None,
                                 detail="an earlier required step failed", output_tail="", duration=0.0)
            runner.results.append(skipped)
            results.append(skipped)
            continue
        result = runner.execute(step)
        results.append(result)
        if result.status == "failed" and step.kind in ("setup", "tool"):
            aborted = True
    return results


# --------------------------------------------------------------------------- preflight

def which(name):
    return shutil.which(name)


def kvm_present():
    return os.path.exists(KVM_DEVICE)


def choose_seed_tool():
    for tool in SEED_TOOLS:
        if which(tool):
            return tool
    return None


def preflight_static(config):
    """Problems that can be found without running any command."""
    problems = []
    missing = [tool for tool in BASE_TOOLS if not which(tool)]
    if missing:
        problems.append("Missing required host tools: " + ", ".join(missing))
    if choose_seed_tool() is None:
        problems.append("Need one of " + ", ".join(SEED_TOOLS) + " to build the cloud-init seed image.")
    if not kvm_present():
        problems.append(f"{KVM_DEVICE} is missing: this host cannot run KVM guests.")
    return problems


def preflight_libvirt(config):
    try:
        completed = subprocess.run(["virsh", "-c", config.connect, "uri"], capture_output=True,
                                   text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return [f"libvirt is not reachable at {config.connect}: {error}"]
    if completed.returncode != 0:
        return [f"libvirt is not reachable at {config.connect}: {(completed.stderr or '').strip()}"]
    return []


# --------------------------------------------------------------------------- python actions

def _curl_text(url):
    completed = subprocess.run(["curl", "-fsSL", "--retry", "3", url], capture_output=True, text=True,
                               check=False)
    if completed.returncode != 0:
        raise RehearsalError(f"Could not download {url}: {(completed.stderr or '').strip()}")
    return completed.stdout


def fetch_image(config, label, state=None):
    """Make the base image for LABEL available and verified; returns a short description.

    The image keeps its build-specific name and is never repointed, so a VM's backing
    file cannot change under it when another run selects a different build.
    """
    state = state or RunState()
    override = config.image_override(label)
    release = config.release(label)
    if override:
        if not override.is_file():
            raise RehearsalError(f"Image not found: {override}")
        state.base_images[label] = override
        return f"using local image {override} (no checksum available)"
    config.images_dir.mkdir(parents=True, exist_ok=True)
    build = config.build(label) or discover_build(_curl_text(index_url(release)), release)
    name = image_name(release, build)
    checksum_text = _curl_text(checksum_url(release, build))
    expected = expected_sha256(parse_checksum(checksum_text), name)
    target = config.images_dir / name
    note = "cached"
    if target.exists():
        try:
            verify_image(target, expected)
        except RehearsalError:
            target.unlink()
    if not target.exists():
        partial = target.with_name(name + ".partial")
        completed = subprocess.run(["curl", "-fL", "--retry", "3", "-o", str(partial), image_url(release, build)],
                                   check=False)
        if completed.returncode != 0:
            raise RehearsalError(f"Download of {image_url(release, build)} failed (exit {completed.returncode}).")
        try:
            verify_image(partial, expected)
        except RehearsalError:
            partial.unlink(missing_ok=True)
            raise
        os.replace(partial, target)
        note = "downloaded"
    (config.images_dir / (checksum_name(release, build))).write_text(checksum_text)
    state.base_images[label] = target
    return f"{name} {note}, SHA-256 verified"


def prepare_workdir(config, state=None):
    """Create this run's private directory; refuse to adopt an existing one."""
    state = state or RunState()
    config.run_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        config.run_dir.mkdir(mode=0o700)
    except FileExistsError:
        raise RehearsalError(f"The run directory {config.run_dir} already exists; nothing in it will be "
                             "used or deleted. Choose a new --run-id.") from None
    state.owns_run_dir = True
    config.run_dir.chmod(0o700)
    return str(config.run_dir)


def write_cloud_init(config, label):
    directory = config.run_dir / label
    directory.mkdir(parents=True, exist_ok=True)
    public_key = Path(str(config.key_path) + ".pub").read_text().strip()
    hostname = config.domain_name(label)
    (directory / "user-data").write_text(render_user_data(config.user(label), public_key, hostname))
    (directory / "meta-data").write_text(render_meta_data(hostname, hostname))
    return str(directory)


def make_repo_tarball(repo, output):
    """Archive the checkout (working tree, including uncommitted files) without .git and caches."""
    repo, output = Path(repo), Path(output)
    with tarfile.open(output, "w") as archive:
        for root, directories, files in os.walk(repo):
            directories[:] = sorted(d for d in directories if d not in TARBALL_EXCLUDES)
            for name in sorted(files):
                path = Path(root) / name
                if name.endswith(".pyc") or path == output:
                    continue
                archive.add(path, arcname=path.relative_to(repo).as_posix(), recursive=False)
    return str(output)


def remove_run_files(config, state=None):
    """Delete scratch files of the run directory this run created, unless a VM may still use them."""
    state = state or RunState()
    if not state.owns_run_dir:
        return "kept everything: this run did not create its run directory"
    if state.unconfirmed_domains:
        return ("kept overlays, seed images and the SSH key in " + str(config.run_dir)
                + " because removal of " + ", ".join(sorted(state.unconfirmed_domains))
                + " could not be confirmed")
    removed = []
    names = ["id_ed25519", "id_ed25519.pub", "repo.tar", "bundle.tar"]
    for label in ("src", "dst"):
        names += [f"{label}/user-data", f"{label}/meta-data", f"{label}/seed.iso", f"{label}/disk.qcow2"]
    for name in names:
        path = config.run_dir / name
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(name)
    for label in ("src", "dst"):
        try:
            (config.run_dir / label).rmdir()
        except OSError:
            pass
    return "removed " + (", ".join(removed) if removed else "nothing")


NOT_FOUND_MARKERS = ("failed to get domain", "domain not found")
NOT_RUNNING_MARKERS = ("not running", "is not active")


def _virsh(config, *args):
    return subprocess.run(["virsh", "-c", config.connect, *args], capture_output=True, text=True, check=False,
                          env={**os.environ, "LC_ALL": "C"}, timeout=120)


def _domain_uuid(config, name):
    """The UUID of a defined domain; None when libvirt says there is no such domain."""
    completed = _virsh(config, "domuuid", name)
    if completed.returncode == 0:
        return completed.stdout.strip()
    error = _text(completed.stderr).strip()
    if any(marker in error.lower() for marker in NOT_FOUND_MARKERS):
        return None
    raise RehearsalError(f"Could not query libvirt domain {name}: {error or 'exit ' + str(completed.returncode)}")


def remove_domain(config, state, name):
    """Destroy and undefine NAME, but only if its UUID is the one this run gave it."""
    scoped_domain(config, name)
    label = "src" if name == config.domain_name("src") else "dst"
    ours = config.domain_uuid(label)
    try:
        actual = _domain_uuid(config, name)
        if actual is None:
            return f"{name} is not defined; nothing to remove"
        if actual.lower() != ours.lower():
            raise RehearsalError(f"{name} exists with UUID {actual}, not this run's {ours}; left alone")
        for verb, args, tolerated in (("destroy", ("destroy", name), NOT_RUNNING_MARKERS + NOT_FOUND_MARKERS),
                                      ("undefine", ("undefine", "--nvram", name), NOT_FOUND_MARKERS)):
            completed = _virsh(config, *args)
            error = _text(completed.stderr).strip()
            if completed.returncode != 0 and not any(m in error.lower() for m in tolerated):
                raise RehearsalError(f"virsh {verb} {name} failed: {error or 'exit ' + str(completed.returncode)}")
        if _domain_uuid(config, name) is not None:
            raise RehearsalError(f"{name} is still defined after destroy and undefine")
        return f"removed {name}"
    except RehearsalError:
        state.unconfirmed_domains.add(name)
        raise
    except (OSError, subprocess.SubprocessError) as error:
        state.unconfirmed_domains.add(name)
        raise RehearsalError(f"Could not remove {name}: {error}") from error


# --------------------------------------------------------------------------- fixtures and checks

def source_fixture_script(config):
    files = " ".join(shlex.quote(item) for item in SAMPLE_FILES)
    return f"""set -euo pipefail
cd "$HOME"
cfg=.config/xfce4/xfconf/xfce-perchannel-xml
mkdir -p "$cfg" Documents Pictures .local/bin .local/share/applications .rehearsal-extra/sub .ssh
chmod 700 .ssh
panel=
for candidate in /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml /etc/xdg/xfce4/panel/default.xml; do
  if [ -f "$candidate" ]; then panel=$candidate; break; fi
done
if [ -n "$panel" ]; then
  cp "$panel" {PANEL_XML}
else
  printf '<?xml version="1.0" encoding="UTF-8"?>\\n<channel name="xfce4-panel" version="1.0"/>\\n' > {PANEL_XML}
fi
printf '\\n<!-- fmr-rehearsal customised panel -->\\n' >> {PANEL_XML}
printf '<?xml version="1.0" encoding="UTF-8"?>\\n<channel name="displays" version="1.0"><property name="Default" type="empty"/></channel>\\n' > {DISPLAYS_XML}
printf 'rehearsal note\\n' > Documents/fmr-note.txt
chmod 640 Documents/fmr-note.txt
touch -d '2021-03-04 05:06:07' Documents/fmr-note.txt
seq 1 2000 > Pictures/fmr-pic.dat
printf 'hidden extra\\n' > .rehearsal-extra/sub/data.txt
printf '#!/bin/sh\\necho fmr-hello\\n' > .local/bin/fmr-hello
chmod 755 .local/bin/fmr-hello
ln -sfn fmr-note.txt Documents/fmr-link-internal
ln -sfn /etc/hostname Documents/fmr-link-external
printf '[Desktop Entry]\\nType=Application\\nName=FMR rehearsal\\nExec=%s/.local/bin/fmr-hello\\nIcon=utilities-terminal\\n' "$HOME" > {LAUNCHER}
printf 'fmr-secret-marker\\n' > .ssh/fmr-rehearsal-secret
chmod 600 .ssh/fmr-rehearsal-secret
: > {EXPECTED_FILE}
for f in {files}; do
  printf '%s %s %s %s\\n' "$(sha256sum "$f" | cut -d' ' -f1)" "$(stat -c %a "$f")" "$(stat -c %Y "$f")" "$f" >> {EXPECTED_FILE}
done
echo "fixtures ready"
"""


def dest_fixture_script(config):
    return """set -euo pipefail
cd "$HOME"
mkdir -p Documents
printf 'conflict-original\\n' > Documents/fmr-note.txt
printf 'unrelated\\n' > Documents/fmr-unrelated.txt
echo "destination fixtures ready"
"""


SECRETS_ABSENT_SCRIPT = f"""b={BUNDLE_DIR}
if find "$b" -path '*/.ssh*' | grep -q .; then echo "a .ssh path is in the bundle"; exit 1; fi
if grep -rqa fmr-secret-marker "$b"; then echo "the secret marker is in the bundle"; exit 1; fi
echo "no default secrets in the bundle"
"""

DISPLAYS_ABSENT_SCRIPT = f"""b={BUNDLE_DIR}
find "$b" -name xfce4-panel.xml | grep -q . || {{ echo "panel config missing: check would be vacuous"; exit 1; }}
if find "$b" -name displays.xml | grep -q .; then echo "displays.xml is in the bundle"; exit 1; fi
echo "displays.xml excluded"
"""

PREVIEW_UNCHANGED_SCRIPT = f"""cd "$HOME"
[ "$(cat Documents/fmr-note.txt)" = conflict-original ] || {{ echo "conflict file changed by preview"; exit 1; }}
[ ! -e Pictures/fmr-pic.dat ] || {{ echo "preview created files"; exit 1; }}
[ ! -e {ROLLBACK_DIR} ] || {{ echo "preview created a rollback directory"; exit 1; }}
echo "preview changed nothing"
"""

RESTORED_FILES_TEMPLATE = """cd "$HOME"; fail=0; checked=0
exp=@EXPECTED@; want=@COUNT@
if [ ! -r "$exp" ] || [ ! -s "$exp" ]; then echo "expectation file $exp is missing, unreadable or empty"; exit 1; fi
while read -r sha mode mtime path; do
  checked=$((checked + 1))
  [ -n "$path" ] || { echo "malformed expectation line"; fail=1; continue; }
  [ -f "$path" ] || { echo "MISSING $path"; fail=1; continue; }
  [ "$(sha256sum "$path" | cut -d' ' -f1)" = "$sha" ] || { echo "CONTENT differs: $path"; fail=1; }
  [ "$(stat -c %a "$path")" = "$mode" ] || { echo "MODE differs: $path"; fail=1; }
  [ "$(stat -c %Y "$path")" = "$mtime" ] || { echo "MTIME differs: $path"; fail=1; }
done < "$exp"
if [ "$checked" -le 0 ] || [ "$checked" != "$want" ]; then
  echo "checked $checked entries, expected $want"; fail=1
fi
[ "$fail" = 0 ] || exit 1
echo "all $checked sampled files match"
"""


def restored_files_script(expected_file=EXPECTED_FILE, count=len(SAMPLE_FILES)):
    """Compare restored files with the source's recorded sha256, mode and mtime; fails without expectations."""
    return RESTORED_FILES_TEMPLATE.replace("@EXPECTED@", shlex.quote(str(expected_file))).replace("@COUNT@", str(int(count)))


RESTORED_FILES_SCRIPT = restored_files_script()

SYMLINKS_SCRIPT = """cd "$HOME"
[ -L Documents/fmr-link-internal ] && [ "$(readlink Documents/fmr-link-internal)" = fmr-note.txt ] \\
  || { echo "internal symlink wrong"; exit 1; }
[ -L Documents/fmr-link-external ] && [ "$(readlink Documents/fmr-link-external)" = /etc/hostname ] \\
  || { echo "external symlink wrong"; exit 1; }
echo "symlinks preserved"
"""


def launcher_script(config):
    old = f"/home/{config.source_user}"
    new = f"/home/{config.dest_user}"
    return f"""cd "$HOME"
grep -q '^Exec={new}/.local/bin/fmr-hello$' {LAUNCHER} || {{ echo "launcher not rewritten to {new}"; cat {LAUNCHER}; exit 1; }}
if grep -q {shlex.quote(old)} {LAUNCHER}; then echo "old home still referenced"; exit 1; fi
echo "launcher rewritten"
"""


RESTORE_EXTRAS_SCRIPT = f"""cd "$HOME"
grep -q 'fmr-rehearsal customised panel' {PANEL_XML} || {{ echo "panel customisation missing"; exit 1; }}
[ -x .local/bin/fmr-hello ] || {{ echo "executable lost its mode"; exit 1; }}
[ "$(cat Documents/fmr-unrelated.txt)" = unrelated ] || {{ echo "unrelated file lost"; exit 1; }}
[ ! -e {DISPLAYS_XML} ] || {{ echo "displays.xml was restored"; exit 1; }}
echo "panel, executable and unrelated file as expected"
"""

ROLLBACK_RESTORED_SCRIPT = """cd "$HOME"
[ "$(cat Documents/fmr-note.txt)" = conflict-original ] || { echo "original conflict file not restored"; exit 1; }
[ "$(cat Documents/fmr-unrelated.txt)" = unrelated ] || { echo "unrelated file lost"; exit 1; }
echo "originals restored"
"""

ROLLBACK_REMOVED_SCRIPT = """cd "$HOME"
for f in Pictures/fmr-pic.dat .rehearsal-extra/sub/data.txt .local/bin/fmr-hello Documents/fmr-link-internal Documents/fmr-link-external; do
  if [ -e "$f" ] || [ -L "$f" ]; then echo "still present: $f"; exit 1; fi
done
echo "files created by the restore are gone"
"""


# --------------------------------------------------------------------------- plan

def build_plan(config, state=None):
    """The full ordered list of steps; building it runs nothing."""
    state = state or RunState()
    steps = []
    add = steps.append
    src, dst = config.target("src"), config.target("dst")
    targets = {"src": src, "dst": dst}
    seed_tool = choose_seed_tool() or SEED_TOOLS[0]

    def remote(step_id, description, label, command, **kwargs):
        add(Step(id=step_id, description=description, argv=ssh_argv(targets[label], command), vm=label, **kwargs))

    def script(step_id, description, label, text, **kwargs):
        remote(step_id, description, label, bash_script(text), **kwargs)

    def action(step_id, description, function, **kwargs):
        add(Step(id=step_id, description=description, action=function, **kwargs))

    action("prepare-workdir", f"Create the private run directory {config.run_dir}", lambda: prepare_workdir(config, state))
    add(Step(id="record-tool-commit", description="Record the tool commit under test",
             argv=["git", "-C", str(config.repo_root), "rev-parse", "HEAD"], kind="check", record=True))
    add(Step(id="record-tool-dirty", description="Record uncommitted changes in the checkout",
             argv=["git", "-C", str(config.repo_root), "status", "--porcelain"], kind="check", record=True))
    for label in ("src", "dst"):
        add(Step(id=f"check-domain-absent-{label}", vm=label, expect="nonzero",
                 description=f"Refuse to continue if a libvirt domain named {config.domain_name(label)} exists",
                 argv=["virsh", "-c", config.connect, "domuuid", config.domain_name(label)]))
    if config.source_image is None or config.dest_image is None:
        action("download-images",
               f"Download and SHA-256-verify the Fedora Cloud images for {config.source_release} and "
               f"{config.dest_release} (no GPG check of the CHECKSUM files)",
               lambda: "; ".join(fetch_image(config, label, state) for label in ("src", "dst")
                                 if config.image_override(label) is None))
    add(Step(id="generate-ssh-key", description="Generate an ephemeral SSH key for this run",
             argv=["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"fmr-rehearsal-{config.run_id}",
                   "-f", str(config.key_path)]))

    for label in ("src", "dst"):
        directory = config.run_dir / label
        domain = config.domain_name(label)
        action(f"write-cloud-init-{label}", f"Render NoCloud user-data for user {config.user(label)} ({label})",
               lambda label=label: write_cloud_init(config, label))
        add(Step(id=f"create-seed-{label}", description=f"Build the cloud-init seed image ({label})",
                 argv=seed_command(seed_tool, directory / "seed.iso", directory / "user-data",
                                   directory / "meta-data")))
        def overlay_argv(base, label=label, directory=directory):
            return ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base),
                    str(directory / "disk.qcow2"), f"{config.disk_gib}G"]

        def late_overlay_argv(label=label, overlay_argv=overlay_argv):
            if label not in state.base_images:
                raise RehearsalError(f"The verified base image for {label} is not known yet.")
            return overlay_argv(state.base_images[label])

        pinned = config.pinned_base_image(label)
        add(Step(id=f"create-overlay-{label}", description=f"Create a qcow2 overlay on the build-specific "
                 f"Fedora {config.release(label)} image ({label})",
                 argv=overlay_argv(pinned or config.placeholder_base_image(label)),
                 argv_from=None if pinned else late_overlay_argv))
        add(Step(id=f"create-vm-{label}", description=f"Create and boot VM {domain}",
                 argv=["virt-install", "--connect", config.connect, "--name", domain, "--uuid", str(config.domain_uuid(label)), "--import",
                       "--virt-type", "kvm", "--vcpus", str(config.vcpus), "--memory", str(config.memory_mib),
                       "--osinfo", config.osinfo,
                       "--disk", f"path={directory / 'disk.qcow2'},format=qcow2,bus=virtio",
                       "--disk", f"path={directory / 'seed.iso'},device=cdrom",
                       "--network", "none",
                       "--qemu-commandline",
                       f"-netdev user,id=fwd0,hostfwd=tcp:{SSH_HOST}:{config.ssh_port(label)}-:22 "
                       "-device virtio-net-pci,netdev=fwd0",
                       "--graphics", "none", "--noautoconsole"],
                 vm=label, creates_domain=domain))
    for label in ("src", "dst"):
        remote(f"wait-ssh-{label}", f"Wait for SSH on {label}", label, ["true"], wait=config.ssh_timeout)
        remote(f"cloud-init-{label}", f"Wait for cloud-init to finish on {label}", label,
               ["sudo", "cloud-init", "status", "--wait"], timeout=900)
        remote(f"record-os-{label}", f"Record OS release of {label}", label, ["cat", "/etc/os-release"], record=True)

    for label in ("src", "dst"):
        remote(f"provision-{label}", f"Install XFCE (never started), Flatpak and Python on {label}", label,
               ["sudo", "dnf5", "-y", "install", "@xfce-desktop-environment", "flatpak", "python3", "rsync",
                "procps-ng"], timeout=3600)
        remote(f"console-only-{label}", f"Keep {label} on a console target so XFCE is never started", label,
               ["sudo", "systemctl", "set-default", "multi-user.target"])
        remote(f"record-packages-{label}", f"Record package versions on {label}", label,
               ["rpm", "-q", "xfce4-panel", "dnf5", "python3"], record=True)

    action("make-repo-tarball", "Archive the current checkout without .git",
           lambda: make_repo_tarball(config.repo_root, config.run_dir / "repo.tar"))
    for label in ("src", "dst"):
        add(Step(id=f"upload-repo-{label}", description=f"Copy the checkout archive to {label}",
                 argv=scp_argv(targets[label], config.run_dir / "repo.tar", "/tmp/fmr-repo.tar"), vm=label))
        script(f"extract-repo-{label}", f"Unpack the checkout on {label}", label,
               f"set -e\nrm -rf {REMOTE_REPO}\nmkdir -p {REMOTE_REPO}\ntar -xf /tmp/fmr-repo.tar -C {REMOTE_REPO}\n")

    remote("install-extra-rpm-src", f"Install an extra RPM ({EXTRA_RPM}) on the source", "src",
           ["sudo", "dnf5", "-y", "install", EXTRA_RPM], timeout=1800)
    if config.with_flatpak:
        remote("flatpak-remote-src", "Add flathub as a user remote on the source", "src",
               ["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATPAK_REMOTE_URL])
        remote("flatpak-install-src", f"Install {FLATPAK_APP} as a user Flatpak on the source", "src",
               ["flatpak", "install", "--user", "-y", "flathub", FLATPAK_APP], timeout=1800)
        remote("flatpak-remote-dst", "Add flathub as a user remote on the destination", "dst",
               ["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATPAK_REMOTE_URL])
    script("fixtures-src", "Create the checklist fixtures on the source", "src", source_fixture_script(config))
    script("fixtures-dst", "Create a conflicting and an unrelated file on the destination", "dst",
           dest_fixture_script(config))

    remote("backup-src", "Back up on the source", "src",
           tool_command("backup", "--output", BUNDLE_DIR, "--include", ".rehearsal-extra"), kind="tool",
           expect_output=("Files:",))
    remote("verify-src", "Verify the bundle on the source", "src", tool_command("verify", "--bundle", BUNDLE_DIR),
           kind="tool")
    script("check-default-secrets-absent", "Default secrets are absent from the bundle", "src",
           SECRETS_ABSENT_SCRIPT, kind="check")
    script("check-displays-absent", "displays.xml is absent from the bundle", "src", DISPLAYS_ABSENT_SCRIPT,
           kind="check")
    script("check-rpm-recorded", f"{EXTRA_RPM} is recorded in the manifest", "src",
           f"grep -q {EXTRA_RPM} {BUNDLE_DIR}/manifest.json", kind="check")
    if config.with_flatpak:
        script("check-flatpak-recorded", f"{FLATPAK_APP} is recorded in the manifest", "src",
               f"grep -q {FLATPAK_APP} {BUNDLE_DIR}/manifest.json", kind="check")

    add(Step(id="pull-bundle", description="Copy the bundle from the source to the host (tar over ssh)",
             argv=ssh_argv(src, ["tar", "-C", "/var/tmp", "-cf", "-", "bundle", "fmr-expected.txt"]),
             vm="src", kind="tool", stdout_file=config.run_dir / "bundle.tar"))
    add(Step(id="push-bundle", description="Copy the bundle from the host to the destination",
             argv=ssh_argv(dst, ["tar", "-C", "/var/tmp", "-xpf", "-"]), vm="dst", kind="tool",
             stdin_file=config.run_dir / "bundle.tar"))

    remote("verify-dst", "Verify the transferred bundle on the destination", "dst",
           tool_command("verify", "--bundle", BUNDLE_DIR), kind="tool")
    remote("restore-preview", "Preview the restore on the destination", "dst",
           tool_command("restore", "--bundle", BUNDLE_DIR), kind="tool", expect_output=("Preview only",))
    script("check-preview-unchanged", "The preview changed nothing", "dst", PREVIEW_UNCHANGED_SCRIPT, kind="check")
    remote("restore-apply", "Apply the restore with --rewrite-home", "dst",
           tool_command("restore", "--bundle", BUNDLE_DIR, "--apply", "--rewrite-home",
                        "--rollback-dir", ROLLBACK_DIR), kind="tool", expect_output=("Restored",))
    script("check-restored-files", "Restored files match the source (sha256, mode, mtime)", "dst",
           RESTORED_FILES_SCRIPT, kind="check")
    script("check-symlinks", "Symlinks are preserved", "dst", SYMLINKS_SCRIPT, kind="check")
    script("check-launcher-rewritten", f"The launcher references /home/{config.dest_user}", "dst",
           launcher_script(config), kind="check")
    script("check-restore-extras", "Panel, executable, unrelated file and displays.xml", "dst",
           RESTORE_EXTRAS_SCRIPT, kind="check")
    remote("restore-install-triage", "Show the RPM triage (preview only, nothing is installed)", "dst",
           tool_command("restore", "--bundle", BUNDLE_DIR, "--install-packages"), kind="tool",
           expect_output=("RPM triage", "Preview only"))
    remote("rollback-preview", "Preview the rollback", "dst",
           tool_command("rollback", "--rollback-dir", ROLLBACK_DIR), kind="tool",
           expect_output=("Rollback preview",))
    remote("rollback-apply", "Apply the rollback", "dst",
           tool_command("rollback", "--rollback-dir", ROLLBACK_DIR, "--apply"), kind="tool")
    script("check-rollback-restored", "Rollback restored the original files", "dst", ROLLBACK_RESTORED_SCRIPT,
           kind="check")
    script("check-rollback-removed-new", "Rollback removed files the restore created", "dst",
           ROLLBACK_REMOVED_SCRIPT, kind="check")
    remote("check-second-rollback-refused", "A finished rollback is refused a second time", "dst",
           tool_command("rollback", "--rollback-dir", ROLLBACK_DIR, "--apply"), kind="check", expect="nonzero")
    for label in ("src", "dst"):
        remote(f"record-versions-{label}", f"Record final package versions on {label}", label,
               ["rpm", "-q", "xfce4-panel", "dnf5", "python3"], kind="check", record=True)
    return steps


def scoped_domain(config, name):
    if name not in config.domain_names():
        raise RehearsalError(f"Refusing to touch libvirt domain {name!r}: it is not one of this run's "
                             f"{' / '.join(config.domain_names())}.")
    return name


def cleanup_steps(config, attempted, state=None):
    """Remove only the domains this run started creating (verified by UUID), then its scratch files."""
    if config.keep:
        return []
    state = state or RunState()
    steps = []
    for name in dict.fromkeys(attempted):
        scoped_domain(config, name)
        label = "src" if name == config.domain_name("src") else "dst"
        steps.append(Step(
            id=f"remove-domain-{name}", kind="cleanup", vm=label,
            description=f"Remove VM {name} (virsh destroy, undefine) only if its UUID is "
                        f"{config.domain_uuid(label)}",
            action=lambda name=name: remove_domain(config, state, name)))
    steps.append(Step(id="remove-run-files", kind="cleanup",
                      description="Delete overlays, seed images, the SSH key and archives (kept if a VM "
                                  "could not be confirmed removed)",
                      action=lambda: remove_run_files(config, state)))
    return steps


# --------------------------------------------------------------------------- report

def _summary(results):
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for result in results:
        if result.status in counts:
            counts[result.status] += 1
    return counts


def render_report_json(meta, results):
    counts = _summary(results)
    return json.dumps({
        "passed": counts["failed"] == 0 and counts["skipped"] == 0 and counts["passed"] > 0,
        "summary": counts,
        "meta": meta,
        "steps": [asdict(result) for result in results],
    }, indent=2, sort_keys=True) + "\n"


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _fenced(text):
    return "```\n" + text.replace("```", "'''") + "\n```"


def render_report_markdown(meta, results):
    counts = _summary(results)
    passed = counts["failed"] == 0 and counts["skipped"] == 0 and counts["passed"] > 0
    lines = ["# Fedora migration rehearsal report", "",
             f"- Result: **{'PASSED' if passed else 'FAILED'}**",
             f"- Summary: {counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped"]
    for key in ("run_id", "tool_commit", "tool_dirty", "source_release", "dest_release", "connect", "generated_at"):
        if key in meta:
            lines.append(f"- {key.replace('_', ' ').capitalize()}: {_cell(meta[key]) or '-'}")
    lines += [f"- {CHECKSUM_GPG_NOTE}", "", "## Steps", "",
              "| Step | Description | VM | Status | Detail |", "| --- | --- | --- | --- | --- |"]
    for result in results:
        lines.append(f"| {_cell(result.id)} | {_cell(result.description)} | {_cell(result.vm or '-')} | "
                     f"{result.status} | {_cell(result.detail)} |")
    failed = [result for result in results if result.status == "failed" and result.output_tail]
    if failed:
        lines += ["", "## Output of failed steps", ""]
        for result in failed:
            lines += [f"### {result.id}", "", _fenced(result.output_tail), ""]
    records = meta.get("records") or {}
    if records:
        lines += ["", "## Recorded system information", ""]
        for key in sorted(records):
            lines += [f"### {key}", "", _fenced(records[key]), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- main

def print_kept(config):
    print("VMs and overlays were kept (--keep). Inspect them with:")
    for label in ("src", "dst"):
        target = config.target(label)
        print("  " + shlex.join(ssh_argv(target, "")[:-2]) + f"   # {config.domain_name(label)}")
    print("Remove them later with:")
    for name in config.domain_names():
        print(f"  virsh -c {shlex.quote(config.connect)} destroy {name}; "
              f"virsh -c {shlex.quote(config.connect)} undefine --nvram {name}")
    print(f"Then delete {config.run_dir}.")


def dry_run(config, runner):
    print("DRY RUN: nothing below is executed, downloaded or created. Add --execute to run it.")
    print(CHECKSUM_GPG_NOTE)
    problems = preflight_static(config)
    print("Preflight (an --execute run would " + ("refuse:" if problems else "pass the static checks):"))
    for problem in problems:
        print("  - " + problem)
    print("Preflight would also run: " + shlex.join(["virsh", "-c", config.connect, "uri"]))
    print()
    for step in build_plan(config):
        runner.execute(step)
    print("\n--- cleanup (after the run, also after a failure) ---")
    if config.keep:
        print("--keep given: VMs and overlays would be kept.")
    for step in cleanup_steps(config, config.domain_names()):
        runner.execute(step)
    print(f"\nReports would be written to {config.run_dir}/report.json and report.md")
    return 0


def write_reports(config, runner, state):
    """Write report.json and report.md into the run directory this run created; returns the paths."""
    base_images = {}
    for label in ("src", "dst"):
        image = config.pinned_base_image(label) or state.base_images.get(label)
        base_images[label] = str(image) if image else None
    meta = {
        "run_id": config.run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool_commit": runner.records.get("record-tool-commit", "unknown"),
        "tool_dirty": "yes" if runner.records.get("record-tool-dirty") else "no",
        "source_release": config.source_release,
        "dest_release": config.dest_release,
        "connect": config.connect,
        "checksum_gpg_verified": False,
        "base_images": base_images,
        "records": runner.records,
    }
    json_text = render_report_json(meta, runner.results)
    markdown_text = render_report_markdown(meta, runner.results)
    if not state.owns_run_dir:
        print("\nThe run directory was not created by this run, so no report file is written. Report:\n")
        print(markdown_text)
        return None, json.loads(json_text)["passed"]
    json_path, markdown_path = config.run_dir / "report.json", config.run_dir / "report.md"
    json_path.write_text(json_text)
    markdown_path.write_text(markdown_text)
    print(f"Report: {markdown_path}\n        {json_path}")
    return markdown_path, json.loads(json_text)["passed"]


def execute(config, runner):
    problems = preflight_static(config)
    if not problems:
        problems = preflight_libvirt(config)
    if problems:
        print("Refusing to run the rehearsal:", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 2
    print(CHECKSUM_GPG_NOTE)
    state = RunState()
    interrupted = False
    passed = False
    try:
        try:
            run_plan(build_plan(config, state), runner)
        except KeyboardInterrupt:
            interrupted = True
            print("Interrupted; cleaning up.", file=sys.stderr)
        except Exception as error:
            runner.results.append(StepResult(
                id="unexpected-error", description="Unexpected error while running the plan", kind="setup",
                vm=None, command="", status="failed", returncode=None,
                detail=f"{type(error).__name__}: {error}", output_tail="", duration=0.0))
            print(f"Unexpected error: {type(error).__name__}: {error}", file=sys.stderr)
        finally:
            try:
                for step in cleanup_steps(config, runner.attempted_domains, state):
                    runner.execute(step)
            except KeyboardInterrupt:
                interrupted = True
                print("Cleanup interrupted; remaining run files were kept.", file=sys.stderr)
    finally:
        if interrupted:
            runner.results.append(StepResult(
                id="interrupted", description="Rehearsal interrupted before completion", kind="setup",
                vm=None, command="", status="failed", returncode=130,
                detail="Interrupted by user; a complete rehearsal was not recorded.",
                output_tail="", duration=0.0))
        _, passed = write_reports(config, runner, state)
        counts = _summary(runner.results)
        print(f"\n{counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped.")
        if state.unconfirmed_domains:
            print("Could not confirm removal of: " + ", ".join(sorted(state.unconfirmed_domains))
                  + ". Overlays, seed images and the SSH key were kept; inspect with virsh and remove by hand.")
    if config.keep:
        print_kept(config)
    if interrupted:
        return 130
    return 0 if passed else 1


def main(argv=None):
    config = parse_args(argv)
    try:
        if not config.execute:
            return dry_run(config, Runner(dry_run=True))
        return execute(config, Runner(dry_run=False))
    except RehearsalError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
