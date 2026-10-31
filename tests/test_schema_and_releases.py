"""Manifest schema versions and supported Fedora release pairs."""

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

import fedora_migration
from fedora_migration.bundle import (
    BundleError, create_bundle, restore_bundle, verify_bundle,
)

from test_bundle import BundleFixture

ROOT = Path(__file__).resolve().parents[1]

# tests/fixtures/bundle-v1 is a REAL schema-1 bundle (tool_version "1.0"), written
# by the code that shipped before schema 2 with a one-off script and frozen here.
# It stands for the bundles users already hold. NEVER regenerate it: if a change
# breaks these tests, the change is what has to give. Contents (home /home/alice,
# source Fedora 43 x86_64): .bashrc (0644), .config/xfce4/terminal/terminalrc (0600),
# xfconf/xfce-perchannel-xml/xfce4-panel.xml (0644), and the relative symlink
# .config/xfce4/panel-link.xml -> xfconf/xfce-perchannel-xml/xfce4-panel.xml,
# plus RPM (2 requests) and Flatpak (1 user app) inventories. The bundle directory
# must not gain extra files (they would fail verification), so it has no README.
FROZEN_V1 = ROOT / "tests" / "fixtures" / "bundle-v1"
TERMINALRC = ".config/xfce4/terminal/terminalrc"


def cli_module():
    return importlib.import_module("fedora_migration.cli")


def package_fixture():
    return {
        "rpm": {"status": "captured", "requests": [], "inventory": [],
                "repos": [], "groups": [], "environments": []},
        "flatpak": {"status": "not-installed", "apps": [], "remotes": []},
        "warnings": [],
    }


class SchemaVersionTests(BundleFixture):
    def setUp(self):
        super().setUp()
        self.write(self.home / ".config/xfce4/settings.xml", b"<channel/>\n", 0o600)

    def test_new_bundles_are_schema_2_and_record_the_tool_version(self):
        manifest = self.backup()
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["tool_version"], fedora_migration.__version__)
        self.assertNotEqual(fedora_migration.__version__, "1.0")
        on_disk = verify_bundle(self.bundle)
        self.assertEqual((on_disk["schema_version"], on_disk["tool_version"]),
                         (2, fedora_migration.__version__))

    def test_schema_2_bundle_roundtrips_bytes_and_modes(self):
        self.backup()
        summary = restore_bundle(self.bundle, self.target, dry_run=False)
        restored = self.target / ".config/xfce4/settings.xml"
        self.assertEqual(restored.read_bytes(), b"<channel/>\n")
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o600)
        self.assertEqual(summary["files"], 1)

    def test_tool_version_is_informational_for_both_schemas(self):
        self.backup()
        for schema in (1, 2):
            for tool_version in ("9.9", "1.0", "1.1.0", "0.1-dev"):
                with self.subTest(schema=schema, tool_version=tool_version):
                    self.manifest(lambda data: data.update(schema_version=schema,
                                                           tool_version=tool_version))
                    self.assertEqual(verify_bundle(self.bundle)["tool_version"], tool_version)

    def test_invalid_tool_version_is_rejected_for_both_schemas(self):
        self.backup()
        for schema in (1, 2):
            for value in (None, 1, 1.0, ["1.0"], {"v": 1}, True, "", "1.0\n", "1\x00", "a\tb", "x\x7f",
                          "1.1\u0085", "1.1\u009b", "\u009f"):
                with self.subTest(schema=schema, tool_version=value):
                    self.manifest(lambda data: data.update(schema_version=schema, tool_version=value))
                    with self.assertRaisesRegex(BundleError, "tool version"):
                        verify_bundle(self.bundle)
        self.manifest(lambda data: data.pop("tool_version"))
        with self.assertRaisesRegex(BundleError, "tool version"):
            verify_bundle(self.bundle)

    def test_newer_schema_is_rejected_with_an_update_hint(self):
        self.backup()
        for schema in (3, 4, 99):
            self.manifest(lambda data: data.update(schema_version=schema))
            with self.subTest(schema=schema), self.assertRaises(BundleError) as caught:
                verify_bundle(self.bundle)
            self.assertIn("newer version of this tool", str(caught.exception))
            self.assertIn("update the tool", str(caught.exception))

    def test_other_schema_values_are_rejected(self):
        self.backup()
        for schema in (0, -1, "2", 2.0, True, None):
            self.manifest(lambda data: data.update(schema_version=schema))
            with self.subTest(schema=schema), self.assertRaises(BundleError) as caught:
                verify_bundle(self.bundle)
            self.assertNotIn("newer version", str(caught.exception))

    def test_cli_verify_reports_the_newer_version_hint(self):
        self.backup()
        self.manifest(lambda data: data.update(schema_version=3))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_module().main(["verify", "--bundle", str(self.bundle)])
        self.assertEqual(code, 1)
        self.assertIn("newer version of this tool", err.getvalue())


