import errno
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


class BundleFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "source"
        self.home.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        self.bundle = self.root / "bundle"
        # The workflow rejects nondefault XDG base directories; keep the
        # caller's desktop environment from changing these filesystem fixtures.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            os.environ.pop(variable, None)

    def umask(self, value):
        previous = os.umask(value)
        self.addCleanup(os.umask, previous)

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


class BundleTests(BundleFixture):
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



def _fd_path(stream):
    return os.readlink(f"/proc/self/fd/{stream.fileno()}")


PROC_FDS = unittest.skipUnless(os.path.isdir("/proc/self/fd"),
                               "Copy instrumentation needs Linux /proc/self/fd")


@PROC_FDS
class RestorePrivacyTests(BundleFixture):
    def test_restore_temporary_files_stay_private_until_replacement(self):
        private = self.write(self.home / ".config/xfce4/private.xml", b"secret", 0o600)
        public = self.write(self.home / ".config/xfce4/public.xml", b"shared", 0o644)
        os.utime(private, ns=(1700000000000000000, 1700000000123456789))
        os.utime(public, ns=(1600000000000000000, 1600000000987654321))
        self.backup()
        # A transferred payload copy can be wider than its manifest mode.
        (self.bundle / "payload/.config/xfce4/private.xml").chmod(0o644)
        existing = self.target / ".config/xfce4"
        existing.mkdir(parents=True)
        for directory in (existing.parent, existing):
            directory.chmod(0o755)
        self.umask(0o022)
        observed = []
        copy, digest = shutil.copyfileobj, bundle_module._digest

        def record_copy(reader, writer, *args):
            name = Path(_fd_path(writer)).name
            if name.startswith(".fedora-migration-"):
                observed.append(("copy", stat.S_IMODE(os.fstat(writer.fileno()).st_mode)))
            return copy(reader, writer, *args)

        def record_digest(path):
            if Path(path).name.startswith(".fedora-migration-"):
                observed.append(("verify", stat.S_IMODE(Path(path).stat().st_mode)))
            return digest(path)

        with patch("fedora_migration.bundle.shutil.copyfileobj", side_effect=record_copy), \
             patch("fedora_migration.bundle._digest", side_effect=record_digest):
            restore_bundle(self.bundle, self.target, dry_run=False)
        self.assertEqual(sorted(observed), [("copy", 0o600)] * 2 + [("verify", 0o600)] * 2)
        for source, mode in ((private, 0o600), (public, 0o644)):
            restored = existing / source.name
            self.assertEqual(restored.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(restored.stat().st_mode), mode)
            self.assertEqual(restored.stat().st_mtime_ns, source.stat().st_mtime_ns)
        self.assertEqual(list(existing.glob(".fedora-migration-*")), [])


@PROC_FDS
class RecoveryCopyTests(BundleFixture):
    def fail_saving_bashrc(self, error):
        for name, data in ((".bash_profile", b"new profile"), (".bashrc", b"new rc")):
            self.write(self.home / name, data)
        self.write(self.target / ".bash_profile", b"old profile")
        self.write(self.target / ".bashrc", b"old rc contents", 0o600)
        self.backup()
        rollback = self.root / "rollback"
        copy = shutil.copyfileobj

        def fail_midway(reader, writer, *args):
            if _fd_path(reader) == str(self.target / ".bashrc"):
                writer.write(reader.read(3))
                writer.flush()
                raise error
            return copy(reader, writer, *args)

        with patch("fedora_migration.bundle.shutil.copyfileobj", side_effect=fail_midway):
            with self.assertRaises((BundleError, KeyboardInterrupt)) as caught:
                restore_bundle(self.bundle, self.target, dry_run=False, backup_dir=rollback)
        return rollback, caught.exception

    def assert_recovery_state(self, rollback, status):
        destination = self.target / ".bashrc"
        self.assertEqual(destination.read_bytes(), b"old rc contents")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        # The earlier, completed recovery copy remains valid and published.
        self.assertEqual((self.target / ".bash_profile").read_bytes(), b"new profile")
        self.assertEqual((rollback / "previous/.bash_profile").read_bytes(), b"old profile")
        self.assertFalse(os.path.lexists(rollback / "previous/.bashrc"))
        remaining = sorted(path.relative_to(rollback).as_posix()
                           for path in rollback.rglob("*") if not path.is_dir())
        self.assertEqual(remaining, ["journal.json", "previous/.bash_profile"])
        self.assertEqual(list(self.target.glob(".fedora-migration-*")), [])
        journal = json.loads((rollback / "journal.json").read_text())
        self.assertEqual(journal["status"], status)
        self.assertEqual(journal["replaced"], [".bash_profile", ".bashrc"])
        return journal

    def test_enospc_while_saving_original_publishes_no_partial_copy(self):
        rollback, error = self.fail_saving_bashrc(
            OSError(errno.ENOSPC, os.strerror(errno.ENOSPC)))
        self.assertIsInstance(error, BundleError)
        self.assertIn(f"Retained rollback directory: {rollback}", str(error))
        journal = self.assert_recovery_state(rollback, "failed")
        self.assertIn(os.strerror(errno.ENOSPC), journal["error"])

    def test_interrupt_while_saving_original_publishes_no_partial_copy(self):
        rollback, error = self.fail_saving_bashrc(KeyboardInterrupt())
        self.assertIsInstance(error, bundle_module.BundleInterrupted)
        self.assertIn(f"Retained rollback directory: {rollback}", str(error))
        self.assert_recovery_state(rollback, "interrupted")


