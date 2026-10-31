"""CLI policy tests; external system changes are mocked, file fixtures are real."""

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def cli_module():
    return importlib.import_module("fedora_migration.cli")


def package_fixture():
    return {
        "rpm": {"status": "captured", "requests": [], "inventory": []},
        "flatpak": {"status": "not-installed", "apps": [], "remotes": []},
        "warnings": [],
    }


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "source-home"
        self.home.mkdir()
        self.info = {
            "id": "fedora", "version_id": "43", "pretty_name": "Fedora Linux 43",
            "home": str(self.home), "uid": 1000, "username": "alice",
            "architecture": "x86_64",
        }
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            os.environ.pop(variable, None)
        # Running-application checks inspect the real host; tests opt in.
        guard = patch.object(cli_module(), "assert_applications_closed")
        self.applications_guard = guard.start()
        self.addCleanup(guard.stop)

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_module().main(args)
        return code, out.getvalue(), err.getvalue()

    def backup_fixture(self):
        from fedora_migration.bundle import create_bundle
        config = self.home / ".config/xfce4"
        config.mkdir(parents=True, exist_ok=True)
        (config / "settings.xml").write_text("<channel name='xfce4-panel'/>\n")
        bundle = self.root / "backup"
        create_bundle(self.home, bundle, self.info, package_fixture(), settings_only=True)
        return bundle

    def test_help_works_without_fedora_or_optional_tools(self):
        result = subprocess.run([sys.executable, str(ROOT / "fedora-migration.py"), "--help"],
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("backup", result.stdout)
        self.assertIn("restore", result.stdout)

    def test_verify_does_not_inspect_platform_or_run_package_commands(self):
        cli = cli_module()
        bundle = self.backup_fixture()
        with patch.object(cli, "platform_info", side_effect=AssertionError("not required")), \
             patch.object(cli.packages, "install_packages", side_effect=AssertionError("must not install")):
            code, out, err = self.invoke(["verify", "--bundle", str(bundle)])
        self.assertEqual(code, 0, err)
        self.assertIn("verified", out.lower())

    def test_backup_rejects_root_before_collecting_or_writing(self):
        cli = cli_module()
        info = dict(self.info, uid=0)
        dest = self.root / "backup"
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli.packages, "collect_packages") as collect:
            code, _, err = self.invoke(["backup", "--output", str(dest)])
        self.assertEqual(code, 1)
        self.assertIn("normal desktop user", err)
        collect.assert_not_called()
        self.assertFalse(dest.exists())

    def test_backup_rejects_wrong_source_release(self):
        cli = cli_module()
        with patch.object(cli, "platform_info", return_value=dict(self.info, version_id="42")):
            code, _, err = self.invoke(["backup", "--output", str(self.root / "backup")])
        self.assertEqual(code, 1)
        self.assertIn("Fedora 43", err)

    def test_active_xfce_blocks_backup_before_writing(self):
        cli = cli_module()
        with patch.object(cli, "platform_info", return_value=self.info), \
             patch.object(cli, "assert_xfce_stopped", side_effect=cli.MigrationError("Log out of XFCE")):
            code, _, err = self.invoke(["backup", "--output", str(self.root / "backup")])
        self.assertEqual(code, 1)
        self.assertIn("Log out", err)
        self.assertFalse((self.root / "backup").exists())

    def test_backup_creates_verifiable_bundle_with_include(self):
        cli = cli_module()
        extra = self.home / "work notes"
        extra.mkdir()
        (extra / "keep.txt").write_text("migration evidence")
        dest = self.root / "backup"
        with patch.object(cli, "platform_info", return_value=self.info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli.packages, "collect_packages", return_value=package_fixture()):
            code, _, err = self.invoke(["backup", "--output", str(dest), "--include", "work notes"])
        self.assertEqual(code, 0, err)
        self.assertEqual((dest / "payload/work notes/keep.txt").read_text(), "migration evidence")
        code, _, err = self.invoke(["verify", "--bundle", str(dest)])
        self.assertEqual(code, 0, err)

    def test_backup_passes_excludes_and_checks_applications_with_secret_scope(self):
        cli = cli_module()
        dest = self.root / "backup"
        displays = self.home / ".config/xfce4/xfconf/xfce-perchannel-xml/displays.xml"
        displays.parent.mkdir(parents=True)
        displays.write_text("<monitor/>")
        (self.home / "Projects/build").mkdir(parents=True)
        (self.home / "Projects/build/out.o").write_text("object")
        (self.home / "Projects/main.c").write_text("code")
        with patch.object(cli, "platform_info", return_value=self.info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli.packages, "collect_packages", return_value=package_fixture()):
            code, out, err = self.invoke([
                "backup", "--output", str(dest), "--settings-only", "--include", "Projects",
                "--exclude", "Projects/build", "--include", ".ssh/config",
            ])
        self.assertEqual(code, 0, err)
        self.assertFalse((dest / "payload/Projects/build").exists())
        self.assertTrue((dest / "payload/Projects/main.c").exists())
        self.assertFalse((dest / "payload/.config/xfce4/xfconf/xfce-perchannel-xml/displays.xml").exists())
        self.assertIn("Excluded 1 path matching 'Projects/build'", out)
        self.applications_guard.assert_called_once_with(secrets=True)

    def test_backup_can_keep_default_excluded_paths(self):
        cli = cli_module()
        dest = self.root / "backup"
        displays = self.home / ".config/xfce4/xfconf/xfce-perchannel-xml/displays.xml"
        displays.parent.mkdir(parents=True)
        displays.write_text("<monitor/>")
        with patch.object(cli, "platform_info", return_value=self.info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli.packages, "collect_packages", return_value=package_fixture()):
            code, _, err = self.invoke(["backup", "--output", str(dest), "--settings-only",
                                        "--no-default-excludes"])
        self.assertEqual(code, 0, err)
        self.assertTrue((dest / "payload/.config/xfce4/xfconf/xfce-perchannel-xml/displays.xml").exists())
        self.applications_guard.assert_called_once_with(secrets=False)

    def test_running_applications_block_backup_before_writing(self):
        cli = cli_module()
        self.applications_guard.side_effect = cli.MigrationError("Close firefox")
        dest = self.root / "backup"
        with patch.object(cli, "platform_info", return_value=self.info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli.packages, "collect_packages") as collect:
            code, _, err = self.invoke(["backup", "--output", str(dest)])
        self.assertEqual(code, 1)
        self.assertIn("Close firefox", err)
        collect.assert_not_called()
        self.assertFalse(dest.exists())

    def test_restore_apply_checks_applications_and_secret_agents_from_bundle(self):
        cli = cli_module()
        from fedora_migration.bundle import create_bundle
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh/config").write_text("Host example\n")
        bundle = self.root / "secret-backup"
        create_bundle(self.home, bundle, self.info, package_fixture(),
                      settings_only=True, include_secrets=True)
        target = self.root / "target-home"
        target.mkdir()
        info = dict(self.info, version_id="44", home=str(target))
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli, "check_selinux_tools", return_value=False):
            code, _, err = self.invoke(["restore", "--bundle", str(bundle)])
            self.assertEqual(code, 0, err)
            self.applications_guard.assert_not_called()
            code, _, err = self.invoke(["restore", "--bundle", str(bundle), "--apply"])
        self.assertEqual(code, 0, err)
        self.assertTrue(self.applications_guard.call_args_list)
        for call in self.applications_guard.call_args_list:
            self.assertEqual(call.kwargs, {"secrets": True})

    def test_restore_is_preview_by_default_even_with_install_packages(self):
        cli = cli_module()
        bundle = self.backup_fixture()
        target = self.root / "target-home"
        target.mkdir()
        info = dict(self.info, version_id="44", home=str(target))
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli, "assert_xfce_stopped", side_effect=AssertionError("preview may run in XFCE")), \
             patch.object(cli.packages, "install_packages") as install, \
             patch.object(cli, "relabel_paths") as relabel:
            code, out, err = self.invoke(["restore", "--bundle", str(bundle), "--install-packages"])
        self.assertEqual(code, 0, err)
        self.assertIn("preview", out.lower())
        expected_size = (bundle / "payload/.config/xfce4/settings.xml").stat().st_size
        self.assertIn(f"File content: {expected_size:,} bytes", out)
        self.assertEqual(list(target.iterdir()), [])
        install.assert_not_called()
        relabel.assert_not_called()

    def test_explicit_apply_restores_without_installing_packages_by_default(self):
        cli = cli_module()
        bundle = self.backup_fixture()
        target = self.root / "target-home"
        target.mkdir()
        info = dict(self.info, version_id="44", home=str(target))
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli, "check_selinux_tools", return_value=False), \
             patch.object(cli.packages, "install_packages") as install:
            code, out, err = self.invoke(["restore", "--bundle", str(bundle), "--apply"])
        self.assertEqual(code, 0, err)
        self.assertTrue((target / ".config/xfce4/settings.xml").is_file())
        self.assertIn("restored", out.lower())
        install.assert_not_called()

    def test_restore_verifies_once_for_preview_and_again_before_mutation(self):
        cli = cli_module()
        bundle = self.backup_fixture()
        target = self.root / "target-home"
        target.mkdir()
        info = dict(self.info, version_id="44", home=str(target))
        events = []
        verify = cli.bundle.verify_bundle

        def counted_verify(path):
            events.append("verify")
            return verify(path)

        cases = (
            (["restore", "--bundle", str(bundle)], ["verify"]),
            # Session and application guards run before package installation and
            # again after the final verification, immediately before writing.
            (["restore", "--bundle", str(bundle), "--apply", "--install-packages"],
             ["verify", "xfce", "apps", "install", "verify", "xfce", "apps"]),
        )
        self.applications_guard.side_effect = lambda **kwargs: events.append("apps")
        for argv, expected in cases:
            with self.subTest(argv=argv):
                events.clear()
                with patch.object(cli, "platform_info", return_value=info), \
                     patch.object(cli, "assert_xfce_stopped",
                                  side_effect=lambda: events.append("xfce")), \
                     patch.object(cli, "check_selinux_tools", return_value=False), \
                     patch.object(cli.packages, "install_packages",
                                  side_effect=lambda *a, **k: events.append("install")), \
                     patch.object(cli.bundle, "verify_bundle", side_effect=counted_verify):
                    code, out, err = self.invoke(argv)
                self.assertEqual(code, 0, err)
                # Preview previously verified the payload twice and apply three
                # times; the final check still precedes every file mutation.
                self.assertEqual(events, expected)

    def test_package_failure_does_not_start_file_restore(self):
        cli = cli_module()
        bundle = self.backup_fixture()
        target = self.root / "target-home"
        target.mkdir()
        info = dict(self.info, version_id="44", home=str(target))
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli, "check_selinux_tools", return_value=False), \
             patch.object(cli.packages, "install_packages", side_effect=RuntimeError("DNF transaction failed")):
            code, _, err = self.invoke(["restore", "--bundle", str(bundle), "--apply", "--install-packages"])
        self.assertEqual(code, 1)
        self.assertIn("DNF transaction failed", err)
        self.assertEqual(list(target.iterdir()), [])

    def test_architecture_mismatch_rejects_package_restore(self):
        cli = cli_module()
        bundle = self.backup_fixture()
        info = dict(self.info, version_id="44", architecture="aarch64")
        with patch.object(cli, "platform_info", return_value=info):
            code, _, err = self.invoke(["restore", "--bundle", str(bundle), "--install-packages"])
        self.assertEqual(code, 1)
        self.assertIn("architecture", err.lower())

    def test_corrupt_bundle_rejected_before_any_package_change(self):
        cli = cli_module()
        bundle = self.backup_fixture()
        (bundle / "payload/.config/xfce4/settings.xml").write_text("corrupt")
        info = dict(self.info, version_id="44")
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli.packages, "install_packages") as install:
            code, _, _ = self.invoke(["restore", "--bundle", str(bundle), "--apply", "--install-packages"])
        self.assertEqual(code, 1)
        install.assert_not_called()

    def test_invalid_package_manifest_rejected_during_verify(self):
        bundle = self.backup_fixture()
        path = bundle / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["packages"]["rpm"]["requests"] = ["--allowerasing"]
        path.write_text(json.dumps(manifest))
        code, _, _ = self.invoke(["verify", "--bundle", str(bundle)])
        self.assertEqual(code, 1)

    def test_os_release_is_parsed_as_data(self):
        cli = cli_module()
        release = self.root / "os-release"
        sentinel = self.root / "should-not-exist"
        release.write_text(f'ID=fedora\nVERSION_ID="43"\nPRETTY_NAME="$(touch {sentinel})"\n')
        parsed = cli.read_os_release(release)
        self.assertEqual(parsed["VERSION_ID"], "43")
        self.assertIn("$(touch", parsed["PRETTY_NAME"])
        self.assertFalse(sentinel.exists())

    def test_interrupted_restore_reports_recovery_path_and_status(self):
        cli = cli_module()
        bundle = self.backup_fixture()
        target = self.root / "target-home"
        config = target / ".config/xfce4"
        config.mkdir(parents=True)
        (config / "settings.xml").write_text("original")
        info = dict(self.info, version_id="44", home=str(target))
        rollback = self.root / "rollback"
        original_replace = os.replace

        def interrupt(src, dest):
            if Path(dest) == config / "settings.xml":
                raise KeyboardInterrupt()
            return original_replace(src, dest)

        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli, "check_selinux_tools", return_value=False), \
             patch("fedora_migration.bundle.os.replace", side_effect=interrupt):
            code, out, err = self.invoke(["restore", "--bundle", str(bundle), "--apply",
                                          "--rollback-dir", str(rollback)])
        self.assertEqual(code, 130)
        self.assertIn(str(rollback), out + err)
        self.assertEqual((rollback / "previous/.config/xfce4/settings.xml").read_text(), "original")
        self.assertEqual(json.loads((rollback / "journal.json").read_text())["status"], "interrupted")