class FrozenV1Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle-v1"
        shutil.copytree(FROZEN_V1, self.bundle, symlinks=True)
        self.target = self.root / "target-home"
        self.target.mkdir()
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            os.environ.pop(variable, None)
        guard = patch.object(cli_module(), "assert_applications_closed")
        guard.start()
        self.addCleanup(guard.stop)
        # Package triage queries the real destination; the frozen bundle tests must not.
        self.installed, self.available = set(), {"gcc.x86_64", "htop.x86_64"}
        collector = patch.object(cli_module().packages, "collect_destination_rpms",
                                 side_effect=lambda: (set(self.installed), set(self.available)))
        collector.start()
        self.addCleanup(collector.stop)
        self.info = {
            "id": "fedora", "version_id": "44", "pretty_name": "Fedora Linux 44",
            "home": str(self.target), "uid": 1000, "username": "bob",
            "architecture": "x86_64",
        }

    def manifest_data(self):
        return json.loads((FROZEN_V1 / "manifest.json").read_text())

    def invoke(self, *argv, info=None):
        cli = cli_module()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "platform_info", return_value=info or self.info), \
             patch.object(cli, "assert_xfce_stopped"), \
             patch.object(cli, "check_selinux_tools", return_value=False), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def assert_restored_exactly(self):
        manifest = self.manifest_data()
        modes = {entry["path"]: entry["mode"] for entry in manifest["entries"]}
        self.assertEqual(modes[TERMINALRC], 0o600)
        for entry in manifest["entries"]:
            restored = self.target / entry["path"]
            if entry["type"] == "file":
                self.assertEqual(restored.read_bytes(),
                                 (FROZEN_V1 / "payload" / entry["path"]).read_bytes())
                self.assertEqual(stat.S_IMODE(restored.lstat().st_mode), entry["mode"])
            elif entry["type"] == "symlink":
                self.assertTrue(restored.is_symlink())
                self.assertEqual(os.readlink(restored), entry["target"])
            else:
                self.assertTrue(restored.is_dir())


class FrozenV1BundleTests(FrozenV1Fixture):
    def test_the_fixture_is_still_the_original_schema_1_bundle(self):
        manifest = self.manifest_data()
        self.assertEqual((manifest["schema_version"], manifest["tool_version"]), (1, "1.0"))
        self.assertEqual(manifest["source"]["version_id"], "43")
        kinds = {entry["path"]: entry["type"] for entry in manifest["entries"]}
        self.assertEqual(kinds[".config/xfce4/panel-link.xml"], "symlink")
        self.assertEqual(len(manifest["packages"]["rpm"]["requests"]), 2)
        self.assertEqual(len(manifest["packages"]["flatpak"]["apps"]), 1)

    def test_frozen_v1_bundle_verifies(self):
        manifest = verify_bundle(self.bundle)
        self.assertEqual(manifest["schema_version"], 1)

    def test_frozen_v1_bundle_verifies_whatever_permissions_git_checked_out(self):
        for path in [self.bundle, *self.bundle.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
        self.assertEqual(verify_bundle(self.bundle)["schema_version"], 1)

    def test_frozen_v1_bundle_restores_byte_exact_with_manifest_modes(self):
        summary = restore_bundle(self.bundle, self.target, dry_run=False)
        self.assertEqual((summary["files"], summary["symlinks"]), (3, 1))
        self.assert_restored_exactly()

    def test_frozen_v1_bundle_survives_tampering_checks_like_any_bundle(self):
        (self.bundle / "payload/.bashrc").write_bytes(b"tampered")
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)