class XDGConfigurationTests(BundleFixture):
    def test_symlinked_user_dirs_configuration_is_rejected_not_defaulted(self):
        real = self.write(self.home / ".config/user-dirs.real",
                          b'XDG_DOCUMENTS_DIR="$HOME/WorkFiles"\n')
        self.write(self.home / "WorkFiles/report.txt")
        link = self.home / ".config/user-dirs.dirs"
        for target in (real.name, "missing-user-dirs"):
            with self.subTest(target=target):
                if os.path.lexists(link):
                    link.unlink()
                link.symlink_to(target)
                with self.assertRaisesRegex(BundleError, r"user-dirs\.dirs.*symlink"):
                    self.backup()
                self.assertFalse(os.path.lexists(self.bundle))
        # Settings-only backups do not discover personal directories.
        self.backup(settings_only=True)
        self.assertEqual(os.readlink(self.bundle / "payload/.config/user-dirs.dirs"),
                         "missing-user-dirs")

    def test_non_regular_user_dirs_configuration_is_rejected_not_defaulted(self):
        (self.home / ".config/user-dirs.dirs").mkdir(parents=True)
        self.write(self.home / "Documents/report.txt")
        with self.assertRaisesRegex(BundleError, r"user-dirs\.dirs.*regular file"):
            self.backup()
        self.assertFalse(os.path.lexists(self.bundle))

    def test_missing_user_dirs_configuration_uses_xdg_defaults(self):
        self.write(self.home / "Documents/report.txt")
        manifest = self.backup()
        self.assertIn("Documents", manifest["selected_paths"])


class RestrictiveUmaskTests(BundleFixture):
    def test_backup_and_restore_work_when_umask_removes_owner_access(self):
        nested = self.write(self.home / ".config/xfce4/panel/launcher.desktop", b"new", 0o640)
        os.utime(nested, ns=(1700000000000000000, 1700000000123456789))
        self.write(self.target / ".config/xfce4/panel/launcher.desktop", b"old", 0o600)
        self.umask(0o777)
        self.backup()
        verify_bundle(self.bundle)
        rollback = self.root / "new-parent/rollback"
        restore_bundle(self.bundle, self.target, dry_run=False, backup_dir=rollback)
        restored = self.target / ".config/xfce4/panel/launcher.desktop"
        self.assertEqual(restored.read_bytes(), b"new")
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o640)
        self.assertEqual(restored.stat().st_mtime_ns, nested.stat().st_mtime_ns)
        saved = rollback / "previous/.config/xfce4/panel/launcher.desktop"
        self.assertEqual(saved.read_bytes(), b"old")
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(rollback.stat().st_mode), 0o700)


