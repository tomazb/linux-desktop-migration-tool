import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from fedora_migration import bundle as bundle_module
from fedora_migration.bundle import (
    BundleError, create_bundle, restore_bundle, verify_bundle,
)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "source"
        self.home.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.bundle = self.root / "bundle"

    def write(self, path, data=b"settings", mode=0o640):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def backup(self, **kwargs):
        return create_bundle(self.home, self.bundle, {"version_id": "43"}, {},
                             **kwargs)

    def manifest(self, change):
        path = self.bundle / "manifest.json"
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))

    def test_roundtrip_bytes_modes_mtime_and_symlink(self):
        source = self.write(self.home / ".config/xfce4/test", b"\x00hello", 0o600)
        os.utime(source, ns=(1700000000000000000, 1700000000123456789))
        (source.parent / "link").symlink_to("/outside/never-follow")
        self.backup()
        manifest = verify_bundle(self.bundle)
        self.assertEqual(manifest["schema_version"], 1)
        summary = restore_bundle(self.bundle, self.target, dry_run=False)
        restored = self.target / ".config/xfce4/test"
        self.assertEqual(restored.read_bytes(), source.read_bytes())
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o600)
        self.assertEqual(restored.stat().st_mtime_ns, source.stat().st_mtime_ns)
        self.assertEqual(os.readlink(restored.parent / "link"), "/outside/never-follow")
        self.assertEqual(summary["files"], 1)
        self.assertEqual(summary["symlinks"], 1)

    def test_dry_run_makes_no_files_or_directories(self):
        self.write(self.home / ".bashrc")
        self.backup()
        summary = restore_bundle(self.bundle, self.target)
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertIsNone(summary["backup_dir"])

    def test_settings_include_saved_background_assets(self):
        self.write(self.home / ".local/share/backgrounds/old wallpaper.png", b"wallpaper")
        self.backup(settings_only=True)
        restore_bundle(self.bundle, self.target, dry_run=False)
        self.assertEqual(
            (self.target / ".local/share/backgrounds/old wallpaper.png").read_bytes(),
            b"wallpaper",
        )

    def test_online_accounts_require_explicit_secret_choice_even_in_parent_include(self):
        self.write(self.home / ".config/goa-1.0/accounts.conf", b"account details")
        self.backup(include=(".config",))
        self.assertFalse((self.bundle / "payload/.config/goa-1.0").exists())
        self.assertTrue(any("goa-1.0" in warning
                            for warning in verify_bundle(self.bundle)["warnings"]))

    def test_secret_opt_in_preserves_gnupg_and_online_accounts_but_skips_special_files(self):
        persistent = (
            ".gnupg/pubring.kbx", ".gnupg/trustdb.gpg", ".gnupg/gpg.conf",
            ".gnupg/private-keys-v1.d/test.key", ".gnupg/openpgp-revocs.d/test.rev",
            ".config/goa-1.0/accounts.conf", ".local/share/keyrings/login.keyring",
        )
        for relative in persistent:
            self.write(self.home / relative, relative.encode(), 0o600)
        os.mkfifo(self.home / ".gnupg/runtime-pipe")
        self.backup(include_secrets=True)
        restore_bundle(self.bundle, self.target, dry_run=False)
        for relative in persistent:
            with self.subTest(relative=relative):
                restored = self.target / relative
                self.assertEqual(restored.read_bytes(), relative.encode())
                self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o600)
        self.assertFalse((self.target / ".gnupg/runtime-pipe").exists())

    def test_overwrite_retains_previous_files_and_symlinks(self):
        self.write(self.home / ".bashrc", b"new")
        self.write(self.home / ".bash_profile", b"profile")
        self.write(self.target / ".bashrc", b"old", 0o600)
        (self.target / ".bash_profile").symlink_to("/outside/original")
        self.write(self.target / "unrelated", b"keep")
        self.backup()
        summary = restore_bundle(self.bundle, self.target, dry_run=False)
        previous = Path(summary["backup_dir"]) / "previous"
        self.assertEqual((previous / ".bashrc").read_bytes(), b"old")
        self.assertEqual(os.readlink(previous / ".bash_profile"), "/outside/original")
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"new")
        self.assertEqual((self.target / "unrelated").read_bytes(), b"keep")
        journal = json.loads((previous.parent / "journal.json").read_text())
        self.assertEqual(set(journal["replaced"]), {".bashrc", ".bash_profile"})
        self.assertEqual(journal["status"], "complete")

    def test_corrupt_missing_and_extra_payload_rejected(self):
        self.write(self.home / ".bashrc", b"good")
        self.backup()
        data = self.bundle / "payload/.bashrc"
        data.write_bytes(b"evil")
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)
        data.unlink()
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)
        data.write_bytes(b"good")
        (self.bundle / "payload/extra").touch()
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

    def test_unsafe_manifest_paths_and_duplicates_rejected(self):
        self.write(self.home / ".bashrc")
        self.backup()
        original = (self.bundle / "manifest.json").read_bytes()
        for unsafe in ("../escape", "/tmp/escape", "a//b", "./a", "a/../b", ""):
            with self.subTest(unsafe=unsafe):
                (self.bundle / "manifest.json").write_bytes(original)
                self.manifest(lambda m: m["entries"][0].update(path=unsafe))
                with self.assertRaises(BundleError):
                    verify_bundle(self.bundle)
        (self.bundle / "manifest.json").write_bytes(original)
        self.manifest(lambda m: m["entries"].append(m["entries"][0].copy()))
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

    def test_payload_symlink_ancestor_rejected(self):
        self.write(self.home / ".config/xfce4/test")
        self.backup()
        payload = self.bundle / "payload/.config"
        shutil.rmtree(payload)
        payload.symlink_to(self.home / ".config", target_is_directory=True)
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

    def test_destination_symlink_ancestor_and_directory_conflict_preflight(self):
        self.write(self.home / ".config/xfce4/test")
        self.write(self.home / ".bashrc")
        self.backup()
        (self.target / ".config").symlink_to(self.home / ".config")
        with self.assertRaises(BundleError):
            restore_bundle(self.bundle, self.target, dry_run=False)
        self.assertFalse((self.target / ".bashrc").exists())
        (self.target / ".config").unlink()
        (self.target / ".bashrc").mkdir()
        with self.assertRaises(BundleError):
            restore_bundle(self.bundle, self.target, dry_run=False)
        self.assertFalse((self.target / ".config").exists())

    def test_settings_only_include_secrets_and_xdg_config(self):
        self.write(self.home / ".config/user-dirs.dirs",
                   b'XDG_DOCUMENTS_DIR="$HOME/Work Files"\n'
                   b'XDG_DESKTOP_DIR="$HOME"\n'
                   b'XDG_MUSIC_DIR="$(touch /tmp/should-not-execute)"\n')
        self.write(self.home / "Work Files/document")
        self.write(self.home / ".mozilla/profile")
        self.write(self.home / ".ssh/id_test")
        self.write(self.home / "custom/file")
        manifest = self.backup(settings_only=True, include=("custom",),
                               include_secrets=True)
        paths = {entry["path"] for entry in manifest["entries"]}
        self.assertIn(".ssh/id_test", paths)
        self.assertIn("custom/file", paths)
        self.assertNotIn(".mozilla/profile", paths)
        self.assertNotIn("Work Files/document", paths)

    def test_xdg_custom_directory_and_disabled_home(self):
        self.write(self.home / ".config/user-dirs.dirs",
                   b'XDG_DOCUMENTS_DIR="$HOME/Work Files"\nXDG_DESKTOP_DIR="$HOME"\n')
        self.write(self.home / "Work Files/document")
        self.write(self.home / "unselected")
        manifest = self.backup()
        self.assertIn("Work Files", manifest["selected_paths"])
        self.assertNotIn("unselected", {e["path"] for e in manifest["entries"]})

    def test_reject_whole_home_external_include_and_output_inside_home(self):
        for include in (".", "../outside", "/absolute"):
            with self.subTest(include=include), self.assertRaises(BundleError):
                self.backup(include=(include,))
        with self.assertRaises(BundleError):
            create_bundle(self.home, self.home / "backup", {}, {})
        self.assertFalse((self.home / "backup").exists())

    def test_source_symlink_ancestor_rejected_and_special_skipped(self):
        external = self.root / "external"
        self.write(external / "file")
        (self.home / "linked").symlink_to(external)
        with self.assertRaises(BundleError):
            self.backup(include=("linked/file",))
        os.mkfifo(self.home / ".bashrc")
        manifest = self.backup()
        self.assertTrue(any("special" in text.lower() for text in manifest["warnings"]))
        self.assertFalse((self.bundle / "payload/.bashrc").exists())

    def test_changing_source_aborts_without_completed_bundle(self):
        source = self.write(self.home / ".bashrc", b"before")
        original = shutil.copyfileobj

        def change_during_copy(src, dst, *args, **kwargs):
            original(src, dst, *args, **kwargs)
            source.write_bytes(b"after and longer")

        with patch("fedora_migration.bundle.shutil.copyfileobj",
                   side_effect=change_during_copy):
            with self.assertRaises(BundleError):
                self.backup()
        self.assertFalse(self.bundle.exists())

    def test_all_validation_finishes_before_any_restore_writes(self):
        self.write(self.home / ".bashrc")
        self.write(self.home / ".bash_profile")
        self.backup()
        (self.bundle / "payload/.bash_profile").write_bytes(b"corrupt")
        with self.assertRaises(BundleError):
            restore_bundle(self.bundle, self.target, dry_run=False)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_secrets_excluded_and_missing_paths_reported_by_default(self):
        self.write(self.home / ".ssh/id_test")
        self.write(self.home / ".local/share/keyrings/login.keyring")
        manifest = self.backup()
        self.assertEqual(manifest["entries"], [])
        self.assertTrue(any("Missing source path" in w for w in manifest["warnings"]))

    def test_broad_include_and_xdg_cannot_silently_include_known_keys(self):
        self.write(self.home / ".ssh/id_test")
        self.write(self.home / ".local/share/keyrings/login.keyring")
        self.write(self.home / ".local/share/other", b"include")
        self.write(self.home / ".config/user-dirs.dirs",
                   b'XDG_DOCUMENTS_DIR="$HOME/.ssh"\n')
        manifest = self.backup(include=(".local",))
        paths = {entry["path"] for entry in manifest["entries"]}
        self.assertNotIn(".ssh/id_test", paths)
        self.assertNotIn(".local/share/keyrings/login.keyring", paths)
        self.assertIn(".local/share/other", paths)

    def test_manifest_file_cannot_be_parent_of_another_entry(self):
        self.write(self.home / ".config/xfce4/test")
        self.backup()

        def corrupt(manifest):
            entry = next(e for e in manifest["entries"] if e["path"] == ".config")
            entry.update(type="symlink", target=str(self.home / ".config"))

        self.manifest(corrupt)
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

    def test_special_destination_rejected_before_writes(self):
        self.write(self.home / ".bashrc")
        self.backup()
        os.mkfifo(self.target / ".bashrc")
        with self.assertRaises(BundleError):
            restore_bundle(self.bundle, self.target, dry_run=False)
        self.assertEqual([p.name for p in self.target.iterdir()], [".bashrc"])

    def test_explicit_rollback_path_checked_even_in_dryrun(self):
        self.write(self.home / ".bashrc")
        self.backup()
        existing = self.root / "rollback"
        existing.mkdir()
        with self.assertRaises(BundleError):
            restore_bundle(self.bundle, self.target, backup_dir=existing)
        with self.assertRaises(BundleError):
            restore_bundle(self.bundle, self.target,
                           backup_dir=self.target / ".bashrc/rollback")
        self.assertEqual(list(self.target.iterdir()), [])

    def test_failed_replace_retains_original_and_actionable_journal(self):
        self.write(self.home / ".bashrc", b"new")
        self.write(self.target / ".bashrc", b"old")
        self.backup()
        rollback = self.root / "rollback"
        replace = os.replace

        def fail_target(src, dst):
            if Path(dst) == self.target / ".bashrc":
                raise OSError("injected disk failure")
            return replace(src, dst)

        with patch("fedora_migration.bundle.os.replace", side_effect=fail_target):
            with self.assertRaisesRegex(BundleError, "Retained rollback directory"):
                restore_bundle(self.bundle, self.target, dry_run=False,
                               backup_dir=rollback)
        self.assertEqual((rollback / "previous/.bashrc").read_bytes(), b"old")
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"old")
        journal = json.loads((rollback / "journal.json").read_text())
        self.assertEqual(journal["status"], "failed")
        self.assertEqual(journal["replaced"], [".bashrc"])
        self.assertEqual(list(self.target.glob(".fedora-migration-*")), [])

    def test_existing_home_parent_mode_preserved(self):
        source = self.write(self.home / ".config/xfce4/test")
        source.parent.chmod(0o750)
        source.parent.parent.chmod(0o700)
        config = self.target / ".config"
        config.mkdir(mode=0o755)
        config.chmod(0o755)
        self.backup()
        restore_bundle(self.bundle, self.target, dry_run=False)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((config / "xfce4").stat().st_mode), 0o750)

    def test_custom_xdg_base_directory_is_not_silently_omitted(self):
        self.write(self.home / "my-config/xfce4/panel.xml")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / "my-config")}):
            with self.assertRaisesRegex(BundleError, "XDG_CONFIG_HOME"):
                self.backup()
        self.assertFalse(self.bundle.exists())

    def test_custom_xdg_data_directory_rejected_before_restore(self):
        self.write(self.home / ".bashrc")
        self.backup()
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.target / "my-data")}):
            with self.assertRaisesRegex(BundleError, "XDG_DATA_HOME"):
                restore_bundle(self.bundle, self.target, dry_run=False)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_unsupported_escaped_xdg_assignment_does_not_fall_back(self):
        assignment = 'XDG_DOCUMENTS_DIR="$HOME/Work \\"Quoted\\""\n'
        self.write(self.home / ".config/user-dirs.dirs", assignment.encode())
        self.write(self.home / 'Work "Quoted"/important.txt')
        with self.assertRaisesRegex(BundleError, "XDG_DOCUMENTS_DIR"):
            self.backup()
        self.assertFalse(self.bundle.exists())

    def test_out_of_range_timestamp_rejected_before_any_restore_write(self):
        self.write(self.home / ".bash_profile", b"first")
        self.write(self.home / ".bashrc", b"second")
        self.backup()
        self.manifest(lambda m: next(e for e in m["entries"] if e["path"] == ".bashrc").update(mtime_ns=10**100))
        with self.assertRaises(BundleError):
            restore_bundle(self.bundle, self.target, dry_run=False)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_verify_rejects_out_of_range_timestamp_metadata(self):
        self.write(self.home / ".bashrc")
        self.backup()
        self.manifest(lambda m: m["entries"][0].update(mtime_ns=-(10**100)))
        with self.assertRaises(BundleError):
            verify_bundle(self.bundle)

    def test_journal_serialization_is_bounded_by_final_plan_size(self):
        for number in range(200):
            self.write(self.home / f".var/app/org.example.App/cache/{number:04d}", b"x")
        self.backup()
        written = []
        original = bundle_module._write_json

        def record_size(path, data):
            original(path, data)
            if path.name.startswith("journal-"):
                written.append(path.stat().st_size)

        with patch.object(bundle_module, "_write_json", side_effect=record_size):
            result = restore_bundle(self.bundle, self.target, dry_run=False)
        final_size = (Path(result["backup_dir"]) / "journal.json").stat().st_size
        self.assertLessEqual(sum(written), 4 * final_size + 1024)


if __name__ == "__main__":
    unittest.main()