class FrozenV1CLITests(FrozenV1Fixture):
    def test_verify_accepts_the_frozen_bundle(self):
        code, out, err = self.invoke("verify", "--bundle", str(self.bundle))
        self.assertEqual(code, 0, err)
        self.assertIn("verified", out.lower())
        self.assertIn("RPM requests: 2 (captured)", out)
        self.assertIn("Flatpak apps: 1 (captured)", out)

    def test_restore_previews_the_frozen_bundle_without_writing(self):
        code, out, err = self.invoke("restore", "--bundle", str(self.bundle),
                                     "--install-packages")
        self.assertEqual(code, 0, err)
        self.assertIn("Restore preview:", out)
        self.assertIn("sudo dnf5 install gcc.x86_64 htop.x86_64", out)
        self.assertIn("flatpak install --user flathub org.gimp.GIMP/x86_64/stable", out)
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertNotIn("has not been tested", out)

    def test_frozen_bundle_package_preview_is_triaged_without_recorded_repositories(self):
        self.installed, self.available = {"gcc.x86_64"}, set()
        code, out, err = self.invoke("restore", "--bundle", str(self.bundle),
                                     "--install-packages")
        self.assertEqual(code, 0, err)
        self.assertIn("Already installed: 1", out)
        self.assertIn("Unavailable: 1", out)
        self.assertIn("repository fedora from the source system is not recorded", out)
        self.assertNotIn("sudo dnf5 install", out)
        self.assertIn("flatpak install --user flathub org.gimp.GIMP/x86_64/stable", out)
        self.assertEqual(list(self.target.iterdir()), [])
        code, _, err = self.invoke("restore", "--bundle", str(self.bundle),
                                   "--install-packages", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("--skip-unavailable", err)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_restore_apply_restores_the_frozen_bundle_exactly(self):
        code, out, err = self.invoke("restore", "--bundle", str(self.bundle), "--apply")
        self.assertEqual(code, 0, err)
        self.assertIn("Restored 3 files and 1 symlinks.", out)
        self.assert_restored_exactly()

    def test_frozen_bundle_restores_on_later_releases_within_two(self):
        for release in ("43", "45"):
            with self.subTest(release=release):
                target = self.root / f"home-{release}"
                target.mkdir()
                code, out, err = self.invoke(
                    "restore", "--bundle", str(self.bundle), "--apply",
                    info=dict(self.info, version_id=release, home=str(target)))
                self.assertEqual(code, 0, err)
                self.assertTrue((target / TERMINALRC).is_file())

    def test_rollback_on_a_later_release_undoes_a_frozen_bundle_restore(self):
        (self.target / ".bashrc").write_bytes(b"mine")
        rollback = self.root / "rollback"
        code, _, err = self.invoke("restore", "--bundle", str(self.bundle), "--apply",
                                   "--rollback-dir", str(rollback))
        self.assertEqual(code, 0, err)
        info = dict(self.info, version_id="45")
        code, out, err = self.invoke("rollback", "--rollback-dir", str(rollback), info=info)
        self.assertEqual(code, 0, err)
        self.assertIn("PUT_BACK: 1", out)
        code, _, err = self.invoke("rollback", "--rollback-dir", str(rollback), "--apply",
                                   info=info)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"mine")


