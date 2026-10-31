"""CLI policy tests; external system changes are mocked, file fixtures are real."""

import contextlib
import errno
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
        "rpm": {"status": "captured", "requests": [], "inventory": [],
                "repos": [], "groups": [], "environments": []},
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

    def references_fixture(self):
        """A bundle whose bookmarks name the source home, plus a destination home."""
        from fedora_migration.bundle import create_bundle
        gtk = self.home / ".config/gtk-3.0"
        gtk.mkdir(parents=True)
        (gtk / "bookmarks").write_text(f"file://{self.home}/Documents Docs\n"
                                       f"file://{self.home}/Music Music\n")
        (self.home / ".bashrc").write_text(f"export P={self.home}/bin\n")
        bundle = self.root / "refs-backup"
        create_bundle(self.home, bundle, self.info, package_fixture(), settings_only=True)
        target = self.root / "target-home"
        target.mkdir()
        return bundle, target, dict(self.info, version_id="44", home=str(target))

    def restore(self, info, *argv):
        cli = cli_module()
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli, "check_selinux_tools", return_value=False):
            return self.invoke(["restore", *argv])

    def test_preview_prints_destination_checks_with_counts_and_references(self):
        bundle, target, info = self.references_fixture()
        (target / ".bashrc").write_text("old")
        code, out, err = self.restore(info, "--bundle", str(bundle))
        self.assertEqual(code, 0, err)
        self.assertIn("Destination checks", out)
        self.assertIn("Will replace 1 existing files or symlinks (originals are kept in "
                      "the rollback directory) and create 1 new ones.", out)
        self.assertIn(f"{self.home}", out)
        self.assertIn(".config/gtk-3.0/bookmarks: 2", out)
        self.assertNotIn(".bashrc: ", out)
        self.assertIn("--rewrite-home", out)
        self.assertEqual((target / ".bashrc").read_text(), "old")
        self.assertEqual(sorted(p.name for p in target.iterdir()), [".bashrc"])

    def test_apply_also_prints_the_destination_checks_before_installing(self):
        cli = cli_module()
        bundle, target, info = self.references_fixture()
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli, "check_selinux_tools", return_value=False), \
             patch.object(cli.packages, "install_packages"):
            code, out, err = self.invoke(["restore", "--bundle", str(bundle), "--apply",
                                          "--install-packages", "--skip-rpm", "--skip-flatpak"])
        self.assertEqual(code, 0, err)
        self.assertIn("Destination checks", out)
        self.assertIn("create 2 new ones", out)
        self.assertLess(out.index("Destination checks"),
                        out.index("Installing requested packages"))
        self.assertIn(f"file://{self.home}/Documents", (target / ".config/gtk-3.0/bookmarks").read_text())

    def test_rewrite_home_without_apply_writes_nothing(self):
        bundle, target, info = self.references_fixture()
        code, out, err = self.restore(info, "--bundle", str(bundle), "--rewrite-home")
        self.assertEqual(code, 0, err)
        self.assertEqual(list(target.iterdir()), [])
        self.assertIn("would be rewritten", out.lower())
        self.assertIn("preview only", out.lower())

    def test_rewrite_home_with_apply_rewrites_only_eligible_files(self):
        bundle, target, info = self.references_fixture()
        code, out, err = self.restore(info, "--bundle", str(bundle), "--apply",
                                      "--rewrite-home")
        self.assertEqual(code, 0, err)
        self.assertEqual((target / ".config/gtk-3.0/bookmarks").read_text(),
                         f"file://{target}/Documents Docs\nfile://{target}/Music Music\n")
        self.assertEqual((target / ".bashrc").read_text(), f"export P={self.home}/bin\n")
        self.assertIn("Rewrote the old home path in 1 file", out)

    def test_unsafe_rewrite_home_refuses_before_package_installation_or_restore(self):
        cli = cli_module()
        bundle, target, info = self.references_fixture()
        unsafe_target = self.root / "target&home"
        target.rename(unsafe_target)
        info["home"] = str(unsafe_target)
        installed = self.root / "packages-were-installed"

        def install(*_args, **_kwargs):
            installed.write_text("package side effect")

        with patch.object(cli.packages, "install_packages", side_effect=install):
            code, _, err = self.restore(info, "--bundle", str(bundle), "--apply",
                                        "--install-packages", "--rewrite-home")
        self.assertEqual(code, 1)
        self.assertIn("rewrite-home", err)
        self.assertIn("ASCII", err)
        self.assertFalse(installed.exists())
        self.assertEqual(list(unsafe_target.iterdir()), [])

    def test_preview_rejects_unsafe_rewrite_home_without_writes(self):
        bundle, target, info = self.references_fixture()
        unsafe_target = self.root / "target home"
        target.rename(unsafe_target)
        info["home"] = str(unsafe_target)
        code, _, err = self.restore(info, "--bundle", str(bundle), "--rewrite-home")
        self.assertEqual(code, 1)
        self.assertIn("rewrite-home", err)
        self.assertEqual(list(unsafe_target.iterdir()), [])

    def test_old_source_home_note_is_not_duplicated(self):
        cli = cli_module()
        bundle = self.backup_fixture()
        target = self.root / "target-home"
        target.mkdir()
        info = dict(self.info, version_id="44", home=str(target))
        with patch.object(cli, "platform_info", return_value=info):
            code, out, err = self.invoke(["restore", "--bundle", str(bundle)])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Note: source home was", out)

    def test_same_home_has_no_reference_report(self):
        bundle = self.backup_fixture()
        info = dict(self.info, version_id="44")
        code, out, err = self.restore(info, "--bundle", str(bundle), "--rewrite-home")
        self.assertEqual(code, 0, err)
        self.assertIn("Destination checks", out)
        self.assertNotIn("would be rewritten", out.lower())

    def test_destination_checks_report_missing_plugins_and_autostart(self):
        from fedora_migration import report
        from fedora_migration.bundle import create_bundle
        panel = self.home / ".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"
        panel.parent.mkdir(parents=True)
        panel.write_text('<channel><property name="plugins" type="empty">'
                         '<property name="plugin-1" type="string" value="weather"/>'
                         '</property></channel>')
        autostart = self.home / ".config/autostart"
        autostart.mkdir(parents=True)
        (autostart / "x.desktop").write_text("[Desktop Entry]\nExec=no-such-program-xyz\n")
        bundle = self.root / "plugins-backup"
        create_bundle(self.home, bundle, self.info, package_fixture(), settings_only=True)
        target = self.root / "target-home"
        target.mkdir()
        empty = self.root / "no-plugins"
        empty.mkdir()
        with patch.object(report, "PANEL_PLUGIN_DIRS", (str(empty),)):
            code, out, err = self.restore(dict(self.info, version_id="44", home=str(target)),
                                          "--bundle", str(bundle))
        self.assertEqual(code, 0, err)
        self.assertIn("weather", out)
        self.assertIn(".config/autostart/x.desktop: no-such-program-xyz", out)
        self.assertEqual(list(target.iterdir()), [])

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

    # --- rollback ---

    def rollback_fixture(self):
        """A target home with one replaced and one new restored file, plus its rollback dir."""
        from fedora_migration.bundle import restore_bundle
        (self.home / ".config/xfce4").mkdir(parents=True)
        (self.home / ".config/xfce4/extra.xml").write_text("<extra/>\n")
        bundle = self.backup_fixture()
        target = self.root / "target-home"
        config = target / ".config/xfce4"
        config.mkdir(parents=True)
        (config / "settings.xml").write_text("original")
        rollback = self.root / "rollback"
        restore_bundle(bundle, target, dry_run=False, backup_dir=rollback)
        info = dict(self.info, version_id="44", home=str(target))
        return bundle, target, rollback, info

    def rollback(self, info, *argv, selinux=False):
        cli = cli_module()
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli, "check_selinux_tools", return_value=selinux):
            return self.invoke(["rollback", *argv])

    def test_help_lists_rollback(self):
        result = subprocess.run([sys.executable, str(ROOT / "fedora-migration.py"), "--help"],
                                text=True, capture_output=True, check=False)
        self.assertIn("rollback", result.stdout)

    def test_rollback_is_preview_by_default_and_writes_nothing(self):
        cli = cli_module()
        bundle, target, rollback, info = self.rollback_fixture()
        before = sorted(str(p) for p in self.root.rglob("*"))
        with patch.object(cli, "assert_xfce_stopped",
                          side_effect=AssertionError("preview may run in XFCE")), \
             patch.object(cli, "relabel_paths") as relabel:
            code, out, err = self.rollback(info, "--rollback-dir", str(rollback))
        self.assertEqual(code, 0, err)
        self.assertIn("preview", out.lower())
        self.assertIn("PUT_BACK: 1", out)
        self.assertIn("REMOVE: 1", out)
        self.assertIn("settings.xml", out)
        self.assertIn("--apply", out)
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)
        self.applications_guard.assert_not_called()
        relabel.assert_not_called()

    def test_rollback_apply_puts_back_and_reports_undone_dir(self):
        bundle, target, rollback, info = self.rollback_fixture()
        with patch.object(cli_module(), "assert_xfce_stopped"):
            code, out, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual((target / ".config/xfce4/settings.xml").read_text(), "original")
        self.assertFalse((target / ".config/xfce4/extra.xml").exists())
        undone = sorted(rollback.glob("undone-*"))
        self.assertEqual(len(undone), 1)
        self.assertIn(str(undone[0]), out)
        self.assertEqual(json.loads((rollback / "rollback-journal.json").read_text())["status"],
                         "complete")

    def test_rollback_warns_when_older_journal_cannot_restore_directory_metadata(self):
        bundle, target, rollback, info = self.rollback_fixture()
        journal = json.loads((rollback / "journal.json").read_text())
        journal.pop("original_directories", None)
        (rollback / "journal.json").write_text(json.dumps(journal))
        code, out, err = self.rollback(info, "--rollback-dir", str(rollback))
        self.assertEqual(code, 0, err)
        self.assertIn("directory metadata", out)
        self.assertIn("cannot be restored", out)

    def test_cross_device_rollback_reports_retained_original_locations(self):
        bundle, target, rollback, info = self.rollback_fixture()
        real = os.rename

        def cross_device(source, destination, **kwargs):
            if kwargs.get("src_dir_fd") is not None and kwargs.get("dst_dir_fd") is None:
                raise OSError(errno.EXDEV, "cross-device link")
            return real(source, destination, **kwargs)

        with patch.object(cli_module(), "assert_xfce_stopped"), \
             patch("fedora_migration.rollback.os.rename", side_effect=cross_device):
            code, out, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply")
        self.assertEqual(code, 0, err)
        records = json.loads((rollback / "rollback-journal.json").read_text())["retained_quarantines"]
        self.assertTrue(records)
        for item in records:
            self.assertIn(str(target / item["quarantine"]), out)
        self.assertIn("all writers are stopped", out)

    def test_rollback_apply_runs_guards_before_the_first_write(self):
        cli = cli_module()
        bundle, target, rollback, info = self.rollback_fixture()
        with patch.object(cli, "assert_xfce_stopped",
                          side_effect=cli.MigrationError("Log out of XFCE")):
            code, _, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("Log out", err)
        self.assertEqual((target / ".config/xfce4/settings.xml").read_text(),
                         (bundle / "payload/.config/xfce4/settings.xml").read_text())
        self.assertFalse((rollback / "rollback-journal.json").exists())
        self.assertEqual(list(rollback.glob("undone-*")), [])

    def test_rollback_apply_rechecks_guards_immediately_before_writing(self):
        cli = cli_module()
        bundle, target, rollback, info = self.rollback_fixture()
        calls = []

        def record():
            calls.append((rollback / "rollback-journal.json").exists())

        with patch.object(cli, "assert_xfce_stopped", side_effect=record):
            code, _, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply")
        self.assertEqual(code, 0, err)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[-1], False)  # the last check precedes the first write

    def test_rollback_apply_blocked_by_running_applications(self):
        cli = cli_module()
        bundle, target, rollback, info = self.rollback_fixture()
        self.applications_guard.side_effect = cli.MigrationError("Close firefox")
        with patch.object(cli, "assert_xfce_stopped"):
            code, _, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("Close firefox", err)
        self.assertFalse((rollback / "rollback-journal.json").exists())

    def test_rollback_apply_checks_secret_agents_when_journal_touches_secret_trees(self):
        from fedora_migration.bundle import create_bundle, restore_bundle
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh/config").write_text("Host example\n")
        bundle = self.root / "secret-backup"
        create_bundle(self.home, bundle, self.info, package_fixture(),
                      settings_only=True, include_secrets=True)
        target = self.root / "target-home"
        target.mkdir()
        rollback = self.root / "rollback"
        restore_bundle(bundle, target, dry_run=False, backup_dir=rollback)
        info = dict(self.info, version_id="44", home=str(target))
        with patch.object(cli_module(), "assert_xfce_stopped"):
            code, _, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.applications_guard.call_args_list)
        for call in self.applications_guard.call_args_list:
            self.assertEqual(call.kwargs, {"secrets": True})

    def test_rollback_relabels_put_back_paths_when_selinux_is_enabled(self):
        cli = cli_module()
        bundle, target, rollback, info = self.rollback_fixture()
        with patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli, "relabel_paths") as relabel:
            code, _, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply",
                                         selinux=True)
        self.assertEqual(code, 0, err)
        relabel.assert_called_once()
        home, paths = relabel.call_args.args
        self.assertEqual(Path(home), target)
        self.assertEqual(list(paths), [".config/xfce4/settings.xml"])

    def test_rollback_does_not_relabel_without_selinux(self):
        cli = cli_module()
        bundle, target, rollback, info = self.rollback_fixture()
        with patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli, "relabel_paths") as relabel:
            code, _, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply")
        self.assertEqual(code, 0, err)
        relabel.assert_not_called()

    def test_rollback_conflicts_are_listed_with_hint_and_include_changed_applies_them(self):
        cli = cli_module()
        bundle, target, rollback, info = self.rollback_fixture()
        (target / ".config/xfce4/settings.xml").write_text("edited")
        code, out, err = self.rollback(info, "--rollback-dir", str(rollback))
        self.assertEqual(code, 0, err)
        self.assertIn("CONFLICT: 1", out)
        self.assertIn("settings.xml", out)
        self.assertIn("--include-changed", out)
        with patch.object(cli, "assert_xfce_stopped"):
            code, out, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply",
                                           "--include-changed")
        self.assertEqual(code, 0, err)
        self.assertEqual((target / ".config/xfce4/settings.xml").read_text(), "original")

    def test_rollback_without_a_bundle_says_cannot_verify_prominently(self):
        bundle, target, rollback, info = self.rollback_fixture()
        bundle.rename(self.root / "moved")
        code, out, err = self.rollback(info, "--rollback-dir", str(rollback))
        self.assertEqual(code, 0, err)
        self.assertIn("cannot verify", out.lower())
        self.assertIn("--bundle", out)
        code, out, err = self.rollback(info, "--rollback-dir", str(rollback),
                                       "--bundle", str(self.root / "moved"))
        self.assertEqual(code, 0, err)
        self.assertNotIn("cannot verify", out.lower())
        self.assertIn("PUT_BACK: 1", out)

    def test_rollback_requires_a_supported_fedora_and_a_journal(self):
        bundle, target, rollback, info = self.rollback_fixture()
        code, _, err = self.rollback(dict(info, version_id="42"), "--rollback-dir", str(rollback))
        self.assertEqual(code, 1)
        self.assertIn("Fedora 43 or newer", err)
        code, _, err = self.rollback(info, "--rollback-dir", str(self.root / "missing"))
        self.assertEqual(code, 1)

    def test_rollback_refuses_a_journal_from_another_home(self):
        bundle, target, rollback, info = self.rollback_fixture()
        other = self.root / "other-home"
        other.mkdir()
        code, _, err = self.rollback(dict(info, home=str(other)), "--rollback-dir", str(rollback))
        self.assertEqual(code, 1)
        self.assertIn("home", err)

    def test_second_rollback_is_refused(self):
        cli = cli_module()
        bundle, target, rollback, info = self.rollback_fixture()
        with patch.object(cli, "assert_xfce_stopped"):
            code, _, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply")
            self.assertEqual(code, 0, err)
        code, _, err = self.rollback(info, "--rollback-dir", str(rollback))
        self.assertEqual(code, 1)
        self.assertIn("already rolled back", err)

    def test_interrupted_rollback_exits_130_and_reports_undone_dir(self):
        cli = cli_module()
        bundle, target, rollback, info = self.rollback_fixture()
        original_link = os.link

        def interrupt(src, dest, **kwargs):
            if dest == "settings.xml" and kwargs.get("src_dir_fd") is not None:
                raise KeyboardInterrupt()
            return original_link(src, dest, **kwargs)

        with patch.object(cli, "assert_xfce_stopped"), \
             patch("fedora_migration.rollback.os.link", side_effect=interrupt):
            code, out, err = self.rollback(info, "--rollback-dir", str(rollback), "--apply")
        self.assertEqual(code, 130)
        undone = sorted(rollback.glob("undone-*"))
        self.assertIn(str(undone[0]), out + err)
        self.assertEqual(json.loads((rollback / "rollback-journal.json").read_text())["status"],
                         "interrupted")


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


