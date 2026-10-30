"""Tests for scripts/rehearsal.py; no VM, libvirt or network is ever used."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rehearsal.py"


def load_script():
    spec = importlib.util.spec_from_file_location("rehearsal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["rehearsal"] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


rehearsal = load_script()

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyMaterialForTests fmr-rehearsal"
CHECKSUM_TEXT = """-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA256

# Fedora-Cloud-Base-AmazonEC2-43-1.6.x86_64.raw.xz: 592568816 bytes
SHA256 (Fedora-Cloud-Base-AmazonEC2-43-1.6.x86_64.raw.xz) = 91a3fa8f5fd870ca4de56c24df97ceb4f8bd806c2e01a1a8a4fa780395a84bfc
# Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2: 583335936 bytes
SHA256 (Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2) = 846574c8a97cd2d8dc1f231062d73107cc85cbbbda56335e264a46e3a6c8ab2f
-----BEGIN PGP SIGNATURE-----

iQIzBAEBCAAdFiEExufwgc+A4TFGZ26IgptgZjFkVTEFAmj9DpIACgkQgptgZjFk
=hjFD
-----END PGP SIGNATURE-----
"""


def make_config(workdir, *extra):
    return rehearsal.parse_args(["--workdir", str(workdir), "--run-id", "abc123", *extra])


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def all_argvs(steps):
    return [step.argv for step in steps if step.argv]


class FakeLibvirt:
    """A tiny stand-in for virsh/virt-install state: domain name -> UUID."""

    NOT_FOUND = "error: failed to get domain '{}'\n"

    def __init__(self, domains=None, foreign_on_create=False, destroy_error=None, destroy_not_running=False):
        self.domains = dict(domains or {})
        self.foreign_on_create = foreign_on_create
        self.destroy_error = destroy_error
        self.destroy_not_running = destroy_not_running
        self.installs = []

    def handle(self, argv, install_fails=False):
        """A CompletedProcess for libvirt commands, or None for anything else."""
        if argv[0] == "virt-install":
            name, uuid = argv[argv.index("--name") + 1], argv[argv.index("--uuid") + 1]
            self.installs.append(name)
            if self.foreign_on_create:
                self.domains[name] = "99999999-9999-9999-9999-999999999999"
                return completed(argv, 1, "", "ERROR: domain already exists")
            self.domains[name] = uuid
            return completed(argv, 1 if install_fails else 0, "", "boom" if install_fails else "")
        if argv[0] != "virsh" or len(argv) < 5:
            return None
        verb, name = argv[3], argv[-1]
        if verb == "domuuid":
            if name in self.domains:
                return completed(argv, 0, self.domains[name] + "\n")
            return completed(argv, 1, "", self.NOT_FOUND.format(name))
        if verb == "destroy":
            if name not in self.domains:
                return completed(argv, 1, "", self.NOT_FOUND.format(name))
            if self.destroy_error and name in self.destroy_error:
                return completed(argv, 1, "", "error: internal error: qemu is stuck")
            if self.destroy_not_running:
                return completed(argv, 1, "", "error: Requested operation is not valid: domain is not running")
            return completed(argv, 0)
        if verb == "undefine":
            if name not in self.domains:
                return completed(argv, 1, "", self.NOT_FOUND.format(name))
            del self.domains[name]
            return completed(argv, 0)
        return None



class DryRunDefaultTests(unittest.TestCase):
    def test_default_is_dry_run(self):
        config = rehearsal.parse_args([])
        self.assertFalse(config.execute)

    def test_execute_flag_enables_real_runs(self):
        self.assertTrue(rehearsal.parse_args(["--execute"]).execute)

    def test_dry_run_prints_commands_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            out = io.StringIO()
            with patch.object(subprocess, "run") as run, patch.object(subprocess, "Popen") as popen, \
                    patch.object(subprocess, "check_call") as check_call, \
                    patch.object(subprocess, "check_output") as check_output, \
                    contextlib.redirect_stdout(out):
                code = rehearsal.main(["--workdir", str(workdir), "--run-id", "abc123"])
            self.assertEqual(code, 0)
            for mock in (run, popen, check_call, check_output):
                mock.assert_not_called()
            self.assertFalse(workdir.exists(), "dry-run must not create anything")
        text = out.getvalue()
        self.assertIn("DRY RUN", text)
        for expected in ("virt-install", "fmr-src-abc123", "fmr-dst-abc123", "qemu-img",
                         "fedora-migration.py backup --output /var/tmp/bundle",
                         "--rewrite-home", "--rollback-dir /var/tmp/rollback-1",
                         "virsh"):
            self.assertIn(expected, text)

    def test_dry_run_does_not_fail_on_missing_kvm_or_tools(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(rehearsal, "which", return_value=None), \
                patch.object(rehearsal, "kvm_present", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = rehearsal.main(["--workdir", str(Path(tmp) / "w")])
        self.assertEqual(code, 0)
        self.assertIn("/dev/kvm", out.getvalue())

    def test_rejects_unsafe_run_id(self):
        for bad in ("a b", "../x", "A;rm", "", "x" * 40):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    rehearsal.parse_args(["--run-id", bad])


class PreflightTests(unittest.TestCase):
    def config(self, *extra):
        return make_config("/tmp/fmr-test-unused", *extra)

    def test_reports_missing_kvm(self):
        with patch.object(rehearsal, "which", return_value="/usr/bin/x"), \
                patch.object(rehearsal, "kvm_present", return_value=False):
            problems = rehearsal.preflight_static(self.config())
        self.assertTrue(any("/dev/kvm" in problem for problem in problems))

    def test_reports_each_missing_tool(self):
        present = {"ssh", "curl"}
        with patch.object(rehearsal, "which", side_effect=lambda name: f"/usr/bin/{name}" if name in present else None), \
                patch.object(rehearsal, "kvm_present", return_value=True):
            problems = "\n".join(rehearsal.preflight_static(self.config()))
        for tool in ("virt-install", "virsh", "qemu-img", "scp", "sha256sum"):
            self.assertIn(tool, problems)
        self.assertNotIn("curl", problems)

    def test_seed_tool_alternatives(self):
        def only(*names):
            return lambda name: f"/usr/bin/{name}" if name in names or name in rehearsal.BASE_TOOLS else None
        with patch.object(rehearsal, "kvm_present", return_value=True):
            with patch.object(rehearsal, "which", side_effect=only()):
                self.assertTrue(any("cloud-localds" in p for p in rehearsal.preflight_static(self.config())))
            for tool in ("cloud-localds", "genisoimage", "xorriso"):
                with patch.object(rehearsal, "which", side_effect=only(tool)):
                    self.assertEqual(rehearsal.preflight_static(self.config()), [])

    def test_seed_command_per_tool(self):
        files = ("/w/user-data", "/w/meta-data")
        self.assertEqual(rehearsal.seed_command("cloud-localds", "/w/seed.iso", *files)[:2],
                         ["cloud-localds", "/w/seed.iso"])
        for tool in ("genisoimage", "xorriso"):
            argv = rehearsal.seed_command(tool, "/w/seed.iso", *files)
            self.assertEqual(argv[0], tool)
            self.assertIn("cidata", argv)
            self.assertIn("/w/seed.iso", argv)

    def test_execute_refuses_without_kvm_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with patch.object(rehearsal, "which", return_value="/usr/bin/x"), \
                    patch.object(rehearsal, "kvm_present", return_value=False), \
                    patch.object(subprocess, "run") as run, \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = rehearsal.main(["--execute", "--workdir", str(Path(tmp) / "w")])
            self.assertEqual(code, 2)
            run.assert_not_called()
            self.assertFalse((Path(tmp) / "w").exists())
        self.assertIn("/dev/kvm", err.getvalue())
        self.assertIn("Refusing", err.getvalue())

    def test_execute_refuses_without_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with patch.object(rehearsal, "which", return_value=None), \
                    patch.object(rehearsal, "kvm_present", return_value=True), \
                    patch.object(subprocess, "run") as run, \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = rehearsal.main(["--execute", "--workdir", str(Path(tmp) / "w")])
            self.assertEqual(code, 2)
            run.assert_not_called()
        self.assertIn("virt-install", err.getvalue())

    def test_execute_refuses_when_libvirt_unreachable(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return completed(argv, 1, "", "failed to connect")

        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with patch.object(rehearsal, "which", return_value="/usr/bin/x"), \
                    patch.object(rehearsal, "kvm_present", return_value=True), \
                    patch.object(subprocess, "run", side_effect=fake_run), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = rehearsal.main(["--execute", "--workdir", str(Path(tmp) / "w"),
                                       "--connect", "qemu:///system"])
        self.assertEqual(code, 2)
        self.assertEqual(calls, [["virsh", "-c", "qemu:///system", "uri"]])
        self.assertIn("libvirt", err.getvalue())

    def test_default_connection_is_session(self):
        self.assertEqual(rehearsal.parse_args([]).connect, "qemu:///session")


class ImageTests(unittest.TestCase):
    def test_urls_for_43_and_44(self):
        self.assertEqual(
            rehearsal.index_url(43),
            "https://download.fedoraproject.org/pub/fedora/linux/releases/43/Cloud/x86_64/images/")
        self.assertEqual(
            rehearsal.image_url(44, "1.7"),
            "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/"
            "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2")
        self.assertEqual(
            rehearsal.checksum_url(43, "1.6"),
            "https://download.fedoraproject.org/pub/fedora/linux/releases/43/Cloud/x86_64/images/"
            "Fedora-Cloud-43-1.6-x86_64-CHECKSUM")

    def test_discover_build_from_index(self):
        html = ('<a href="Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2">x</a>'
                '<a href="Fedora-Cloud-Base-AmazonEC2-43-1.6.x86_64.raw.xz">y</a>'
                '<a href="Fedora-Cloud-43-1.6-x86_64-CHECKSUM">z</a>')
        self.assertEqual(rehearsal.discover_build(html, 43), "1.6")
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.discover_build(html, 44)

    def test_discover_build_picks_newest(self):
        html = ("Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2 Fedora-Cloud-Base-Generic-43-1.10.x86_64.qcow2 "
                "Fedora-Cloud-Base-Generic-43-1.9.x86_64.qcow2")
        self.assertEqual(rehearsal.discover_build(html, 43), "1.10")

    def test_parse_checksum_reads_bsd_lines_only(self):
        parsed = rehearsal.parse_checksum(CHECKSUM_TEXT)
        self.assertEqual(parsed["Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2"],
                         "846574c8a97cd2d8dc1f231062d73107cc85cbbbda56335e264a46e3a6c8ab2f")
        self.assertEqual(len(parsed), 2)

    def test_parse_checksum_rejects_duplicates_and_empty(self):
        line = "SHA256 (a.qcow2) = " + "0" * 64
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.parse_checksum(line + "\n" + line.replace("0" * 64, "1" * 64))
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.parse_checksum("# nothing here\n")

    def test_expected_sha256_missing_file(self):
        parsed = rehearsal.parse_checksum(CHECKSUM_TEXT)
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.expected_sha256(parsed, "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2")

    def test_verify_image_accepts_match_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "image.qcow2"
            image.write_bytes(b"not really a qcow2")
            good = hashlib.sha256(b"not really a qcow2").hexdigest()
            rehearsal.verify_image(image, good)
            with self.assertRaisesRegex(rehearsal.RehearsalError, "SHA-256 mismatch"):
                rehearsal.verify_image(image, "0" * 64)


class CloudInitTests(unittest.TestCase):
    def test_user_data_contains_public_key_and_no_private_key(self):
        text = rehearsal.render_user_data("alice", PUBLIC_KEY, "fmr-src-abc123")
        self.assertTrue(text.startswith("#cloud-config\n"))
        self.assertIn(PUBLIC_KEY, text)
        self.assertIn("name: alice", text)
        self.assertNotIn("PRIVATE KEY", text)
        self.assertIn("NOPASSWD", text)
        self.assertIn("fmr-src-abc123", text)

    def test_user_data_rejects_unsafe_input(self):
        for key in ("ssh-ed25519 AAAA\nruncmd: [rm]", "-----BEGIN OPENSSH PRIVATE KEY-----", "not a key"):
            with self.assertRaises(rehearsal.RehearsalError):
                rehearsal.render_user_data("alice", key, "h")
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.render_user_data("al ice", PUBLIC_KEY, "h")

    def test_users_differ_between_vms(self):
        config = make_config("/tmp/x")
        plan = rehearsal.build_plan(config)
        self.assertEqual((config.source_user, config.dest_user), ("alice", "bob"))
        text = "\n".join(shlex.join(a) for a in all_argvs(plan))
        self.assertIn("alice@127.0.0.1", text)
        self.assertIn("bob@127.0.0.1", text)

    def test_meta_data(self):
        text = rehearsal.render_meta_data("fmr-src-abc123", "fmr-src-abc123")
        self.assertIn("instance-id: fmr-src-abc123", text)
        self.assertIn("local-hostname: fmr-src-abc123", text)


class RemoteCommandTests(unittest.TestCase):
    def target(self):
        return rehearsal.Target(label="src", user="alice", port=2222, key=Path("/work dir/key"))

    def test_ssh_argv_quotes_paths_with_spaces(self):
        argv = rehearsal.ssh_argv(self.target(), ["ls", "-l", "/var/tmp/my bundle", "it's"])
        self.assertEqual(argv[0], "ssh")
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") - 1], "alice@127.0.0.1")
        remote = argv[-1]
        self.assertEqual(shlex.split(remote), ["ls", "-l", "/var/tmp/my bundle", "it's"])
        self.assertIn("/work dir/key", argv)  # a single argv element, never re-split
        self.assertIn("2222", argv)

    def test_ssh_argv_is_non_interactive_and_ephemeral(self):
        text = " ".join(rehearsal.ssh_argv(self.target(), ["true"]))
        for option in ("BatchMode=yes", "StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null",
                       "IdentitiesOnly=yes"):
            self.assertIn(option, text)

    def test_tool_command_quotes_bundle_path(self):
        command = rehearsal.tool_command("verify", "--bundle", "/var/tmp/my bundle")
        self.assertEqual(shlex.split(command)[-2:], ["--bundle", "/var/tmp/my bundle"])
        self.assertTrue(command.startswith("cd "))

    def test_shell_script_wrapper_round_trips(self):
        script = "set -e\nprintf '%s\\n' \"it's $HOME\"\n"
        argv = rehearsal.ssh_argv(self.target(), rehearsal.bash_script(script))
        words = shlex.split(argv[-1])
        self.assertEqual(words[:2], ["bash", "-c"])
        self.assertEqual(words[2], script)

    def test_plan_has_no_shell_strings(self):
        plan = rehearsal.build_plan(make_config("/tmp/x"))
        for step in plan:
            if step.argv:
                self.assertIsInstance(step.argv, list)
                self.assertTrue(all(isinstance(item, str) for item in step.argv))
                if step.argv[0] == "ssh":
                    self.assertIn("--", step.argv)


class PlanTests(unittest.TestCase):
    def plan(self, *extra):
        return rehearsal.build_plan(make_config("/tmp/fmr-plan", *extra))

    def commands(self, plan):
        return [rehearsal.display_command(step) for step in plan]

    def index_of(self, plan, needle, start=0):
        for index, step in enumerate(plan[start:], start):
            if needle in rehearsal.display_command(step):
                return index
        self.fail(f"{needle!r} not found in plan")

    def test_tool_steps_run_in_documented_order(self):
        plan = self.plan()
        backup = self.index_of(plan, "backup --output /var/tmp/bundle")
        verify = self.index_of(plan, "verify --bundle /var/tmp/bundle", backup)
        preview = self.index_of(plan, "restore --bundle /var/tmp/bundle", verify + 1)
        apply = self.index_of(plan, "--apply --rewrite-home --rollback-dir /var/tmp/rollback-1", preview)
        triage = self.index_of(plan, "--install-packages", apply)
        rb_preview = self.index_of(plan, "rollback --rollback-dir /var/tmp/rollback-1", triage)
        rb_apply = self.index_of(plan, "rollback --rollback-dir /var/tmp/rollback-1 --apply", rb_preview + 1)
        self.assertLess(preview, apply)
        self.assertLess(triage, rb_preview)
        self.assertLess(rb_preview, rb_apply)

    def test_triage_step_never_applies_or_installs(self):
        plan = self.plan()
        triage = [s for s in plan if "--install-packages" in rehearsal.display_command(s)]
        self.assertEqual(len(triage), 1)
        self.assertNotIn("--apply", rehearsal.display_command(triage[0]))
        self.assertIn("RPM triage", triage[0].expect_output[0])

    def test_provisioning_installs_xfce_but_never_starts_it(self):
        text = "\n".join(self.commands(self.plan()))
        self.assertIn("dnf5 -y install @xfce-desktop-environment flatpak python3 rsync", text)
        self.assertIn("multi-user.target", text)
        for forbidden in ("startxfce4", "xfce4-session", "graphical.target", "lightdm"):
            self.assertNotIn(forbidden, text)

    def test_flatpak_fixture_is_opt_in(self):
        self.assertNotIn("flathub", "\n".join(self.commands(self.plan())))
        self.assertIn("flathub", "\n".join(self.commands(self.plan("--with-flatpak"))))

    def test_extra_rpm_fixture_installed_on_source_only(self):
        plan = self.plan()
        cowsay = [s for s in plan if "cowsay" in rehearsal.display_command(s)]
        self.assertTrue(cowsay)
        self.assertTrue(all(s.vm == "src" for s in cowsay))

    def test_bundle_moves_via_host_with_tar(self):
        plan = self.plan()
        pull = [s for s in plan if s.stdout_file]
        push = [s for s in plan if s.stdin_file and "bundle" in str(s.stdin_file)]
        self.assertTrue(pull and push)
        self.assertEqual(Path(pull[0].stdout_file), Path(push[0].stdin_file))

    def test_displays_xml_and_secrets_checks_present(self):
        text = "\n".join(self.commands(self.plan()))
        self.assertIn("displays.xml", text)
        self.assertIn(".ssh", text)

    def test_settings_checks_cover_rewrite_symlinks_and_rollback(self):
        ids = {step.id for step in self.plan()}
        for expected in ("check-default-secrets-absent", "check-displays-absent", "check-restored-files",
                         "check-symlinks", "check-launcher-rewritten", "check-rollback-restored",
                         "check-rollback-removed-new", "check-second-rollback-refused"):
            self.assertIn(expected, ids)

    def test_step_ids_unique(self):
        ids = [step.id for step in self.plan()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_second_rollback_expects_failure(self):
        step = next(s for s in self.plan() if s.id == "check-second-rollback-refused")
        self.assertEqual(step.expect, "nonzero")

    def test_tool_commit_recorded(self):
        self.assertTrue(any(s.id == "record-tool-commit" for s in self.plan()))

    def test_overrides_skip_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "s.qcow2"
            dst = Path(tmp) / "d.qcow2"
            plan = self.plan("--source-image", str(src), "--dest-image", str(dst))
            text = "\n".join(self.commands(plan))
            self.assertNotIn("curl", text)
            self.assertIn(str(src), text)
            self.assertIn(str(dst), text)

    def test_default_downloads_use_curl(self):
        self.assertIn("download-images", {s.id for s in self.plan()})

    def test_virt_install_defaults(self):
        step = next(s for s in self.plan() if s.id == "create-vm-src")
        text = shlex.join(step.argv)
        for expected in ("--import", "--vcpus 2", "--memory 4096", "--osinfo fedora-unknown",
                         "--name fmr-src-abc123", "--noautoconsole", "--connect qemu:///session"):
            self.assertIn(expected, text)
        self.assertEqual(step.creates_domain, "fmr-src-abc123")

    def test_ssh_ports_are_distinct(self):
        config = make_config("/tmp/x")
        self.assertNotEqual(config.ssh_port("src"), config.ssh_port("dst"))


class DomainScopingTests(unittest.TestCase):
    def test_domain_names(self):
        config = make_config("/tmp/x")
        self.assertEqual(config.domain_name("src"), "fmr-src-abc123")
        self.assertEqual(config.domain_name("dst"), "fmr-dst-abc123")

    def run_cleanup(self, config, libvirt, created, state=None):
        state = state or rehearsal.RunState()
        runner = rehearsal.Runner(dry_run=False, out=lambda text: None)
        with patch.object(subprocess, "run", side_effect=lambda argv, **kw: libvirt.handle(argv)):
            results = [runner.execute(step) for step in rehearsal.cleanup_steps(config, created, state)]
        return results, state

    def spy(self, libvirt):
        calls = []
        original = libvirt.handle
        libvirt.handle = lambda argv, **kw: (calls.append(list(argv)), original(argv))[1]
        return calls

    def test_cleanup_targets_only_created_domains(self):
        config = make_config("/tmp/x")
        libvirt = FakeLibvirt({"fmr-src-abc123": str(config.domain_uuid("src")),
                               "fmr-dst-abc123": str(config.domain_uuid("dst"))})
        calls = self.spy(libvirt)
        results, _ = self.run_cleanup(config, libvirt, ["fmr-src-abc123"])
        self.assertTrue(all(r.status == "passed" for r in results), results)
        self.assertEqual(set(libvirt.domains), {"fmr-dst-abc123"})
        flat = " ".join(" ".join(c) for c in calls)
        self.assertIn("destroy", flat)
        self.assertIn("undefine", flat)
        self.assertNotIn("fmr-dst-abc123", flat)
        self.assertNotIn("--all", flat)
        for argv in calls:
            self.assertEqual(argv[:3], ["virsh", "-c", "qemu:///session"])

    def test_cleanup_leaves_domain_with_foreign_uuid_alone(self):
        config = make_config("/tmp/x")
        libvirt = FakeLibvirt({"fmr-src-abc123": "11111111-1111-1111-1111-111111111111"})
        calls = self.spy(libvirt)
        results, state = self.run_cleanup(config, libvirt, ["fmr-src-abc123"])
        self.assertIn("fmr-src-abc123", libvirt.domains)
        self.assertFalse([c for c in calls if c[3] in ("destroy", "undefine")])
        self.assertIn("failed", [r.status for r in results])
        self.assertIn("fmr-src-abc123", state.unconfirmed_domains)

    def test_cleanup_of_partially_created_own_domain_succeeds(self):
        config = make_config("/tmp/x")
        libvirt = FakeLibvirt({"fmr-dst-abc123": str(config.domain_uuid("dst"))})
        results, state = self.run_cleanup(config, libvirt, ["fmr-dst-abc123"])
        self.assertEqual(libvirt.domains, {})
        self.assertFalse(state.unconfirmed_domains)
        self.assertTrue(all(r.status == "passed" for r in results))

    def test_cleanup_of_absent_domain_is_fine_and_runs_no_destroy(self):
        config = make_config("/tmp/x")
        libvirt = FakeLibvirt()
        calls = self.spy(libvirt)
        results, state = self.run_cleanup(config, libvirt, ["fmr-src-abc123"])
        self.assertTrue(all(r.status == "passed" for r in results))
        self.assertFalse([c for c in calls if c[3] in ("destroy", "undefine")])
        self.assertFalse(state.unconfirmed_domains)

    def test_cleanup_tolerates_domain_not_running(self):
        config = make_config("/tmp/x")
        libvirt = FakeLibvirt({"fmr-src-abc123": str(config.domain_uuid("src"))}, destroy_not_running=True)
        results, state = self.run_cleanup(config, libvirt, ["fmr-src-abc123"])
        self.assertEqual(libvirt.domains, {})
        self.assertTrue(all(r.status == "passed" for r in results), results)

    def test_cleanup_failure_is_a_failed_step_and_keeps_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp) / "w")
            state = rehearsal.RunState()
            rehearsal.prepare_workdir(config, state)
            (config.run_dir / "id_ed25519").write_text("key")
            (config.run_dir / "report.json").write_text("{}")
            libvirt = FakeLibvirt({"fmr-src-abc123": str(config.domain_uuid("src"))},
                                  destroy_error={"fmr-src-abc123"})
            results, state = self.run_cleanup(config, libvirt, ["fmr-src-abc123"], state)
            self.assertIn("failed", [r.status for r in results])
            self.assertIn("fmr-src-abc123", libvirt.domains)
            self.assertTrue((config.run_dir / "id_ed25519").exists(), "key must be kept")
            files_step = next(r for r in results if r.id == "remove-run-files")
            self.assertIn("kept", files_step.detail)

    def test_cleanup_removes_artifacts_when_domains_confirmed_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp) / "w")
            state = rehearsal.RunState()
            rehearsal.prepare_workdir(config, state)
            (config.run_dir / "id_ed25519").write_text("key")
            (config.run_dir / "report.json").write_text("{}")
            libvirt = FakeLibvirt({"fmr-src-abc123": str(config.domain_uuid("src"))})
            self.run_cleanup(config, libvirt, ["fmr-src-abc123"], state)
            self.assertFalse((config.run_dir / "id_ed25519").exists())
            self.assertTrue((config.run_dir / "report.json").exists())

    def test_cleanup_does_not_delete_files_of_a_run_dir_it_did_not_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp) / "w")
            config.run_dir.mkdir(parents=True)
            (config.run_dir / "id_ed25519").write_text("someone else's")
            self.run_cleanup(config, FakeLibvirt(), [], rehearsal.RunState())
            self.assertTrue((config.run_dir / "id_ed25519").exists())

    def test_cleanup_refuses_foreign_domains(self):
        config = make_config("/tmp/x")
        for foreign in ("prod-db", "fmr-src-other", "fmr-dst-abc123 extra", "fmr-src-abc123;x", "fmr-xxx-abc123"):
            with self.assertRaises(rehearsal.RehearsalError, msg=foreign):
                rehearsal.cleanup_steps(config, [foreign])

    def test_cleanup_with_nothing_created_runs_no_virsh(self):
        config = make_config("/tmp/x")
        argvs = all_argvs(rehearsal.cleanup_steps(config, []))
        self.assertFalse([a for a in argvs if a[0] == "virsh"])

    def test_no_planned_command_lists_or_wildcards_domains(self):
        config = make_config("/tmp/x")
        for step in rehearsal.build_plan(config) + rehearsal.cleanup_steps(config, list(config.domain_names())):
            if step.argv and step.argv[0] == "virsh":
                self.assertNotIn("list", step.argv)

    def test_keep_skips_domain_cleanup(self):
        config = make_config("/tmp/x", "--keep")
        self.assertEqual(rehearsal.cleanup_steps(config, list(config.domain_names())), [])


class RunnerTests(unittest.TestCase):
    def test_dry_run_runner_prints_and_does_not_execute(self):
        calls = []
        out = io.StringIO()
        runner = rehearsal.Runner(dry_run=True, run=lambda *a, **k: calls.append(a), out=out.write)
        step = rehearsal.Step(id="x", description="Do a thing", argv=["echo", "hello world"])
        result = runner.execute(step)
        self.assertEqual(calls, [])
        self.assertEqual(result.status, "dry-run")
        self.assertIn("echo 'hello world'", out.getvalue())

    def test_expectations(self):
        def runner_for(code, stdout=""):
            return rehearsal.Runner(dry_run=False, run=lambda argv, **kw: completed(argv, code, stdout),
                                    out=lambda text: None)
        step = rehearsal.Step(id="x", description="d", argv=["true"])
        self.assertEqual(runner_for(0).execute(step).status, "passed")
        self.assertEqual(runner_for(1).execute(step).status, "failed")
        nonzero = rehearsal.Step(id="y", description="d", argv=["false"], expect="nonzero")
        self.assertEqual(runner_for(1).execute(nonzero).status, "passed")
        self.assertEqual(runner_for(0).execute(nonzero).status, "failed")
        wants = rehearsal.Step(id="z", description="d", argv=["true"], expect_output=("RPM triage",))
        self.assertEqual(runner_for(0, "xx RPM triage yy").execute(wants).status, "passed")
        result = runner_for(0, "nothing").execute(wants)
        self.assertEqual(result.status, "failed")
        self.assertIn("RPM triage", result.detail)

    def test_wait_retries_until_success(self):
        outcomes = iter([255, 255, 0])
        clock = {"now": 0.0}
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds

        runner = rehearsal.Runner(dry_run=False, run=lambda argv, **kw: completed(argv, next(outcomes)),
                                  out=lambda text: None, sleep=fake_sleep, clock=lambda: clock["now"])
        step = rehearsal.Step(id="w", description="wait", argv=["ssh", "x"], wait=60)
        self.assertEqual(runner.execute(step).status, "passed")
        self.assertEqual(len(sleeps), 2)

    def test_wait_gives_up_at_timeout(self):
        clock = {"now": 0.0}

        def fake_sleep(seconds):
            clock["now"] += seconds

        runner = rehearsal.Runner(dry_run=False, run=lambda argv, **kw: completed(argv, 255),
                                  out=lambda text: None, sleep=fake_sleep, clock=lambda: clock["now"])
        step = rehearsal.Step(id="w", description="wait", argv=["ssh", "x"], wait=20)
        result = runner.execute(step)
        self.assertEqual(result.status, "failed")
        self.assertGreaterEqual(clock["now"], 20)

    def test_action_step_runs_callable_and_failure_is_reported(self):
        runner = rehearsal.Runner(dry_run=False, run=None, out=lambda text: None)
        ok = rehearsal.Step(id="a", description="act", action=lambda: "did it")
        self.assertEqual(runner.execute(ok).detail, "did it")

        def boom():
            raise rehearsal.RehearsalError("kaput")

        bad = runner.execute(rehearsal.Step(id="b", description="act", action=boom))
        self.assertEqual(bad.status, "failed")
        self.assertIn("kaput", bad.detail)

    def test_domain_ownership_recorded_only_after_success(self):
        step = rehearsal.Step(id="v", description="d", argv=["virt-install"], kind="setup",
                              creates_domain="fmr-src-abc123")
        failing = rehearsal.Runner(dry_run=False, run=lambda argv, **kw: completed(argv, 1), out=lambda t: None)
        failing.execute(step)
        self.assertEqual(failing.created_domains, [])
        self.assertEqual(failing.attempted_domains, ["fmr-src-abc123"])
        working = rehearsal.Runner(dry_run=False, run=lambda argv, **kw: completed(argv, 0), out=lambda t: None)
        working.execute(step)
        self.assertEqual(working.created_domains, ["fmr-src-abc123"])
        self.assertEqual(working.attempted_domains, ["fmr-src-abc123"])

    def test_binary_transfer_stderr_is_decoded_not_crashing(self):
        def fake_run(argv, **kw):
            kw["stdout"].write(b"tar-bytes")
            return subprocess.CompletedProcess(argv, 1, None, b"tar: \xff\xfe cannot stat\n")

        with tempfile.TemporaryDirectory() as tmp:
            runner = rehearsal.Runner(dry_run=False, run=fake_run, out=lambda text: None)
            result = runner.execute(rehearsal.Step(id="pull", description="pull", argv=["ssh"],
                                                   stdout_file=Path(tmp) / "b.tar", kind="tool"))
        self.assertEqual(result.status, "failed")
        self.assertIn("cannot stat", result.output_tail)
        self.assertEqual(result.returncode, 1)

    def test_str_stdout_and_bytes_stderr_mix(self):
        runner = rehearsal.Runner(
            dry_run=False, out=lambda text: None,
            run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "out\n", b"err\n"))
        result = runner.execute(rehearsal.Step(id="m", description="m", argv=["x"]))
        self.assertIn("out", result.output_tail)
        self.assertIn("err", result.output_tail)

    def test_unexpected_exception_in_a_step_becomes_a_failed_result(self):
        def boom():
            raise TypeError("unexpected")

        runner = rehearsal.Runner(dry_run=False, out=lambda text: None)
        result = runner.execute(rehearsal.Step(id="b", description="b", action=boom))
        self.assertEqual(result.status, "failed")
        self.assertIn("unexpected", result.detail)

    def test_run_plan_aborts_after_fatal_failure_but_continues_after_check_failure(self):
        def fake_run(argv, **kw):
            return completed(argv, 1 if argv[0] in ("bad-tool", "bad-check") else 0)

        runner = rehearsal.Runner(dry_run=False, run=fake_run, out=lambda text: None)
        steps = [
            rehearsal.Step(id="1", description="a", argv=["ok"], kind="tool"),
            rehearsal.Step(id="2", description="b", argv=["bad-check"], kind="check"),
            rehearsal.Step(id="3", description="c", argv=["ok"], kind="tool"),
            rehearsal.Step(id="4", description="d", argv=["bad-tool"], kind="tool"),
            rehearsal.Step(id="5", description="e", argv=["ok"], kind="tool"),
        ]
        results = rehearsal.run_plan(steps, runner)
        self.assertEqual([r.status for r in results], ["passed", "failed", "passed", "failed", "skipped"])

    def test_stdin_and_stdout_files(self):
        seen = {}

        def fake_run(argv, **kw):
            seen.update(kw)
            if hasattr(kw["stdout"], "write"):
                kw["stdout"].write(b"payload")
            return completed(argv, 0)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.tar"
            runner = rehearsal.Runner(dry_run=False, run=fake_run, out=lambda text: None)
            runner.execute(rehearsal.Step(id="p", description="pull", argv=["ssh"], stdout_file=target))
            self.assertEqual(target.read_bytes(), b"payload")
            source = Path(tmp) / "in.tar"
            source.write_bytes(b"abc")
            runner.execute(rehearsal.Step(id="q", description="push", argv=["ssh"], stdin_file=source))
            self.assertTrue(seen["stdin"].closed)


class ExecuteFlowTests(unittest.TestCase):
    """Drive main(--execute) with every external command faked."""

    def run_main(self, tmp, fail_on=None, extra=(), libvirt=None, prepare=None, patches=()):
        calls = []
        libvirt = libvirt or FakeLibvirt()
        self.libvirt = libvirt
        src = Path(tmp) / "src.qcow2"
        dst = Path(tmp) / "dst.qcow2"
        src.write_bytes(b"s")
        dst.write_bytes(b"d")
        if prepare:
            prepare()

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[0] == "ssh-keygen":
                key = Path(argv[argv.index("-f") + 1])
                key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n")
                Path(str(key) + ".pub").write_text(PUBLIC_KEY + "\n")
            failing = bool(fail_on and fail_on(argv))
            handled = libvirt.handle(argv, install_fails=failing)
            if handled is not None:
                return handled
            if failing:
                return completed(argv, 1, "", "boom")
            return completed(argv, 0, "ok\n", "")

        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for item in (patch.object(rehearsal, "which", return_value="/usr/bin/x"),
                         patch.object(rehearsal, "kvm_present", return_value=True),
                         patch.object(subprocess, "run", side_effect=fake_run),
                         patch.object(rehearsal.time, "sleep", lambda s: None),
                         contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()),
                         *patches):
                stack.enter_context(item)
            code = rehearsal.main(["--execute", "--workdir", str(Path(tmp) / "w"), "--run-id", "abc123",
                                   "--source-image", str(src), "--dest-image", str(dst), *extra])
        return code, calls, out.getvalue()

    def virsh_names(self, calls):
        names = []
        for argv in calls:
            if argv[0] == "virsh" and ("destroy" in argv or "undefine" in argv):
                names.append((argv[argv.index("destroy") if "destroy" in argv else argv.index("undefine")],
                              argv[-1]))
        return names

    def test_cleanup_after_failure_targets_only_created_fmr_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, calls, _ = self.run_main(
                tmp, fail_on=lambda a: a[0] == "virt-install" and "fmr-dst-abc123" in a)
            self.assertEqual(code, 1)
            self.assertEqual(
                sorted(set(self.virsh_names(calls))),
                sorted({("destroy", "fmr-src-abc123"), ("destroy", "fmr-dst-abc123"),
                        ("undefine", "fmr-src-abc123"), ("undefine", "fmr-dst-abc123")}))
            report = Path(tmp) / "w" / "runs" / "abc123" / "report.json"
            self.assertTrue(report.exists())
            data = json.loads(report.read_text())
            self.assertFalse(data["passed"])
            run_dir = Path(tmp) / "w" / "runs" / "abc123"
            self.assertFalse((run_dir / "id_ed25519").exists(), "ephemeral key removed")

    def test_failure_before_any_vm_does_not_touch_libvirt_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, calls, _ = self.run_main(tmp, fail_on=lambda a: a[0] == "ssh-keygen")
            self.assertEqual(code, 1)
            self.assertEqual(self.virsh_names(calls), [])

    def test_keep_leaves_vms_and_overlays(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, calls, out = self.run_main(tmp, extra=("--keep",))
            self.assertEqual(self.virsh_names(calls), [])
            self.assertIn("fmr-src-abc123", out)
            self.assertTrue((Path(tmp) / "w" / "runs" / "abc123" / "id_ed25519").exists())

    def test_successful_run_writes_reports_and_destroys_vms(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The faked commands succeed, but expect_output markers are absent, so
            # output-checking steps fail; the flow and cleanup still complete.
            code, calls, out = self.run_main(tmp)
            run_dir = Path(tmp) / "w" / "runs" / "abc123"
            self.assertTrue((run_dir / "report.md").exists())
            self.assertTrue((run_dir / "report.json").exists())
            names = {name for _, name in self.virsh_names(calls)}
            self.assertEqual(names, {"fmr-src-abc123", "fmr-dst-abc123"})
            self.assertIn(str(run_dir / "report.md"), out)
            self.assertIn("GPG", out)  # the unverified-CHECKSUM-signature caveat is stated


    def run_dir(self, tmp):
        return Path(tmp) / "w" / "runs" / "abc123"

    def destructive(self, calls):
        return [c for c in calls if c[0] == "virsh" and c[3] in ("destroy", "undefine")]

    def test_refuses_when_domain_name_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            libvirt = FakeLibvirt({"fmr-src-abc123": "11111111-1111-1111-1111-111111111111"})
            code, calls, _ = self.run_main(tmp, libvirt=libvirt)
        self.assertEqual(code, 1)
        self.assertEqual(libvirt.installs, [])
        self.assertEqual(self.destructive(calls), [])
        self.assertEqual(libvirt.domains, {"fmr-src-abc123": "11111111-1111-1111-1111-111111111111"})

    def test_domain_that_appears_during_create_is_never_destroyed(self):
        with tempfile.TemporaryDirectory() as tmp:
            libvirt = FakeLibvirt(foreign_on_create=True)
            code, calls, _ = self.run_main(tmp, libvirt=libvirt)
            data = json.loads((self.run_dir(tmp) / "report.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(self.destructive(calls), [])
        self.assertIn("fmr-src-abc123", libvirt.domains)
        self.assertFalse(data["passed"])

    def test_uuid_passed_to_virt_install_matches_cleanup_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, calls, _ = self.run_main(tmp)
        installs = [c for c in calls if c[0] == "virt-install"]
        uuids = {c[c.index("--name") + 1]: c[c.index("--uuid") + 1] for c in installs}
        self.assertEqual(set(uuids), {"fmr-src-abc123", "fmr-dst-abc123"})
        self.assertEqual(self.libvirt.domains, {})  # both were ours and are gone

    def test_run_dir_reuse_is_refused_and_nothing_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = self.run_dir(tmp)

            def prepare():
                existing.mkdir(parents=True)
                (existing / "id_ed25519").write_text("precious")
                (existing / "report.json").write_text("old report")

            code, calls, out = self.run_main(tmp, prepare=prepare)
            self.assertEqual(code, 1)
            self.assertEqual((existing / "id_ed25519").read_text(), "precious")
            self.assertEqual((existing / "report.json").read_text(), "old report")
            self.assertFalse((existing / "report.md").exists())
            self.assertEqual(self.destructive(calls), [])
            self.assertEqual([c for c in calls if c[0] == "virt-install"], [])
        self.assertIn("--run-id", out)

    def test_failed_domain_removal_fails_report_and_keeps_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            libvirt = FakeLibvirt(destroy_error={"fmr-src-abc123"})
            code, calls, out = self.run_main(tmp, libvirt=libvirt)
            run_dir = self.run_dir(tmp)
            data = json.loads((run_dir / "report.json").read_text())
            self.assertEqual(code, 1)
            self.assertFalse(data["passed"])
            failed = {s["id"] for s in data["steps"] if s["status"] == "failed"}
            self.assertTrue(any(i.startswith("remove-domain-") for i in failed), failed)
            self.assertTrue((run_dir / "id_ed25519").exists())
        self.assertIn("kept", out)

    def test_report_written_when_run_plan_raises(self):
        def boom(steps, runner):
            runner.execute(steps[0])
            raise ValueError("kaboom")

        with tempfile.TemporaryDirectory() as tmp:
            code, calls, _ = self.run_main(tmp, patches=(patch.object(rehearsal, "run_plan", boom),))
            data = json.loads((self.run_dir(tmp) / "report.json").read_text())
        self.assertEqual(code, 1)
        self.assertFalse(data["passed"])
        self.assertIn("kaboom", json.dumps(data["steps"]))

    def test_report_records_base_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_main(tmp)
            data = json.loads((self.run_dir(tmp) / "report.json").read_text())
            self.assertEqual(data["meta"]["base_images"]["src"], str(Path(tmp) / "src.qcow2"))


class DomainOwnershipPlanTests(unittest.TestCase):
    def test_uuid_is_passed_to_virt_install(self):
        config = make_config("/tmp/x")
        for label in ("src", "dst"):
            step = next(s for s in rehearsal.build_plan(config) if s.id == f"create-vm-{label}")
            self.assertEqual(step.argv[step.argv.index("--uuid") + 1], str(config.domain_uuid(label)))
        self.assertNotEqual(config.domain_uuid("src"), config.domain_uuid("dst"))

    def test_name_precheck_precedes_creation_and_expects_absence(self):
        plan = rehearsal.build_plan(make_config("/tmp/x"))
        ids = [s.id for s in plan]
        for label in ("src", "dst"):
            check = plan[ids.index(f"check-domain-absent-{label}")]
            self.assertLess(ids.index(check.id), ids.index(f"create-vm-{label}"))
            self.assertEqual(check.expect, "nonzero")
            self.assertEqual(check.kind, "setup")
            self.assertEqual(check.argv[-2:], ["domuuid", f"fmr-{label}-abc123"])


class RunDirTests(unittest.TestCase):
    def test_prepare_workdir_creates_exclusively(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp) / "w")
            state = rehearsal.RunState()
            rehearsal.prepare_workdir(config, state)
            self.assertTrue(state.owns_run_dir)
            self.assertEqual(config.run_dir.stat().st_mode & 0o777, 0o700)
            again = rehearsal.RunState()
            with self.assertRaisesRegex(rehearsal.RehearsalError, "--run-id"):
                rehearsal.prepare_workdir(config, again)
            self.assertFalse(again.owns_run_dir)


class ImmutableBackingImageTests(unittest.TestCase):
    NAME = "Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2"

    def fake_curl(self, content):
        digest = hashlib.sha256(content).hexdigest()

        def fake(argv, **kw):
            self.assertEqual(argv[0], "curl")
            if "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_bytes(content)
                return completed(argv, 0)
            if argv[-1].endswith("/images/"):
                return completed(argv, 0, f'<a href="{self.NAME}">x</a>')
            return completed(argv, 0, f"SHA256 ({self.NAME}) = {digest}\n")
        return fake

    def test_fetch_records_build_specific_file_and_creates_no_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp) / "w")
            state = rehearsal.RunState()
            with patch.object(subprocess, "run", side_effect=self.fake_curl(b"qcow")):
                rehearsal.fetch_image(config, "src", state)
            expected = config.images_dir / self.NAME
            self.assertEqual(state.base_images["src"], expected)
            self.assertTrue(expected.is_file())
            self.assertEqual([p.name for p in config.images_dir.iterdir() if p.is_symlink()], [])
            self.assertFalse((config.images_dir / "fedora-cloud-base-43.qcow2").exists())
            overlay = next(s for s in rehearsal.build_plan(config, state) if s.id == "create-overlay-src")
            argv = rehearsal.resolve_argv(overlay)
            self.assertEqual(argv[argv.index("-b") + 1], str(expected))

    def test_dry_run_overlay_never_names_a_mutable_alias(self):
        config = make_config("/tmp/fmr-x")
        overlay = next(s for s in rehearsal.build_plan(config) if s.id == "create-overlay-src")
        text = rehearsal.display_command(overlay)
        self.assertNotIn("fedora-cloud-base-43", text)
        self.assertIn("Fedora-Cloud-Base-Generic-43-", text)

    def test_known_build_gives_static_absolute_path(self):
        config = make_config("/tmp/fmr-x", "--source-build", "1.6")
        overlay = next(s for s in rehearsal.build_plan(config) if s.id == "create-overlay-src")
        self.assertIn(str(config.images_dir / self.NAME), overlay.argv)


class RestoredFilesScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.files = ["a.txt", "b.txt"]
        for name in self.files:
            path = self.home / name
            path.write_text("content " + name)
            path.chmod(0o640)
        self.expected = Path(self.tmp.name) / "expected.txt"

    def entries(self, names):
        lines = []
        for name in names:
            path = self.home / name
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{sha} 640 {int(path.stat().st_mtime)} {name}\n")
        return "".join(lines)

    def run_script(self, count=2):
        script = rehearsal.restored_files_script(str(self.expected), count)
        return subprocess.run(["bash", "-c", script], env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
                              capture_output=True, text=True)

    def test_correct_expectations_pass(self):
        self.expected.write_text(self.entries(self.files))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all 2 sampled files match", result.stdout)

    def test_missing_expectation_file_fails(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("match", result.stdout)

    def test_empty_expectation_file_fails(self):
        self.expected.write_text("")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("match", result.stdout)

    def test_partial_expectation_file_fails(self):
        self.expected.write_text(self.entries(self.files[:1]))
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected 2", result.stdout)

    def test_content_difference_fails(self):
        self.expected.write_text(self.entries(self.files))
        (self.home / "a.txt").write_text("tampered")
        self.assertNotEqual(self.run_script().returncode, 0)

    def test_plan_uses_sample_size(self):
        self.assertIn(f"want={len(rehearsal.SAMPLE_FILES)}", rehearsal.RESTORED_FILES_SCRIPT)


class ReportTests(unittest.TestCase):
    def results(self):
        return [
            rehearsal.StepResult(id="backup", description="Back up on source", kind="tool", vm="src",
                                 command="ssh x -- backup", status="passed", returncode=0,
                                 detail="", output_tail="done", duration=1.5),
            rehearsal.StepResult(id="check-symlinks", description="Symlinks preserved", kind="check", vm="dst",
                                 command="ssh y -- check", status="failed", returncode=1,
                                 detail="expected exit 0, got 1", output_tail="link missing", duration=0.2),
            rehearsal.StepResult(id="rollback", description="Roll back", kind="tool", vm="dst",
                                 command="ssh y -- rollback", status="skipped", returncode=None,
                                 detail="earlier step failed", output_tail="", duration=0.0),
        ]

    def meta(self):
        return {"run_id": "abc123", "tool_commit": "deadbeef", "source_release": 43, "dest_release": 44,
                "dry_run": False, "os_release": {"src": "Fedora 43", "dst": "Fedora 44"}}

    def test_json_report(self):
        data = json.loads(rehearsal.render_report_json(self.meta(), self.results()))
        self.assertFalse(data["passed"])
        self.assertEqual(data["summary"], {"passed": 1, "failed": 1, "skipped": 1})
        self.assertEqual(data["meta"]["tool_commit"], "deadbeef")
        self.assertEqual([s["id"] for s in data["steps"]], ["backup", "check-symlinks", "rollback"])

    def test_markdown_report(self):
        text = rehearsal.render_report_markdown(self.meta(), self.results())
        self.assertIn("# Fedora migration rehearsal report", text)
        self.assertIn("deadbeef", text)
        self.assertIn("| check-symlinks |", text)
        self.assertIn("FAILED", text)
        self.assertIn("1 passed, 1 failed, 1 skipped", text)
        self.assertIn("link missing", text)

    def test_all_passed_is_passed(self):
        results = [r for r in self.results() if r.status == "passed"]
        self.assertTrue(json.loads(rehearsal.render_report_json(self.meta(), results))["passed"])

    def test_markdown_escapes_pipes_in_cells(self):
        result = self.results()[0]
        result.description = "a | b"
        self.assertIn("a \\| b", rehearsal.render_report_markdown(self.meta(), [result]))


class RepoTarballTests(unittest.TestCase):
    def test_tarball_excludes_git_and_caches(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / ".git" / "config").write_text("x")
            (repo / "pkg" / "__pycache__").mkdir(parents=True)
            (repo / "pkg" / "__pycache__" / "m.pyc").write_bytes(b"x")
            (repo / "pkg" / "m.py").write_text("print(1)")
            (repo / "fedora-migration.py").write_text("")
            out = Path(tmp) / "repo.tar"
            rehearsal.make_repo_tarball(repo, out)
            with tarfile.open(out) as archive:
                names = archive.getnames()
        self.assertIn("pkg/m.py", names)
        self.assertIn("fedora-migration.py", names)
        self.assertFalse([n for n in names if n.startswith(".git") or "__pycache__" in n])


if __name__ == "__main__":
    unittest.main()