class ReleaseRuleTests(unittest.TestCase):
    """The pure functions behind the supported Fedora release pairs."""

    def test_release_numbers_are_plain_decimal_integers(self):
        cli = cli_module()
        self.assertEqual(cli.parse_release("43"), 43)
        self.assertEqual(cli.parse_release("100"), 100)
        for value in ("", "rawhide", "43.1", "+43", "-43", " 43", "43\n", "0x2b", "४३",
                      "04", None, 43):
            with self.subTest(value=value):
                self.assertIsNone(cli.parse_release(value))

    def test_backup_accepts_fedora_43_and_newer(self):
        cli = cli_module()
        for release in ("43", "44", "45", "99"):
            self.assertEqual(cli.platform_release({"id": "fedora", "version_id": release,
                                                   "pretty_name": "F"}), int(release))

    def test_old_unknown_and_non_fedora_systems_are_refused(self):
        cli = cli_module()
        cases = [("fedora", "42"), ("fedora", "1"), ("fedora", ""), ("fedora", "rawhide"),
                 ("fedora", "44.1"), ("ubuntu", "44"), ("", "44")]
        for distro, release in cases:
            with self.subTest(id=distro, version_id=release):
                with self.assertRaisesRegex(cli.MigrationError, "Fedora 43 or newer"):
                    cli.platform_release({"id": distro, "version_id": release,
                                          "pretty_name": "Some OS"})

    def test_tested_pair_has_no_note(self):
        self.assertIsNone(cli_module().restore_release_note(43, 44))

    def test_other_allowed_pairs_get_the_untested_note(self):
        cli = cli_module()
        for source, destination in ((43, 43), (43, 45), (44, 44), (44, 45), (44, 46),
                                    (45, 45), (45, 47), (42, 43)):
            with self.subTest(pair=(source, destination)):
                self.assertEqual(
                    cli.restore_release_note(source, destination),
                    f"Fedora {source} → {destination} has not been tested with this tool; "
                    "review the preview carefully.")

    def test_downgrades_and_jumps_beyond_two_releases_are_refused(self):
        cli = cli_module()
        with self.assertRaisesRegex(cli.MigrationError, "downgrade"):
            cli.restore_release_note(44, 43)
        with self.assertRaisesRegex(cli.MigrationError, "downgrade"):
            cli.restore_release_note(46, 43)
        for source, destination in ((43, 46), (43, 50), (44, 47)):
            with self.subTest(pair=(source, destination)):
                with self.assertRaisesRegex(cli.MigrationError, "more than two releases"):
                    cli.restore_release_note(source, destination)

    def test_bundle_source_must_be_fedora_with_an_integer_release(self):
        cli = cli_module()
        self.assertEqual(cli.source_release({"id": "fedora", "version_id": "43"}), 43)
        for source in ({"id": "ubuntu", "version_id": "43"},
                       {"id": "fedora", "version_id": "rawhide"},
                       {"id": "fedora", "version_id": "43.1"}):
            with self.subTest(source=source):
                with self.assertRaisesRegex(cli.MigrationError, "made on Fedora"):
                    cli.source_release(source)