def triage_package_data():
    """A schema 2 package inventory covering every triage class."""
    def entry(spec, repo, nevra):
        return {"spec": spec, "reason": "User", "from_repo": repo, "full_nevra": nevra}
    return {
        "rpm": {
            "status": "captured",
            "requests": ["bash.x86_64", "htop.x86_64", "zsh.x86_64", "hello.x86_64",
                         "ghost.x86_64", "local-tool.x86_64", "mystery.noarch"],
            "inventory": [
                entry("bash.x86_64", "fedora", "bash-0:5.2-1.fc43.x86_64"),
                entry("htop.x86_64", "fedora", "htop-0:3.4-1.fc43.x86_64"),
                entry("zsh.x86_64", "updates", "zsh-0:5.9-1.fc43.x86_64"),
                entry("hello.x86_64", "copr:u:p", "hello-0:1.0-1.fc43.x86_64"),
                entry("ghost.x86_64", "rpmfusion-free", "ghost-0:2.0-1.fc43.x86_64"),
                entry("local-tool.x86_64", "@commandline", "local-tool-0:9.1-1.x86_64"),
                entry("mystery.noarch", "<unknown>", "mystery-0:3-1.noarch")],
            "repos": [{"id": "rpmfusion-free", "name": "RPM Fusion", "base_urls": [],
                       "metalink": "https://mirrors.rpmfusion.example/metalink?repo=free",
                       "mirrorlist": "",
                       "gpg_keys": ["file:///etc/pki/rpm-gpg/RPM-GPG-KEY-rpmfusion-free"],
                       "repo_file_path": "/etc/yum.repos.d/rpmfusion-free.repo"}],
            "groups": [{"id": "core", "userinstalled": True}],
            "environments": [{"id": "xfce-desktop-environment", "groups": ["xfce-desktop"]}],
        },
        "flatpak": {"status": "not-installed", "apps": [], "remotes": []},
        "warnings": [],
    }