class ApplicationGuardTests(unittest.TestCase):
    def run_guard(self, *, pgrep, flatpak=None, flatpak_installed=True, secrets=False):
        cli = cli_module()
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "pgrep":
                return subprocess.CompletedProcess(argv, *pgrep)
            return subprocess.CompletedProcess(argv, *flatpak)

        def fake_which(name):
            return None if name == "flatpak" and not flatpak_installed else f"/usr/bin/{name}"

        with patch.object(cli.subprocess, "run", side_effect=fake_run), \
             patch.object(cli.shutil, "which", side_effect=fake_which):
            try:
                cli.assert_applications_closed(secrets=secrets)
            except cli.MigrationError as error:
                return calls, str(error)
        return calls, None

    def test_nothing_running_passes(self):
        calls, error = self.run_guard(pgrep=(1, "", ""), flatpak=(0, "", ""))
        self.assertIsNone(error)
        self.assertEqual(calls[0][:4], ["pgrep", "-u", str(os.getuid()), "-x"])
        self.assertNotIn("gpg-agent", calls[0][-1])
        self.assertEqual(calls[1], ["flatpak", "ps", "--columns=application"])

    def test_running_browser_or_mail_blocks(self):
        _, error = self.run_guard(pgrep=(0, "1234 firefox\n2345 thunderbird\n", ""),
                                  flatpak=(0, "", ""))
        self.assertIn("firefox", error)
        self.assertIn("thunderbird", error)

    def test_running_flatpak_application_blocks(self):
        _, error = self.run_guard(pgrep=(1, "", ""),
                                  flatpak=(0, "org.mozilla.firefox\norg.mozilla.firefox\n", ""))
        self.assertIn("org.mozilla.firefox", error)
        self.assertEqual(error.count("org.mozilla.firefox"), 1)

    def test_secret_agents_checked_only_with_secrets(self):
        calls, error = self.run_guard(pgrep=(0, "77 gpg-agent\n", ""), flatpak=(0, "", ""),
                                      secrets=True)
        self.assertIn("gpg-agent", calls[0][-1])
        self.assertIn("gnome-keyring-d", calls[0][-1])
        self.assertIn("gpg-agent", error)
        self.assertIn("gpgconf --kill all", error)

    def test_missing_flatpak_is_skipped(self):
        calls, error = self.run_guard(pgrep=(1, "", ""), flatpak_installed=False)
        self.assertIsNone(error)
        self.assertEqual(len(calls), 1)

    def test_check_failures_fail_closed(self):
        _, error = self.run_guard(pgrep=(2, "", "bad pattern"), flatpak=(0, "", ""))
        self.assertIn("Could not check running applications", error)
        _, error = self.run_guard(pgrep=(1, "", ""), flatpak=(1, "", "no session bus"))
        self.assertIn("Could not check running Flatpak applications", error)


if __name__ == "__main__":
    unittest.main()
