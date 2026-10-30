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

    def test_cleanup_targets_only_created_domains(self):
        config = make_config("/tmp/x")
        steps = rehearsal.cleanup_steps(config, ["fmr-src-abc123"])
        argvs = all_argvs(steps)
        self.assertTrue(argvs)
        for argv in argvs:
            self.assertEqual(argv[:3], ["virsh", "-c", "qemu:///session"])
            names = [item for item in argv if item.startswith("fmr-")]
            self.assertEqual(names, ["fmr-src-abc123"])
        flat = " ".join(" ".join(a) for a in argvs)
        self.assertIn("destroy", flat)
        self.assertIn("undefine", flat)
        self.assertNotIn("fmr-dst-abc123", flat)
        self.assertNotIn("--all", flat)

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

    def test_created_domain_recorded_even_when_step_fails(self):
        runner = rehearsal.Runner(dry_run=False, run=lambda argv, **kw: completed(argv, 1),
                                  out=lambda text: None)
        step = rehearsal.Step(id="v", description="d", argv=["virt-install"], kind="setup",
                              creates_domain="fmr-src-abc123")
        runner.execute(step)
        self.assertEqual(runner.created_domains, ["fmr-src-abc123"])

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

    def run_main(self, tmp, fail_on=None, extra=()):
        calls = []
        src = Path(tmp) / "src.qcow2"
        dst = Path(tmp) / "dst.qcow2"
        src.write_bytes(b"s")
        dst.write_bytes(b"d")

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[0] == "ssh-keygen":
                key = Path(argv[argv.index("-f") + 1])
                key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n")
                Path(str(key) + ".pub").write_text(PUBLIC_KEY + "\n")
            if fail_on and fail_on(argv):
                return completed(argv, 1, "", "boom")
            return completed(argv, 0, "ok\n", "")

        out = io.StringIO()
        with patch.object(rehearsal, "which", return_value="/usr/bin/x"), \
                patch.object(rehearsal, "kvm_present", return_value=True), \
                patch.object(subprocess, "run", side_effect=fake_run), \
                patch.object(rehearsal.time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
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