class ReleaseCLITests(FrozenV1Fixture):
    """Release rules through the commands, using the frozen Fedora 43 bundle."""

    def restore(self, release, *extra):
        info = dict(self.info, version_id=release)
        return self.invoke("restore", "--bundle", str(self.bundle), *extra, info=info)

    def test_43_to_44_restores_without_a_note(self):
        code, out, err = self.restore("44")
        self.assertEqual(code, 0, err)
        self.assertNotIn("has not been tested", out)

    def test_same_release_and_upgrade_of_one_or_two_get_the_note(self):
        for release in ("43", "45"):
            with self.subTest(release=release):
                code, out, err = self.restore(release)
                self.assertEqual(code, 0, err)
                self.assertIn(f"Fedora 43 → {release} has not been tested with this tool; "
                              "review the preview carefully.", out)

    def test_44_to_45_is_allowed_with_the_note(self):
        other = self.root / "bundle-44"
        shutil.copytree(self.bundle, other, symlinks=True)
        manifest = json.loads((other / "manifest.json").read_text())
        manifest["source"]["version_id"] = "44"
        (other / "manifest.json").write_text(json.dumps(manifest))
        info = dict(self.info, version_id="45")
        code, out, err = self.invoke("restore", "--bundle", str(other), info=info)
        self.assertEqual(code, 0, err)
        self.assertIn("Fedora 44 → 45 has not been tested", out)

    def test_downgrade_is_refused_before_anything_is_written(self):
        code, out, err = self.restore("42")
        self.assertEqual(code, 1)
        self.assertIn("Fedora 43 or newer", err)
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        manifest["source"]["version_id"] = "44"
        (self.bundle / "manifest.json").write_text(json.dumps(manifest))
        code, out, err = self.restore("43", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("downgrade", err)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_a_jump_of_three_releases_is_refused(self):
        code, out, err = self.restore("46", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("more than two releases", err)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_non_integer_destination_release_is_refused(self):
        for release in ("", "rawhide", "44.1"):
            with self.subTest(release=release):
                code, _, err = self.restore(release)
                self.assertEqual(code, 1)
                self.assertIn("Fedora 43 or newer", err)

    def test_non_fedora_or_unparseable_bundle_source_is_refused(self):
        for key, value in (("id", "ubuntu"), ("version_id", "rawhide")):
            manifest = json.loads((FROZEN_V1 / "manifest.json").read_text())
            manifest["source"][key] = value
            (self.bundle / "manifest.json").write_text(json.dumps(manifest))
            with self.subTest(key=key):
                code, _, err = self.restore("44")
                self.assertEqual(code, 1)
                self.assertIn("made on Fedora", err)

    def test_package_preview_names_the_destination_release(self):
        for release in ("44", "45"):
            with self.subTest(release=release):
                code, out, err = self.restore(release, "--install-packages")
                self.assertEqual(code, 0, err)
                self.assertIn(f"repositories must already be configured for Fedora {release}", out)

    def test_backup_runs_on_44_and_45_and_is_refused_on_42(self):
        cli = cli_module()
        for release, expected in (("44", 0), ("45", 0), ("42", 1), ("rawhide", 1), ("", 1)):
            home = self.root / f"home-{release or 'empty'}"
            home.mkdir()
            output = self.root / f"backup-{release or 'empty'}"
            info = dict(self.info, version_id=release, home=str(home))
            with self.subTest(release=release), \
                 patch.object(cli, "platform_info", return_value=info), \
                 patch.object(cli, "assert_xfce_stopped"), \
                 patch.object(cli.packages, "collect_packages", return_value=package_fixture()):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(["backup", "--output", str(output), "--settings-only"])
                self.assertEqual(code, expected, err.getvalue())
                if expected:
                    self.assertIn("Fedora 43 or newer", err.getvalue())
                    self.assertFalse(output.exists())
                else:
                    self.assertEqual(json.loads((output / "manifest.json").read_text())
                                     ["schema_version"], 2)

    def test_rollback_requires_fedora_43_or_newer(self):
        rollback = self.root / "rollback"
        code, _, err = self.invoke("restore", "--bundle", str(self.bundle), "--apply",
                                   "--rollback-dir", str(rollback))
        self.assertEqual(code, 0, err)
        for release in ("42", "rawhide"):
            code, _, err = self.invoke("rollback", "--rollback-dir", str(rollback),
                                       info=dict(self.info, version_id=release))
            self.assertEqual(code, 1)
            self.assertIn("Fedora 43 or newer", err)
        for release in ("43", "44", "45"):
            code, _, err = self.invoke("rollback", "--rollback-dir", str(rollback),
                                       info=dict(self.info, version_id=release))
            self.assertEqual(code, 0, err)

    def test_help_does_not_demand_exact_releases(self):
        parser = cli_module().parser()
        text = parser.format_help() + " ".join(
            action.format_help() for action in parser._subparsers._group_actions[0].choices.values())
        self.assertNotIn("on Fedora 43", text)
        self.assertNotIn("on Fedora 44", text)


if __name__ == "__main__":
    unittest.main()