INSTALLED = {"bash.x86_64"}
AVAILABLE = {"bash.x86_64", "htop.x86_64", "zsh.x86_64"}


class RestoreTriageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "source-home"
        self.home.mkdir()
        self.target = self.root / "target-home"
        self.target.mkdir()
        self.info = {"id": "fedora", "version_id": "43", "pretty_name": "Fedora Linux 43",
                     "home": str(self.home), "uid": 1000, "username": "alice",
                     "architecture": "x86_64"}
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            os.environ.pop(variable, None)
        guard = patch.object(cli_module(), "assert_applications_closed")
        guard.start()
        self.addCleanup(guard.stop)
        self.installed, self.available = set(INSTALLED), set(AVAILABLE)
        self.bundle = self.make_bundle(triage_package_data())

    def make_bundle(self, package_data, name="backup"):
        from fedora_migration.bundle import create_bundle
        (self.home / ".config/xfce4").mkdir(parents=True, exist_ok=True)
        (self.home / ".config/xfce4/settings.xml").write_text("<channel name='xfce4-panel'/>\n")
        bundle = self.root / name
        create_bundle(self.home, bundle, self.info, package_data, settings_only=True)
        return bundle

    def invoke(self, *argv, bundle=None, run=None, xfce=None):
        """Run restore on a Fedora 44 destination with the destination queries mocked."""
        cli = cli_module()
        info = dict(self.info, version_id="44", home=str(self.target))
        out, err = io.StringIO(), io.StringIO()
        self.collector = patch.object(
            cli.packages, "collect_destination_rpms",
            side_effect=lambda: (set(self.installed), set(self.available)))
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli, "assert_xfce_stopped", side_effect=xfce), \
             patch.object(cli, "check_selinux_tools", return_value=False), \
             self.collector as collector, \
             patch.object(cli.packages, "_run", side_effect=run or (lambda *a, **k: "")) as runner, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["restore", "--bundle", str(bundle or self.bundle), *argv])
        self.collector_calls = collector.call_count
        self.runner = runner
        return code, out.getvalue(), err.getvalue()

    def rpm_argv(self):
        return [call.args[0] for call in self.runner.call_args_list if call.args[0][0] == "sudo"]

    def test_preview_shows_the_triage_and_changes_nothing(self):
        code, out, err = self.invoke("--install-packages")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.collector_calls, 1)
        self.assertIn("RPM triage", out)
        self.assertIn("Already installed: 1", out)
        self.assertIn("To install: 2", out)
        self.assertIn("htop.x86_64", out)
        self.assertIn("Unavailable: 2", out)
        self.assertIn("copr:u:p", out)
        self.assertIn("repository copr:u:p from the source system is not recorded", out)
        self.assertIn("enable repository rpmfusion-free", out)
        self.assertIn("https://mirrors.rpmfusion.example/metalink?repo=free", out)
        self.assertNotIn("RPM-GPG-KEY", out)
        self.assertIn("Manual handling: 2", out)
        self.assertIn("local-tool-0:9.1-1.x86_64", out)
        self.assertIn("mystery-0:3-1.noarch", out)
        self.assertIn("xfce-desktop-environment", out)
        self.assertIn("only explicit packages are requested", out)
        self.assertIn("sudo dnf5 install htop.x86_64 zsh.x86_64", out)
        self.assertIn("--skip-unavailable", out)
        self.assertIn("Preview only", out)
        self.assertEqual(self.runner.call_count, 0)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_triage_summary_lists_are_capped(self):
        data = triage_package_data()
        many = [f"pkg{i:03}.x86_64" for i in range(60)]
        data["rpm"]["requests"] = many
        data["rpm"]["inventory"] = [
            {"spec": spec, "reason": "User", "from_repo": "fedora",
             "full_nevra": f"{spec[:-7]}-0:1-1.x86_64"} for spec in many]
        self.available = set(many)
        code, out, err = self.invoke("--install-packages", bundle=self.make_bundle(data, "many"))
        self.assertEqual(code, 0, err)
        self.assertIn("To install: 60", out)
        self.assertIn("... and 35 more", out)

    def test_apply_refuses_unavailable_packages_before_any_change(self):
        stopped = []
        code, out, err = self.invoke("--apply", "--install-packages",
                                     xfce=lambda: stopped.append(1))
        self.assertEqual(code, 1)
        self.assertIn("--skip-unavailable", err)
        self.assertIn("copr:u:p", err + out)
        self.assertIn("rpmfusion-free", err + out)
        self.assertEqual(self.runner.call_count, 0)
        self.assertEqual(stopped, [])
        self.assertEqual(list(self.target.iterdir()), [])

    def test_apply_refusal_never_reaches_install_or_restore(self):
        cli = cli_module()
        with patch.object(cli.packages, "install_packages") as install, \
             patch.object(cli.bundle, "restore_bundle") as restore:
            code, _, _ = self.invoke("--apply", "--install-packages")
        self.assertEqual(code, 1)
        install.assert_not_called()
        restore.assert_not_called()

    def test_skip_unavailable_installs_only_installable_packages(self):
        code, out, err = self.invoke("--apply", "--install-packages", "--skip-unavailable")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rpm_argv(),
                         [["sudo", "dnf5", "install", "--skip-unavailable", "htop.x86_64", "zsh.x86_64"]])
        flat = " ".join(self.rpm_argv()[0])
        for left_out in ("bash.x86_64", "hello.x86_64", "ghost.x86_64", "local-tool", "mystery"):
            self.assertNotIn(left_out, flat)
        self.assertIn("Skipped", out)
        self.assertIn("hello.x86_64", out)
        self.assertTrue((self.target / ".config/xfce4/settings.xml").is_file())

    def test_manual_packages_never_block_and_are_never_installed(self):
        data = triage_package_data()
        keep = {"bash.x86_64", "htop.x86_64", "local-tool.x86_64", "mystery.noarch"}
        data["rpm"]["requests"] = [r for r in data["rpm"]["requests"] if r in keep]
        data["rpm"]["inventory"] = [i for i in data["rpm"]["inventory"] if i["spec"] in keep]
        code, out, err = self.invoke("--apply", "--install-packages",
                                     bundle=self.make_bundle(data, "manual-only"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rpm_argv(), [["sudo", "dnf5", "install", "htop.x86_64"]])
        self.assertNotIn("--skip-unavailable", self.rpm_argv()[0])
        self.assertIn("local-tool-0:9.1-1.x86_64", out)

    def test_nothing_to_install_runs_no_rpm_command(self):
        self.installed = set(AVAILABLE)
        data = triage_package_data()
        data["rpm"]["requests"] = ["bash.x86_64", "htop.x86_64"]
        data["rpm"]["inventory"] = data["rpm"]["inventory"][:2]
        code, out, err = self.invoke("--apply", "--install-packages",
                                     bundle=self.make_bundle(data, "installed"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rpm_argv(), [])
        self.assertIn("Already installed: 2", out)

    def test_skip_unavailable_is_not_passed_to_dnf_unless_given(self):
        self.available |= {"hello.x86_64", "ghost.x86_64"}
        code, _, err = self.invoke("--apply", "--install-packages")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rpm_argv(), [["sudo", "dnf5", "install", "ghost.x86_64",
                                            "hello.x86_64", "htop.x86_64", "zsh.x86_64"]])

    def test_destination_is_not_queried_when_it_is_not_needed(self):
        for argv in (["--install-packages", "--skip-rpm"], [], ["--skip-unavailable"]):
            with self.subTest(argv=argv):
                code, _, err = self.invoke(*argv)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.collector_calls, 0)
        data = triage_package_data()
        data["rpm"].update(requests=[], inventory=[])
        code, _, err = self.invoke("--install-packages", bundle=self.make_bundle(data, "empty"))
        self.assertEqual((code, self.collector_calls), (0, 0), err)

    def test_query_failure_stops_before_any_change(self):
        cli = cli_module()
        info = dict(self.info, version_id="44", home=str(self.target))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "platform_info", return_value=info), \
             patch.object(cli.packages, "collect_destination_rpms",
                          side_effect=cli.packages.PackageError("cannot download metadata")), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["restore", "--bundle", str(self.bundle), "--apply", "--install-packages"])
        self.assertEqual(code, 1)
        self.assertIn("cannot download metadata", err.getvalue())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_skip_unavailable_without_install_packages_is_reported_as_unused(self):
        code, out, err = self.invoke("--skip-unavailable")
        self.assertEqual(code, 0, err)
        self.assertIn("--skip-unavailable has no effect without --install-packages", out)

    def test_help_documents_skip_unavailable(self):
        text = " ".join(action.format_help() for action in
                        cli_module().parser()._subparsers._group_actions[0].choices.values())
        self.assertIn("--skip-unavailable", text)

    def test_flatpak_batches_follow_the_rpm_command(self):
        data = triage_package_data()
        data["rpm"].update(requests=["htop.x86_64"], inventory=data["rpm"]["inventory"][1:2])
        data["flatpak"] = {
            "status": "captured",
            "apps": [{"scope": "user", "origin": "flathub", "ref": "org.b.B/x86_64/stable"},
                     {"scope": "user", "origin": "flathub", "ref": "org.a.A/x86_64/stable"}],
            "remotes": [{"scope": "user", "name": "flathub", "url": "https://dl.flathub.org/repo/"}]}
        code, out, err = self.invoke("--install-packages",
                                     bundle=self.make_bundle(data, "flatpak"))
        self.assertEqual(code, 0, err)
        self.assertIn("flatpak install --user flathub org.a.A/x86_64/stable org.b.B/x86_64/stable", out)
        self.assertLess(out.index("sudo dnf5 install"), out.index("flatpak install"))


if __name__ == "__main__":
    unittest.main()