@PROC_FDS
class RecoveryDurabilityTests(BundleFixture):
    def test_nested_recovery_copy_is_durable_before_destination_replacement(self):
        relative = ".config/xfce4/panel/launcher.desktop"
        self.write(self.home / relative, b"new")
        original = self.write(self.target / relative, b"old", 0o604)
        os.utime(original, ns=(1600000000000000000, 1600000000987654321))
        expected_metadata = (0o604, original.stat().st_mtime_ns)
        self.backup()
        rollback = self.root / "new-parent/rollback"
        events = []
        fsync, fsync_directory, replace = os.fsync, bundle_module._fsync_directory, os.replace

        def record_fsync(fd):
            path = os.readlink(f"/proc/self/fd/{fd}")
            if "/staging/" in path:
                info = os.fstat(fd)
                events.append(("file", (stat.S_IMODE(info.st_mode), info.st_mtime_ns)))
            return fsync(fd)

        def record_directory(path):
            events.append(("directory", Path(path)))
            return fsync_directory(path)

        def record_replace(source, destination):
            events.append(("replace", Path(destination)))
            return replace(source, destination)

        with patch("fedora_migration.bundle.os.fsync", side_effect=record_fsync), \
             patch("fedora_migration.bundle._fsync_directory", side_effect=record_directory), \
             patch("fedora_migration.bundle.os.replace", side_effect=record_replace):
            restore_bundle(self.bundle, self.target, dry_run=False, backup_dir=rollback)
        # The recovery file's mode and mtime are set before its final fsync.
        self.assertIn(("file", expected_metadata), events)
        overwrite = events.index(("replace", self.target / relative))
        synced = {path for kind, path in events[:overwrite] if kind == "directory"}
        saved = rollback / "previous" / relative
        # Every directory entry on the path to the recovery copy is durable
        # before the original is overwritten, including newly created parents.
        for directory in [saved.parent, *saved.parent.parents]:
            if directory == self.root.parent:
                break
            self.assertIn(directory, synced)


class BackslashNameTests(BundleFixture):
    def test_backslash_in_names_round_trips_on_posix(self):
        # Backslash is an ordinary filename character on Linux.
        source = self.write(self.home / "Documents/a\\b.txt", b"report", 0o600)
        self.write(self.home / "Documents/odd\\dir/nested.txt", b"nested")
        (self.home / "Documents/link\\name").symlink_to("a\\b.txt")
        self.backup(include=["Documents"], settings_only=True)
        verify_bundle(self.bundle)
        restore_bundle(self.bundle, self.target, dry_run=False)
        documents = self.target / "Documents"
        self.assertEqual((documents / "a\\b.txt").read_bytes(), b"report")
        self.assertEqual(stat.S_IMODE((documents / "a\\b.txt").stat().st_mode), 0o600)
        self.assertEqual((documents / "odd\\dir/nested.txt").read_bytes(), b"nested")
        self.assertEqual(os.readlink(documents / "link\\name"), "a\\b.txt")
        self.assertEqual(source.read_bytes(), b"report")

    def test_backslash_rejected_where_it_separates_paths(self):
        # verify can run on other systems; there a backslash name could escape.
        self.write(self.home / "Documents/a\\b.txt")
        self.backup(include=["Documents"], settings_only=True)
        with patch.object(bundle_module, "_BACKSLASH_IS_SEPARATOR", True):
            with self.assertRaisesRegex(BundleError, "Unsafe relative path"):
                verify_bundle(self.bundle)

    def test_unsupported_name_fails_before_later_files_are_copied(self):
        self.write(self.home / "Documents/a\\b.txt")
        self.write(self.home / "Documents/z-large.bin", b"x" * 4096)
        copied = []
        copy = bundle_module._copy_regular

        def record(source, *args, **kwargs):
            copied.append(Path(source).name)
            return copy(source, *args, **kwargs)

        with patch.object(bundle_module, "_BACKSLASH_IS_SEPARATOR", True), \
             patch("fedora_migration.bundle._copy_regular", side_effect=record):
            with self.assertRaisesRegex(BundleError, r"Unsafe relative path: 'Documents/a\\\\b\.txt'"):
                self.backup(include=["Documents"], settings_only=True)
        self.assertEqual(copied, [])
        self.assertFalse(os.path.lexists(self.bundle))


if __name__ == "__main__":
    unittest.main()
