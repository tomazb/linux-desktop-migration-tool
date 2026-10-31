"""Rollback tests: real bundles, real restores and real journals; no mocks of file logic."""

import errno
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import unittest
from unittest.mock import patch

from fedora_migration import bundle as bundle_module
from fedora_migration.bundle import BundleError, BundleInterrupted, restore_bundle

from test_bundle import BundleFixture


def rollback_module():
    return importlib.import_module("fedora_migration.rollback")


OLD_HOME = "/home/alice"
NEW_HOME = "/home/bob"
XML_PATH = ".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml"


def snapshot(root):
    """Everything below root that matters to a user: kind, bytes/target, mode, mtime."""
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            result[relative] = ("link", os.readlink(path), info.st_mtime_ns)
        elif stat.S_ISDIR(info.st_mode):
            result[relative] = ("dir",)
        else:
            result[relative] = ("file", path.read_bytes(),
                                stat.S_IMODE(info.st_mode), info.st_mtime_ns)
    return result


def failing_publish(name, error):
    """An os.link stand-in that fails the pinned publication of the saved original at `name`."""
    real = os.link

    def side_effect(src, dst, **kwargs):
        if dst == name and kwargs.get("src_dir_fd") is not None:
            raise error
        return real(src, dst, **kwargs)
    return side_effect


class RollbackFixture(BundleFixture):
    def setUp(self):
        super().setUp()
        self.rollback_dir = self.root / "rollback"

    def restore(self, **kwargs):
        return restore_bundle(self.bundle, self.target, dry_run=False,
                              backup_dir=self.rollback_dir, **kwargs)

    def plan(self, **kwargs):
        return rollback_module().plan_rollback(self.rollback_dir, self.target, **kwargs)

    def apply(self, **kwargs):
        return rollback_module().apply_rollback(self.rollback_dir, self.target, **kwargs)

    def journal(self):
        return json.loads((self.rollback_dir / "journal.json").read_text())

    def undone_dirs(self):
        return sorted(self.rollback_dir.glob("undone-*"))

    def assert_only_recorded_quarantines_remain(self):
        journal = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
        records = journal["retained_quarantines"]
        self.assertEqual(set(self.target.rglob(".fedora-migration-*")),
                         {self.target / Path(item["quarantine"]).parent for item in records})
        for item in records:
            self.assertTrue(os.path.lexists(self.target / item["quarantine"]))
        return records

    def standard_setup(self):
        """Replace a file and a symlink, create new files in a new directory."""
        self.write(self.home / ".bashrc", b"restored bashrc", 0o644)
        self.write(self.home / ".config/xfce4/new.xml", b"restored new", 0o600)
        self.write(self.home / ".config/xfce4/sub/deep.xml", b"restored deep", 0o600)
        (self.home / ".bash_profile").symlink_to("/restored/target")
        self.write(self.target / ".bashrc", b"original bashrc", 0o600)
        os.utime(self.target / ".bashrc", ns=(1600000000000000000, 1600000000123456789))
        (self.target / ".bash_profile").symlink_to("/original/target")
        os.utime(self.target / ".bash_profile", ns=(1600000000000000000, 1600000000987654321),
                 follow_symlinks=False)
        self.write(self.target / "unrelated", b"keep me", 0o644)
        self.backup()


class JournalTests(RollbackFixture):
    def test_journal_records_rewrite_home_pair(self):
        self.write(self.home / XML_PATH, b"x /home/alice")
        self.backup()
        self.restore(rewrite_home=(OLD_HOME, NEW_HOME))
        self.assertEqual(self.journal()["rewrite_home"], [OLD_HOME, NEW_HOME])

    def test_journal_records_null_without_rewrite(self):
        self.write(self.home / ".bashrc")
        self.backup()
        self.restore()
        self.assertIn("rewrite_home", self.journal())
        self.assertIsNone(self.journal()["rewrite_home"])

    def test_identical_rewrite_pair_is_recorded_as_null(self):
        self.write(self.home / ".bashrc")
        self.backup()
        self.restore(rewrite_home=(OLD_HOME, OLD_HOME + "/"))
        self.assertIsNone(self.journal()["rewrite_home"])


class RoundTripTests(RollbackFixture):
    def test_full_round_trip_puts_everything_back(self):
        self.standard_setup()
        before = snapshot(self.target)
        self.restore()
        self.assertNotEqual(snapshot(self.target), before)
        result = self.apply()
        self.assertEqual(snapshot(self.target), before)
        self.assertEqual(sorted(result["put_back"]), [".bash_profile", ".bashrc"])
        self.assertEqual(sorted(result["removed"]),
                         [".config/xfce4/new.xml", ".config/xfce4/sub/deep.xml"])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(
            sorted(result["removed_directories"]),
            [".config", ".config/xfce4", ".config/xfce4/sub"])
        self.assertFalse((self.target / ".config").exists())

    def test_put_back_preserves_bytes_mode_and_mtime(self):
        self.standard_setup()
        self.restore()
        self.apply()
        original = self.target / ".bashrc"
        self.assertEqual(original.read_bytes(), b"original bashrc")
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o600)
        self.assertEqual(original.stat().st_mtime_ns, 1600000000123456789)
        link = self.target / ".bash_profile"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "/original/target")
        self.assertEqual(link.lstat().st_mtime_ns, 1600000000987654321)

    def test_unrelated_files_are_untouched(self):
        self.standard_setup()
        self.restore()
        unrelated = self.target / "unrelated"
        before = (unrelated.read_bytes(), unrelated.stat().st_mtime_ns)
        self.apply()
        self.assertEqual((unrelated.read_bytes(), unrelated.stat().st_mtime_ns), before)

    def test_current_restored_versions_are_saved_before_being_replaced(self):
        self.standard_setup()
        self.restore()
        result = self.apply()
        undone = Path(result["undone_dir"])
        self.assertEqual(self.undone_dirs(), [undone])
        self.assertEqual((undone / ".bashrc").read_bytes(), b"restored bashrc")
        self.assertEqual(os.readlink(undone / ".bash_profile"), "/restored/target")
        self.assertEqual((undone / ".config/xfce4/new.xml").read_bytes(), b"restored new")
        self.assertEqual(stat.S_IMODE(undone.stat().st_mode), 0o700)

    def test_rollback_journal_records_complete_plan(self):
        self.standard_setup()
        self.restore()
        result = self.apply()
        journal = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
        self.assertEqual(journal["status"], "complete")
        self.assertEqual(journal["kind"], "intent_plan")
        self.assertEqual(sorted(journal["put_back"]), [".bash_profile", ".bashrc"])
        self.assertEqual(journal["undone_dir"], result["undone_dir"])
        mode = stat.S_IMODE((self.rollback_dir / "rollback-journal.json").stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.journal()["status"], "complete")  # restore journal untouched

    def test_nothing_to_save_creates_no_undone_directory(self):
        self.write(self.home / ".bashrc", b"restored")
        self.write(self.target / ".bashrc", b"original")
        self.backup()
        self.restore()
        (self.target / ".bashrc").unlink()  # user deleted the restored file
        result = self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"original")
        self.assertIsNone(result["undone_dir"])
        self.assertEqual(self.undone_dirs(), [])

    def test_private_modes_with_permissive_umask(self):
        self.standard_setup()
        self.restore()
        self.umask(0o000)
        result = self.apply()
        self.assertEqual(stat.S_IMODE(Path(result["undone_dir"]).stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.rollback_dir / "rollback-journal.json").stat().st_mode), 0o600)


class PreviewTests(RollbackFixture):
    def test_plan_writes_nothing(self):
        self.standard_setup()
        self.restore()
        everything = snapshot(self.root)
        plan = self.plan()
        self.assertEqual(snapshot(self.root), everything)
        self.assertEqual(sorted(plan["put_back"]), [".bash_profile", ".bashrc"])
        self.assertEqual(sorted(plan["remove"]),
                         [".config/xfce4/new.xml", ".config/xfce4/sub/deep.xml"])
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["unchanged"], [])
        self.assertTrue(plan["verified"])
        self.assertEqual(
            plan["remove_directories"],
            [".config/xfce4/sub", ".config/xfce4", ".config"])
        self.assertFalse((self.rollback_dir / "rollback-journal.json").exists())
        self.assertEqual(self.undone_dirs(), [])


class ConflictTests(RollbackFixture):
    def test_user_edited_restored_file_is_a_conflict_and_untouched(self):
        self.standard_setup()
        self.restore()
        (self.target / ".bashrc").write_bytes(b"edited by user")
        plan = self.plan()
        self.assertEqual([c["path"] for c in plan["conflicts"]], [".bashrc"])
        self.assertEqual(plan["conflicts"][0]["action"], "put_back")
        self.assertNotIn(".bashrc", plan["put_back"])
        result = self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"edited by user")
        self.assertEqual([c["path"] for c in result["conflicts"]], [".bashrc"])
        self.assertNotIn(".bashrc", result["put_back"])
        self.assertEqual((self.target / ".bash_profile").readlink(), Path("/original/target"))

    def test_include_changed_processes_conflict_and_saves_edited_version(self):
        self.standard_setup()
        self.restore()
        (self.target / ".bashrc").write_bytes(b"edited by user")
        result = self.apply(include_changed=True)
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"original bashrc")
        undone = Path(result["undone_dir"])
        self.assertEqual((undone / ".bashrc").read_bytes(), b"edited by user")
        self.assertIn(".bashrc", result["put_back"])
        self.assertEqual([c["path"] for c in result["conflicts"]], [".bashrc"])

    def test_new_file_edited_by_user_is_kept(self):
        self.standard_setup()
        self.restore()
        edited = self.target / ".config/xfce4/new.xml"
        edited.write_bytes(b"my own edits")
        plan = self.plan()
        self.assertEqual([c["path"] for c in plan["conflicts"]], [".config/xfce4/new.xml"])
        self.assertEqual(plan["conflicts"][0]["action"], "remove")
        result = self.apply()
        self.assertEqual(edited.read_bytes(), b"my own edits")
        self.assertIn(".config/xfce4", [d["path"] for d in result["kept_directories"]])
        self.assertTrue((self.target / ".config/xfce4").is_dir())
        self.assertFalse((self.target / ".config/xfce4/sub").exists())

    def test_new_file_edited_by_user_is_removed_with_include_changed_after_saving(self):
        self.standard_setup()
        self.restore()
        edited = self.target / ".config/xfce4/new.xml"
        edited.write_bytes(b"my own edits")
        result = self.apply(include_changed=True)
        self.assertFalse(edited.exists())
        self.assertEqual((Path(result["undone_dir"]) / ".config/xfce4/new.xml").read_bytes(),
                         b"my own edits")

    def test_missing_restored_replaced_file_is_put_back_and_missing_new_is_nothing(self):
        self.standard_setup()
        self.restore()
        (self.target / ".bashrc").unlink()
        (self.target / ".config/xfce4/new.xml").unlink()
        plan = self.plan()
        self.assertIn(".bashrc", plan["put_back"])
        self.assertNotIn(".config/xfce4/new.xml", plan["remove"])
        self.assertEqual(plan["conflicts"], [])

    def test_directory_at_destination_is_never_processed(self):
        self.standard_setup()
        self.restore()
        (self.target / ".bashrc").unlink()
        (self.target / ".bashrc").mkdir()
        (self.target / ".bashrc/inner").write_bytes(b"data")
        plan = self.plan(include_changed=True)
        self.assertEqual([c["path"] for c in plan["conflicts"]], [".bashrc"])
        self.assertTrue(plan["conflicts"][0]["blocked"])
        self.assertNotIn(".bashrc", plan["put_back"])
        self.apply(include_changed=True)
        self.assertEqual((self.target / ".bashrc/inner").read_bytes(), b"data")

    def test_symlink_standing_in_for_a_matching_file_is_a_conflict(self):
        self.standard_setup()
        self.restore()
        decoy = self.root / "decoy"
        decoy.write_bytes(b"restored new")
        path = self.target / ".config/xfce4/new.xml"
        path.unlink()
        path.symlink_to(decoy)
        self.assertEqual([c["path"] for c in self.plan()["conflicts"]], [".config/xfce4/new.xml"])
        self.apply()
        self.assertTrue(path.is_symlink())

    def test_symlink_replaced_by_regular_file_is_a_conflict(self):
        self.standard_setup()
        self.restore()
        link = self.target / ".bash_profile"
        link.unlink()
        link.write_bytes(b"now a file")
        self.assertEqual([c["path"] for c in self.plan()["conflicts"]], [".bash_profile"])


class RewriteHomeRollbackTests(RollbackFixture):
    def populate(self):
        self.write(self.home / XML_PATH, b'<p value="/home/alice/x"/>', 0o600)
        self.write(self.home / ".bashrc", b"export P=/home/alice/bin\n")
        self.write(self.target / XML_PATH, b"destination original", 0o640)
        self.backup()

    def test_rewritten_files_count_as_restored(self):
        self.populate()
        self.restore(rewrite_home=(OLD_HOME, NEW_HOME))
        self.assertEqual((self.target / XML_PATH).read_bytes(), b'<p value="/home/bob/x"/>')
        plan = self.plan()
        self.assertEqual(plan["conflicts"], [])
        self.assertIn(XML_PATH, plan["put_back"])
        self.assertIn(".bashrc", plan["remove"])
        self.apply()
        self.assertEqual((self.target / XML_PATH).read_bytes(), b"destination original")
        self.assertFalse((self.target / ".bashrc").exists())

    def test_without_the_journal_rewrite_the_same_files_are_conflicts(self):
        self.populate()
        self.restore(rewrite_home=(OLD_HOME, NEW_HOME))
        journal = self.journal()
        journal["rewrite_home"] = None
        (self.rollback_dir / "journal.json").write_text(json.dumps(journal))
        self.assertEqual([c["path"] for c in self.plan()["conflicts"]], [XML_PATH])

    def test_older_journal_without_the_key_means_no_rewrite(self):
        self.populate()
        self.restore()
        journal = self.journal()
        del journal["rewrite_home"]
        (self.rollback_dir / "journal.json").write_text(json.dumps(journal))
        self.assertEqual(self.plan()["conflicts"], [])

    def test_ineligible_file_is_not_treated_as_rewritten(self):
        self.populate()
        self.restore(rewrite_home=(OLD_HOME, NEW_HOME))
        (self.target / ".bashrc").write_bytes(b"export P=/home/bob/bin\n")
        self.assertEqual([c["path"] for c in self.plan()["conflicts"]], [".bashrc"])


class BundleAvailabilityTests(RollbackFixture):
    def test_missing_bundle_means_cannot_verify_and_conflicts(self):
        self.standard_setup()
        self.restore()
        moved = self.root / "moved-bundle"
        self.bundle.rename(moved)
        plan = self.plan()
        self.assertFalse(plan["verified"])
        self.assertIsNotNone(plan["bundle_problem"])
        reasons = {c["path"]: c["reason"] for c in plan["conflicts"]}
        self.assertEqual(sorted(reasons), [".bash_profile", ".bashrc",
                                           ".config/xfce4/new.xml",
                                           ".config/xfce4/sub/deep.xml"])
        self.assertTrue(all("cannot verify" in reason.lower() for reason in reasons.values()))
        self.assertEqual(plan["put_back"], [])
        self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"restored bashrc")

    def test_bundle_override_fixes_it(self):
        self.standard_setup()
        self.restore()
        before_restore_files = {".bashrc": b"original bashrc"}
        moved = self.root / "moved-bundle"
        self.bundle.rename(moved)
        plan = self.plan(bundle=moved)
        self.assertTrue(plan["verified"])
        self.assertEqual(plan["conflicts"], [])
        self.apply(bundle=moved)
        for relative, data in before_restore_files.items():
            self.assertEqual((self.target / relative).read_bytes(), data)

    def test_corrupted_bundle_is_cannot_verify(self):
        self.standard_setup()
        self.restore()
        (self.bundle / "payload/.bashrc").write_bytes(b"corrupt")
        plan = self.plan()
        self.assertFalse(plan["verified"])
        self.assertIn("Checksum", plan["bundle_problem"])
        self.assertTrue(plan["conflicts"])

    def test_cannot_verify_missing_paths_are_still_handled(self):
        self.standard_setup()
        self.restore()
        self.bundle.rename(self.root / "moved-bundle")
        (self.target / ".bashrc").unlink()
        (self.target / ".config/xfce4/new.xml").unlink()
        plan = self.plan()
        self.assertIn(".bashrc", plan["put_back"])
        self.assertNotIn(".config/xfce4/new.xml", [c["path"] for c in plan["conflicts"]])

    def test_wrong_bundle_paths_not_in_manifest_cannot_be_verified(self):
        self.standard_setup()
        self.restore()
        other = self.root / "other-bundle"
        other_home = self.root / "other-home"
        self.write(other_home / ".bash_profile", b"x")
        bundle_module.create_bundle(other_home, other, {"version_id": "43"}, {})
        plan = self.plan(bundle=other)
        self.assertTrue(plan["verified"])
        self.assertEqual(len(plan["conflicts"]), 4)


class FailedRestoreTests(RollbackFixture):
    def test_failed_restore_is_rolled_back_and_the_rest_is_unchanged(self):
        self.write(self.home / ".bash_profile", b"restored first")
        self.write(self.home / ".bashrc", b"restored second")
        self.write(self.home / ".gtkrc-2.0", b"restored third")
        self.write(self.target / ".bash_profile", b"original first")
        self.write(self.target / ".bashrc", b"original second")
        self.write(self.target / ".gtkrc-2.0", b"original third")
        self.backup()
        replace = os.replace

        def fail_second(src, dst):
            if Path(dst) == self.target / ".bashrc":
                raise OSError("injected disk failure")
            return replace(src, dst)

        with patch("fedora_migration.bundle.os.replace", side_effect=fail_second):
            with self.assertRaises(BundleError):
                self.restore()
        self.assertEqual(self.journal()["status"], "failed")
        self.assertEqual((self.target / ".bash_profile").read_bytes(), b"restored first")
        plan = self.plan()
        self.assertEqual(plan["put_back"], [".bash_profile"])
        # The second was saved but never replaced; the third was never reached.
        self.assertEqual(sorted(plan["unchanged"]), [".bashrc", ".gtkrc-2.0"])
        self.assertEqual(plan["conflicts"], [])
        self.apply()
        self.assertEqual((self.target / ".bash_profile").read_bytes(), b"original first")
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"original second")
        self.assertEqual((self.target / ".gtkrc-2.0").read_bytes(), b"original third")

    def test_new_paths_not_reached_are_nothing_to_do(self):
        self.write(self.home / ".bash_profile", b"restored a")
        self.write(self.home / ".bashrc", b"restored b")
        self.backup()
        replace = os.replace

        def fail_second(src, dst):
            if Path(dst) == self.target / ".bashrc":
                raise OSError("injected disk failure")
            return replace(src, dst)

        with patch("fedora_migration.bundle.os.replace", side_effect=fail_second):
            with self.assertRaises(BundleError):
                self.restore()
        plan = self.plan()
        self.assertEqual(plan["remove"], [".bash_profile"])
        self.assertEqual(plan["conflicts"], [])
        self.apply()
        self.assertEqual(list(self.target.iterdir()), [])

    def test_interrupted_restore_is_accepted(self):
        self.write(self.home / ".bashrc", b"restored")
        self.write(self.target / ".bashrc", b"original")
        self.backup()
        replace = os.replace

        def interrupt(src, dst):
            if Path(dst) == self.target / ".bashrc":
                raise KeyboardInterrupt()
            return replace(src, dst)

        with patch("fedora_migration.bundle.os.replace", side_effect=interrupt):
            with self.assertRaises(BundleInterrupted):
                self.restore()
        self.assertEqual(self.journal()["status"], "interrupted")
        self.assertEqual(self.plan()["conflicts"], [])


class DirectoryTests(RollbackFixture):
    def test_non_empty_created_directory_is_kept(self):
        self.standard_setup()
        self.restore()
        self.write(self.target / ".config/xfce4/sub/user-added", b"mine")
        plan = self.plan()
        self.assertEqual(plan["remove_directories"], [])
        result = self.apply()
        self.assertEqual((self.target / ".config/xfce4/sub/user-added").read_bytes(), b"mine")
        kept = sorted(d["path"] for d in result["kept_directories"])
        self.assertEqual(kept, [".config", ".config/xfce4", ".config/xfce4/sub"])
        self.assertEqual(result["removed_directories"], [])

    def test_preexisting_directories_are_not_removed(self):
        self.write(self.home / ".config/xfce4/new.xml", b"restored")
        self.write(self.target / ".config/other", b"keep")
        self.backup()
        self.restore()
        self.apply()
        self.assertTrue((self.target / ".config").is_dir())
        self.assertFalse((self.target / ".config/xfce4").exists())


class ValidationTests(RollbackFixture):
    def make(self):
        self.standard_setup()
        self.restore()

    def edit_journal(self, change):
        journal = self.journal()
        change(journal)
        (self.rollback_dir / "journal.json").write_text(json.dumps(journal))

    def assertRefused(self, pattern=None):
        for call in (self.plan, self.apply):
            with self.subTest(call=call.__name__):
                if pattern:
                    with self.assertRaisesRegex(BundleError, pattern):
                        call()
                else:
                    with self.assertRaises(BundleError):
                        call()

    def test_home_mismatch_is_refused(self):
        self.make()
        self.edit_journal(lambda j: j.update(home=str(self.root / "elsewhere")))
        self.assertRefused("home")
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"restored bashrc")

    def test_malformed_journals_are_refused(self):
        self.make()
        original = self.journal()
        cases = {
            "wrong kind": lambda j: j.update(kind="other"),
            "no kind": lambda j: j.pop("kind"),
            "bad status": lambda j: j.update(status="mystery"),
            "replaced not a list": lambda j: j.update(replaced=".bashrc"),
            "new has non-string": lambda j: j.update(new=[1]),
            "dirs missing": lambda j: j.pop("created_directories"),
            "relative home": lambda j: j.update(home="home/x"),
            "non-string bundle": lambda j: j.update(bundle=5),
            "relative bundle": lambda j: j.update(bundle="bundle"),
            "parent traversal": lambda j: j.update(replaced=["../etc/passwd"]),
            "absolute path": lambda j: j.update(new=["/etc/passwd"]),
            "dot segment": lambda j: j["created_directories"].append("a/./b"),
            "duplicate": lambda j: j.update(replaced=[".bashrc", ".bashrc"]),
            "in both lists": lambda j: j.update(new=j["new"] + [".bashrc"]),
            "bad rewrite": lambda j: j.update(rewrite_home=["/home/a"]),
            "relative rewrite": lambda j: j.update(rewrite_home=["home/a", "/home/b"]),
            "not an object": lambda j: (j.clear(), None),
        }
        for name, change in cases.items():
            with self.subTest(name):
                journal = json.loads(json.dumps(original))
                change(journal)
                data = json.dumps(journal if journal else ["list"])
                (self.rollback_dir / "journal.json").write_text(data)
                with self.assertRaises(BundleError):
                    self.plan()
                with self.assertRaises(BundleError):
                    self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"restored bashrc")
        self.assertFalse((self.rollback_dir / "rollback-journal.json").exists())

    def test_unreadable_json_is_refused(self):
        self.make()
        (self.rollback_dir / "journal.json").write_text("{not json")
        self.assertRefused()

    def test_missing_journal_directory_and_symlinks_are_refused(self):
        self.make()
        with self.assertRaises(BundleError):
            rollback_module().plan_rollback(self.root / "nowhere", self.target)
        link = self.root / "link-to-rollback"
        link.symlink_to(self.rollback_dir)
        with self.assertRaises(BundleError):
            rollback_module().plan_rollback(link, self.target)
        (self.rollback_dir / "journal.json").rename(self.root / "saved-journal.json")
        (self.rollback_dir / "journal.json").symlink_to(self.root / "saved-journal.json")
        self.assertRefused()

    def test_second_rollback_is_refused_after_complete(self):
        self.make()
        self.apply()
        with self.assertRaisesRegex(BundleError, "already rolled back"):
            self.plan()
        with self.assertRaisesRegex(BundleError, "already rolled back"):
            self.apply()

    def test_unreadable_rollback_journal_is_refused(self):
        self.make()
        (self.rollback_dir / "rollback-journal.json").write_text("{broken")
        self.assertRefused()

    def test_symlink_ancestor_at_a_destination_is_refused(self):
        self.make()
        elsewhere = self.root / "elsewhere"
        (elsewhere / "xfce4").mkdir(parents=True)
        (elsewhere / "xfce4/new.xml").write_bytes(b"restored new")
        shutil.rmtree(self.target / ".config")
        (self.target / ".config").symlink_to(elsewhere)
        self.assertRefused("symlink")
        self.assertEqual((elsewhere / "xfce4/new.xml").read_bytes(), b"restored new")
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"restored bashrc")

    def test_symlink_ancestor_appearing_at_write_time_is_refused(self):
        self.make()
        elsewhere = self.root / "elsewhere"
        (elsewhere / "xfce4").mkdir(parents=True)
        (elsewhere / "xfce4/new.xml").write_bytes(b"restored new")

        def swap_after_planning():
            shutil.rmtree(self.target / ".config")
            (self.target / ".config").symlink_to(elsewhere)

        with self.assertRaisesRegex(BundleError, "symlink"):
            self.apply(before_write=swap_after_planning)
        self.assertEqual((elsewhere / "xfce4/new.xml").read_bytes(), b"restored new")

    def test_created_directories_inside_the_bundle_are_refused(self):
        (self.home / ".config/xfce4/empty").mkdir(parents=True)
        self.write(self.home / ".config/xfce4/a.xml", b"a")
        self.bundle = self.target / "stored-bundle"
        self.backup()
        self.restore()
        for crafted in ("stored-bundle/payload/.config/xfce4/empty", "stored-bundle/payload",
                        "stored-bundle"):
            with self.subTest(crafted=crafted):
                journal = self.journal()
                journal["created_directories"] = [crafted]
                (self.rollback_dir / "journal.json").write_text(json.dumps(journal))
                self.assertRefused("bundle")
                self.assertTrue((self.bundle / "payload/.config/xfce4/empty").is_dir())
                bundle_module.verify_bundle(self.bundle)

    def test_created_directory_inside_the_rollback_directory_is_refused(self):
        self.rollback_dir = self.target / ".local/state/recovery"
        self.make()
        self.edit_journal(lambda j: j.update(
            created_directories=j["created_directories"] + [".local/state/recovery/previous"]))
        self.assertRefused("rollback directory")

    def test_a_rollback_directory_inside_a_created_directory_is_fine(self):
        self.rollback_dir = self.target / ".local/state/recovery"
        self.write(self.home / ".local/bin/tool", b"tool")
        self.make()
        self.assertIn(".local", self.journal()["created_directories"])
        result = self.apply()
        self.assertEqual(result["status"], "complete")
        self.assertIn(".local", [d["path"] for d in result["kept_directories"]])
        self.assertTrue(self.rollback_dir.is_dir())
        self.assertFalse((self.target / ".local/bin").exists())

    def test_destinations_inside_the_rollback_directory_are_refused(self):
        self.rollback_dir = self.target / ".local/state/recovery"
        self.make()
        self.edit_journal(lambda j: j.update(
            new=j["new"] + [".local/state/recovery/previous/.bashrc"]))
        self.assertRefused("rollback directory")


class MetadataTests(RollbackFixture):
    def directory_setup(self, original_mode=0o700):
        self.write(self.home / ".config/xfce4/a.xml", b"restored")
        self.write(self.target / ".config/xfce4/a.xml", b"original")
        self.write(self.target / ".config/unrelated", b"private unrelated data")
        (self.home / ".config").chmod(0o755)
        (self.target / ".config").chmod(original_mode)
        os.utime(self.home / ".config", ns=(1700000000000000000,) * 2)
        os.utime(self.target / ".config", ns=(1600000000000000000,) * 2)
        self.backup(include=(".config",))
        self.restore()

    def assert_original_directory(self):
        info = (self.target / ".config").stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertEqual(info.st_mtime_ns, 1600000000000000000)
        self.assertEqual((self.target / ".config/unrelated").read_bytes(), b"private unrelated data")

    def test_existing_selected_directory_mode_and_mtime_are_restored(self):
        self.directory_setup()
        self.assertEqual(self.journal().get("original_directories"), [
            {"path": ".config", "mode": 0o700, "mtime_ns": 1600000000000000000}])
        self.apply()
        self.assert_original_directory()

    def test_metadata_only_secret_directory_requires_agent_checks(self):
        (self.home / ".ssh").mkdir(mode=0o755)
        (self.target / ".ssh").mkdir(mode=0o700)
        self.backup(include_secrets=True)
        self.restore()
        self.assertTrue(self.plan()["secrets"])

    def test_user_restricted_directory_permissions_are_a_conflict(self):
        self.directory_setup(original_mode=0o770)
        (self.target / ".config").chmod(0o700)
        result = self.apply()
        self.assertEqual(stat.S_IMODE((self.target / ".config").stat().st_mode), 0o700)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(item["path"] == ".config" for item in result["skipped"]))
        self.assertEqual(self.apply(include_changed=True)["status"], "complete")
        self.assertEqual(stat.S_IMODE((self.target / ".config").stat().st_mode), 0o770)

    def test_user_changed_directory_timestamp_is_a_conflict(self):
        self.directory_setup()
        os.utime(self.target / ".config", ns=(1800000000000000000,) * 2)
        result = self.apply()
        self.assertEqual(result["status"], "partial")
        self.assertEqual((self.target / ".config").stat().st_mtime_ns, 1800000000000000000)
        self.apply(include_changed=True)
        self.assert_original_directory()

    def test_missing_bundle_cannot_authorize_changed_directory_metadata(self):
        self.directory_setup()
        self.bundle.rename(self.root / "moved-bundle")
        result = self.apply()
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(item["path"] == ".config" and "cannot verify" in item["reason"]
                            for item in result["skipped"]))
        self.assertEqual(stat.S_IMODE((self.target / ".config").stat().st_mode), 0o755)

    def test_partial_rollback_restores_metadata_again_when_continued(self):
        self.directory_setup()
        (self.target / ".config/xfce4/a.xml").write_bytes(b"user edit")
        self.assertEqual(self.apply()["status"], "partial")
        self.assert_original_directory()
        self.assertEqual(self.apply(include_changed=True)["status"], "complete")
        self.assert_original_directory()
        self.assertEqual((self.target / ".config/xfce4/a.xml").read_bytes(), b"original")

    def test_older_journal_warns_that_original_directory_metadata_is_unavailable(self):
        self.directory_setup()
        journal = self.journal()
        journal.pop("original_directories", None)
        (self.rollback_dir / "journal.json").write_text(json.dumps(journal))
        plan = self.plan()
        self.assertTrue(any("directory metadata" in warning for warning in plan.get("warnings", [])))
        result = self.apply()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["warnings"], plan["warnings"])
        recorded = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
        self.assertEqual(recorded["warnings"], plan["warnings"])

    def test_invalid_original_directory_metadata_is_refused_before_writes(self):
        self.directory_setup()
        original = self.journal()
        invalid = [
            None, {}, [".config"],
            [{"path": "../outside", "mode": 0o700, "mtime_ns": 0}],
            [{"path": ".config", "mode": True, "mtime_ns": 0}],
            [{"path": ".config", "mode": 0o10000, "mtime_ns": 0}],
            [{"path": ".config", "mode": 0o700, "mtime_ns": 2**63}],
            [{"path": ".config", "mode": 0o700, "mtime_ns": False}],
            [{"path": ".config", "mode": 0o700, "mtime_ns": 0}] * 2,
            [{"path": ".config/xfce4/a.xml", "mode": 0o700, "mtime_ns": 0}],
        ]
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                journal = dict(original, original_directories=metadata)
                (self.rollback_dir / "journal.json").write_text(json.dumps(journal))
                before = snapshot(self.target)
                with self.assertRaises(BundleError):
                    self.plan()
                with self.assertRaises(BundleError):
                    self.apply()
                self.assertEqual(snapshot(self.target), before)
                self.assertFalse((self.rollback_dir / "rollback-journal.json").exists())

    def test_original_directory_symlink_is_refused_before_writes(self):
        self.directory_setup()
        outside = self.root / "outside"
        (self.target / ".config").rename(outside)
        (self.target / ".config").symlink_to(outside, target_is_directory=True)
        before = (outside.stat().st_mode, outside.stat().st_mtime_ns, snapshot(outside))
        with self.assertRaises(BundleError):
            self.apply()
        self.assertEqual((outside.stat().st_mode, outside.stat().st_mtime_ns, snapshot(outside)), before)

    def test_interrupted_directory_metadata_restore_can_be_retried(self):
        self.directory_setup()
        real = os.fchmod

        def interrupt_directory(descriptor, mode):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise KeyboardInterrupt()
            return real(descriptor, mode)

        with patch("fedora_migration.rollback.os.fchmod", side_effect=interrupt_directory):
            with self.assertRaises(BundleInterrupted):
                self.apply()
        self.assertEqual(self.apply()["status"], "complete")
        self.assert_original_directory()

    def readonly_setup(self):
        self.write(self.home / ".config/a.xml", b"restored")
        self.write(self.target / ".config/a.xml", b"original")
        (self.home / ".config").chmod(0o500)
        (self.target / ".config").chmod(0o700)
        self.backup(include=(".config",))
        self.restore()

    def test_readonly_selected_directory_allows_rollback_file_writes(self):
        self.readonly_setup()
        module = rollback_module()
        real = module._stage_saved

        def require_owner_write(saved, descriptor):
            # Root bypasses permission checks in CI; enforce the normal user's
            # prerequisite at the actual pinned directory used to create files.
            if stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o700 != 0o700:
                raise PermissionError("parent does not grant owner write access")
            return real(saved, descriptor)

        with patch.object(module, "_stage_saved", side_effect=require_owner_write):
            self.apply()
        self.assertEqual((self.target / ".config/a.xml").read_bytes(), b"original")
        self.assertEqual(stat.S_IMODE((self.target / ".config").stat().st_mode), 0o700)

    def test_failed_rollback_reverts_temporary_directory_access(self):
        self.readonly_setup()
        fail = failing_publish("a.xml", OSError("injected failure"))
        with patch("fedora_migration.rollback.os.link", side_effect=fail):
            with self.assertRaises(BundleError):
                self.apply()
        self.assertEqual(stat.S_IMODE((self.target / ".config").stat().st_mode), 0o500)
        self.apply(include_changed=True)
        self.assertEqual(stat.S_IMODE((self.target / ".config").stat().st_mode), 0o700)

    def test_deleted_original_empty_directory_is_recreated_with_original_metadata(self):
        (self.home / ".config").mkdir()
        (self.target / ".config").mkdir(mode=0o700)
        os.utime(self.target / ".config", ns=(1600000000000000000,) * 2)
        self.backup(include=(".config",))
        self.restore()
        (self.target / ".config").rmdir()
        self.apply()
        info = (self.target / ".config").stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertEqual(info.st_mtime_ns, 1600000000000000000)

    def test_original_directory_access_stays_granted_until_final_metadata_is_applied(self):
        self.readonly_setup()
        module = rollback_module()
        real_open = module._open_directory

        def require_read_permission(home, relative, **kwargs):
            path = home / relative
            if path.exists() and not stat.S_IMODE(path.stat().st_mode) & 0o400:
                raise PermissionError("normal user cannot reopen unreadable directory")
            return real_open(home, relative, **kwargs)

        def revoke_all_after_open(home, paths, changes):
            # Model a mode000 restored root already pinned by the preparer;
            # a later reopen must occur only after owner access is granted.
            info = (home / ".config").stat()
            changes.append((".config", 0, (info.st_dev, info.st_ino)))
            (home / ".config").chmod(0o700)

        with patch.object(module, "_prepare_directory_access", side_effect=revoke_all_after_open), \
             patch.object(module, "_open_directory", side_effect=require_read_permission):
            self.apply()
        self.assertEqual(stat.S_IMODE((self.target / ".config").stat().st_mode), 0o700)

    def test_same_bytes_but_different_mode_and_mtime_is_put_back(self):
        self.write(self.home / ".bashrc", b"same bytes", 0o644)
        os.utime(self.home / ".bashrc", ns=(1700000000000000000, 1700000000000000000))
        self.write(self.target / ".bashrc", b"same bytes", 0o600)
        os.utime(self.target / ".bashrc", ns=(1600000000000000000, 1600000000000000000))
        self.backup()
        self.restore()
        plan = self.plan()
        self.assertEqual(plan["unchanged"], [])
        self.assertEqual(plan["put_back"], [".bashrc"])
        result = self.apply()
        self.assertEqual(result["status"], "complete")
        original = self.target / ".bashrc"
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o600)
        self.assertEqual(original.stat().st_mtime_ns, 1600000000000000000)
        self.assertEqual(
            stat.S_IMODE((Path(result["undone_dir"]) / ".bashrc").stat().st_mode), 0o644)

    def test_same_symlink_target_but_different_mtime_is_put_back(self):
        (self.home / ".bash_profile").symlink_to("/same")
        os.utime(self.home / ".bash_profile", ns=(1700000000000000000, 1700000000000000000),
                 follow_symlinks=False)
        (self.target / ".bash_profile").symlink_to("/same")
        os.utime(self.target / ".bash_profile", ns=(1600000000000000000, 1600000000000000000),
                 follow_symlinks=False)
        self.backup()
        self.restore()
        self.assertEqual(self.plan()["put_back"], [".bash_profile"])
        self.apply()
        self.assertEqual((self.target / ".bash_profile").lstat().st_mtime_ns,
                         1600000000000000000)


class PinnedWriteTests(RollbackFixture):
    def outside(self):
        outside = self.root / "outside"
        (outside / "xfce4/sub").mkdir(parents=True)
        (outside / "xfce4/new.xml").write_bytes(b"OUTSIDE-ONLY USER DATA")
        (outside / "xfce4/sub/deep.xml").write_bytes(b"OUTSIDE-ONLY DEEP")
        return outside

    def swap_config(self, outside):
        (self.target / ".config").rename(self.target / ".config-original")
        (self.target / ".config").symlink_to(outside, target_is_directory=True)

    def assertOutsideIntact(self, outside):
        self.assertEqual((outside / "xfce4/new.xml").read_bytes(), b"OUTSIDE-ONLY USER DATA")
        self.assertEqual((outside / "xfce4/sub/deep.xml").read_bytes(), b"OUTSIDE-ONLY DEEP")

    def test_parent_swapped_for_a_symlink_while_pinning_is_refused_cleanly(self):
        self.standard_setup()
        self.restore()
        outside = self.outside()
        module = rollback_module()
        real = module._open_parent

        def open_then_swap(home, relative, **kwargs):
            descriptor = real(home, relative, **kwargs)
            if relative == ".config/xfce4/new.xml":
                self.swap_config(outside)
            return descriptor

        with patch.object(module, "_open_parent", side_effect=open_then_swap):
            with self.assertRaises(BundleError):
                self.apply()
        self.assertOutsideIntact(outside)
        journal = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
        self.assertEqual(journal["status"], "failed")

    def test_parent_swapped_after_pinning_never_touches_the_outside(self):
        self.standard_setup()
        self.restore()
        outside = self.outside()
        module = rollback_module()
        real = module._pin

        def pin_then_swap(home, relative, **kwargs):
            descriptor = real(home, relative, **kwargs)
            if relative == ".config/xfce4/new.xml":
                self.swap_config(outside)
            return descriptor

        with patch.object(module, "_pin", side_effect=pin_then_swap):
            try:
                self.apply()
            except BundleError:
                pass
        self.assertOutsideIntact(outside)
        # The exact entry that was pinned is the one that was captured.
        self.assertEqual(
            (self.undone_dirs()[0] / ".config/xfce4/new.xml").read_bytes(), b"restored new")

    def test_edit_after_staging_and_before_capture_is_saved_not_lost(self):
        self.standard_setup()
        self.restore()
        module = rollback_module()
        real = module._stage_saved

        def stage_then_edit(saved, descriptor):
            name = real(saved, descriptor)
            (self.target / ".bashrc").write_bytes(b"LATEST USER EDIT")
            return name

        with patch.object(module, "_stage_saved", side_effect=stage_then_edit):
            result = self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"original bashrc")
        self.assertEqual((Path(result["undone_dir"]) / ".bashrc").read_bytes(),
                         b"LATEST USER EDIT")

    def test_edit_between_plan_and_write_is_saved(self):
        self.standard_setup()
        self.restore()
        result = self.apply(before_write=lambda: (self.target / ".bashrc").write_bytes(
            b"edited after planning"))
        self.assertEqual((Path(result["undone_dir"]) / ".bashrc").read_bytes(),
                         b"edited after planning")
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"original bashrc")

    def exdev(self, source, destination, *args, **kwargs):
        if kwargs.get("src_dir_fd") is not None and kwargs.get("dst_dir_fd") is None:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return self.real_rename(source, destination, *args, **kwargs)

    def assert_open_descriptor_edit_is_recoverable(self, relative):
        self.standard_setup()
        self.restore()
        self.real_rename = os.rename
        module = rollback_module()
        real_copy = module._copy_entry_out
        descriptor = os.open(self.target / relative, os.O_WRONLY)

        def edit_through_descriptor(data):
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, data)
            os.fsync(descriptor)

        def copy_then_edit(source_fd, leaf, destination, staging):
            real_copy(source_fd, leaf, destination, staging)
            if destination.name == Path(relative).name:
                edit_through_descriptor(b"LATE EDIT AFTER COPY")

        try:
            with patch("fedora_migration.rollback.os.rename", side_effect=self.exdev), \
                 patch.object(module, "_copy_entry_out", side_effect=copy_then_edit):
                result = self.apply()
            journal = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
            records = [item for item in journal.get("retained_quarantines", [])
                       if item["path"] == relative]
            self.assertEqual(len(records), 1, "the captured inode needs a journaled recovery location")
            retained = self.target / records[0]["quarantine"]
            self.assertEqual(retained.read_bytes(), b"LATE EDIT AFTER COPY")
            self.assertEqual(retained.stat().st_ino, os.fstat(descriptor).st_ino)
            # A final fingerprint check is insufficient: writes after rollback
            # returns must also remain recoverable through the retained inode.
            edit_through_descriptor(b"LATE EDIT AFTER COMPLETE")
            self.assertEqual(retained.read_bytes(), b"LATE EDIT AFTER COMPLETE")
            self.assertEqual(result["retained_quarantines"], journal["retained_quarantines"])
        finally:
            os.close(descriptor)

    def test_cross_device_put_back_retains_open_descriptor_edits(self):
        self.assert_open_descriptor_edit_is_recoverable(".bashrc")

    def test_cross_device_remove_retains_open_descriptor_edits(self):
        self.assert_open_descriptor_edit_is_recoverable(".config/xfce4/new.xml")

    def test_cross_device_failed_rollback_retry_keeps_prior_recovery_locations(self):
        self.standard_setup()
        self.restore()
        self.real_rename = os.rename
        real_link = os.link

        def fail_final(source, destination, **kwargs):
            if destination == ".bashrc" and str(source).startswith(".fedora-migration-"):
                raise OSError("injected failure")
            return real_link(source, destination, **kwargs)
        with patch("fedora_migration.rollback.os.rename", side_effect=self.exdev), \
             patch("fedora_migration.rollback.os.link", side_effect=fail_final):
            with self.assertRaises(BundleError):
                self.apply()
        previous = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
        records = previous.get("retained_quarantines", [])
        self.assertTrue(records)
        self.assertTrue(all(item["path"] != ".bashrc" for item in records))
        with patch("fedora_migration.rollback.os.rename", side_effect=self.exdev):
            result = self.apply()
        for item in records:
            self.assertIn(item, result["retained_quarantines"])
            self.assertTrue(os.path.lexists(self.target / item["quarantine"]))

    def test_cross_device_capture_refuses_to_move_data_if_recovery_journal_write_fails(self):
        self.standard_setup()
        self.restore()
        self.real_rename = os.rename
        module = rollback_module()
        real_update = module._update_journal

        def fail_quarantine_record(directory, journal, name):
            if journal.get("retained_quarantines"):
                raise OSError("injected journal write failure")
            return real_update(directory, journal, name)

        before = snapshot(self.target)
        with patch("fedora_migration.rollback.os.rename", side_effect=self.exdev), \
             patch.object(module, "_update_journal", side_effect=fail_quarantine_record):
            with self.assertRaisesRegex(BundleError, "journal write failure"):
                self.apply()
        self.assertEqual(snapshot(self.target), before)

    def test_cross_device_recovery_does_not_follow_a_substituted_quarantine_symlink(self):
        self.standard_setup()
        self.restore()
        self.real_rename = os.rename
        module = rollback_module()
        real_capture, real_link = module._capture, os.link
        outside = self.root / "outside"
        self.write(outside / "entry", b"outside data")
        retained = self.root / "moved-quarantine"

        def capture_then_swap(descriptor, leaf, *args, **kwargs):
            result = real_capture(descriptor, leaf, *args, **kwargs)
            if leaf == ".bashrc":
                journal = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
                record = next(item for item in journal["retained_quarantines"]
                              if item["path"] == ".bashrc")
                directory = (self.target / record["quarantine"]).parent
                directory.rename(retained)
                directory.symlink_to(outside, target_is_directory=True)
            return result

        def fail_publication(source, destination, **kwargs):
            if destination == ".bashrc" and str(source).startswith(".fedora-migration-"):
                raise OSError("injected publication failure")
            return real_link(source, destination, **kwargs)

        with patch("fedora_migration.rollback.os.rename", side_effect=self.exdev), \
             patch.object(module, "_capture", side_effect=capture_then_swap), \
             patch("fedora_migration.rollback.os.link", side_effect=fail_publication):
            with self.assertRaises(BundleError):
                self.apply()
        self.assertEqual((outside / "entry").read_bytes(), b"outside data")
        self.assertEqual((retained / "entry").read_bytes(), b"restored bashrc")

    def test_unsafe_retained_quarantine_reference_is_refused_before_writes(self):
        self.standard_setup()
        self.restore()
        path = self.rollback_dir / "rollback-journal.json"
        path.write_text(json.dumps({"status": "partial", "home": str(self.target),
                                   "retained_quarantines": [
                                       {"path": ".bashrc", "quarantine": "../outside/entry"}]}))
        before = snapshot(self.target)
        with self.assertRaises(BundleError):
            self.apply()
        self.assertEqual(snapshot(self.target), before)

    def test_cross_device_capture_falls_back_to_a_verified_copy(self):
        self.standard_setup()
        before = snapshot(self.target)
        self.restore()
        self.real_rename = os.rename
        with patch("fedora_migration.rollback.os.rename", side_effect=self.exdev) as renamed:
            result = self.apply()
        self.assertTrue(renamed.called)
        after = snapshot(self.target)
        for path, entry in before.items():
            self.assertEqual(after[path], entry)
        records = self.assert_only_recorded_quarantines_remain()
        self.assertEqual({path for path, entry in after.items() if entry[0] != "dir"},
                         set(before) | {item["quarantine"] for item in records})
        undone = Path(result["undone_dir"])
        self.assertEqual((undone / ".bashrc").read_bytes(), b"restored bashrc")
        self.assertEqual(os.readlink(undone / ".bash_profile"), "/restored/target")
        self.assertEqual((undone / ".config/xfce4/new.xml").read_bytes(), b"restored new")
        self.assertEqual(list(self.rollback_dir.glob("*staging*")), [])

    def test_cross_device_edit_right_after_the_copy_is_not_lost(self):
        self.standard_setup()
        self.restore()
        self.real_rename = os.rename
        module = rollback_module()
        real = module._copy_entry_out

        def copy_then_write(descriptor, leaf, *args, **kwargs):
            real(descriptor, leaf, *args, **kwargs)
            if (self.target / ".bashrc").exists() or leaf != "entry":
                return
            (self.target / ".bashrc").write_bytes(b"WRITTEN RIGHT AFTER THE COPY")

        with patch("fedora_migration.rollback.os.rename", side_effect=self.exdev), \
             patch.object(module, "_copy_entry_out", side_effect=copy_then_write):
            with self.assertRaisesRegex(BundleError, "created by someone else"):
                self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"WRITTEN RIGHT AFTER THE COPY")
        self.assertEqual(list(self.undone_dirs()[0].glob(".bashrc"))[0].read_bytes(),
                         b"restored bashrc")
        self.assert_only_recorded_quarantines_remain()
        journal = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
        self.assertEqual(journal["status"], "failed")

    def test_cross_device_removal_never_deletes_an_entry_written_after_the_copy(self):
        self.standard_setup()
        self.restore()
        self.real_rename = os.rename
        module = rollback_module()
        real = module._copy_entry_out
        new_xml = self.target / ".config/xfce4/new.xml"

        def copy_then_write(descriptor, leaf, destination, *args, **kwargs):
            real(descriptor, leaf, destination, *args, **kwargs)
            if destination.name == "new.xml":
                new_xml.write_bytes(b"WRITTEN RIGHT AFTER THE COPY")

        with patch("fedora_migration.rollback.os.rename", side_effect=self.exdev), \
             patch.object(module, "_copy_entry_out", side_effect=copy_then_write):
            self.apply()
        self.assertEqual(new_xml.read_bytes(), b"WRITTEN RIGHT AFTER THE COPY")
        self.assertEqual((self.undone_dirs()[0] / ".config/xfce4/new.xml").read_bytes(),
                         b"restored new")
        self.assert_only_recorded_quarantines_remain()

    def test_cross_device_copy_failure_puts_the_entry_back(self):
        self.standard_setup()
        self.restore()
        self.real_rename = os.rename
        module = rollback_module()
        real = module._copy_entry_out

        def fail_for_bashrc(descriptor, leaf, destination, *args, **kwargs):
            if destination.name == ".bashrc":
                raise OSError("injected copy failure")
            return real(descriptor, leaf, destination, *args, **kwargs)

        with patch("fedora_migration.rollback.os.rename", side_effect=self.exdev), \
             patch.object(module, "_copy_entry_out", side_effect=fail_for_bashrc):
            with self.assertRaisesRegex(BundleError, "injected copy failure"):
                self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"restored bashrc")
        self.assert_only_recorded_quarantines_remain()

    def test_failed_final_replace_puts_the_captured_entry_back(self):
        self.standard_setup()
        self.restore()
        fail_final = failing_publish(".bashrc", OSError("injected disk failure"))
        with patch("fedora_migration.rollback.os.link", side_effect=fail_final):
            with self.assertRaises(BundleError):
                self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"restored bashrc")
        self.assertEqual(list(self.target.glob(".fedora-migration-*")), [])


class NoReplaceTests(RollbackFixture):
    def test_entry_created_after_the_capture_is_never_overwritten(self):
        self.standard_setup()
        self.restore()
        module = rollback_module()
        real = module._capture

        def capture_then_create(descriptor, leaf, *args, **kwargs):
            result = real(descriptor, leaf, *args, **kwargs)
            if leaf == ".bashrc":
                (self.target / ".bashrc").write_bytes(b"CREATED AFTER THE CAPTURE")
            return result

        with patch.object(module, "_capture", side_effect=capture_then_create):
            with self.assertRaisesRegex(BundleError, "created by someone else") as caught:
                self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"CREATED AFTER THE CAPTURE")
        undone = self.undone_dirs()[0]
        self.assertEqual((undone / ".bashrc").read_bytes(), b"restored bashrc")
        self.assertIn(str(undone / ".bashrc"), str(caught.exception))
        self.assertIn(str(self.target / ".bashrc"), str(caught.exception))
        self.assertEqual(list(self.target.glob(".fedora-migration-*")), [])
        journal = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
        self.assertEqual(journal["status"], "failed")

    def test_recovery_never_overwrites_an_entry_created_meanwhile(self):
        self.standard_setup()
        self.restore()
        real = os.link

        def create_then_fail(src, dst, **kwargs):
            if dst == ".bashrc" and kwargs.get("src_dir_fd") is not None:
                (self.target / ".bashrc").write_bytes(b"CREATED MEANWHILE")
                raise OSError("injected publication failure")
            return real(src, dst, **kwargs)

        with patch("fedora_migration.rollback.os.link", side_effect=create_then_fail):
            with self.assertRaises(BundleError) as caught:
                self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"CREATED MEANWHILE")
        self.assertEqual((self.undone_dirs()[0] / ".bashrc").read_bytes(), b"restored bashrc")
        self.assertIn("injected publication failure", str(caught.exception))
        self.assertIn(str(self.undone_dirs()[0] / ".bashrc"), str(caught.exception))

    def test_publication_links_symlinks_themselves(self):
        self.standard_setup()
        self.restore()
        self.apply()
        link = self.target / ".bash_profile"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "/original/target")

    def test_filesystem_without_hard_links_refuses_the_path_untouched(self):
        self.standard_setup()
        self.restore()
        real = os.link

        def no_links(src, dst, **kwargs):
            if kwargs.get("src_dir_fd") is not None:
                raise OSError(errno.EPERM, "Operation not permitted")
            return real(src, dst, **kwargs)

        with patch("fedora_migration.rollback.os.link", side_effect=no_links):
            with self.assertRaisesRegex(BundleError, "hard-link"):
                self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"restored bashrc")
        self.assertEqual((self.target / ".bash_profile").readlink(), Path("/restored/target"))
        self.assertEqual(list(self.target.glob(".fedora-migration-*")), [])
        self.assertEqual(list(self.undone_dirs()[0].rglob("*.*")), [])

    def test_a_replacing_rename_is_never_used_to_publish(self):
        self.standard_setup()
        self.restore()
        real = os.replace

        def forbid(src, dst, **kwargs):
            if kwargs.get("dst_dir_fd") is not None:
                raise AssertionError("pinned publication must not use a replacing rename")
            return real(src, dst, **kwargs)

        with patch("fedora_migration.rollback.os.replace", side_effect=forbid):
            self.apply()
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"original bashrc")


class PinnedHomeTests(RollbackFixture):
    def test_home_ancestor_swapped_after_planning_is_refused(self):
        outer = self.root / "outer"
        outer.mkdir()
        self.target = outer / "home"
        self.target.mkdir()
        self.standard_setup()
        self.restore()

        module = rollback_module()
        real = module._assert_no_symlink_ancestors
        armed = []

        def check_then_swap(path, **kwargs):
            real(path, **kwargs)
            if armed and not (self.root / "outer-moved").exists():
                # The lexical check has just passed; now redirect an ancestor of home.
                outer.rename(self.root / "outer-moved")
                outer.symlink_to(self.root / "outer-moved", target_is_directory=True)

        with patch.object(module, "_assert_no_symlink_ancestors", side_effect=check_then_swap):
            with self.assertRaisesRegex(BundleError, "symlink"):
                self.apply(before_write=lambda: armed.append(True))
        moved = self.root / "outer-moved/home"
        self.assertEqual((moved / ".bashrc").read_bytes(), b"restored bashrc")
        # Nothing was written through the redirected ancestor, not even the first path.
        self.assertEqual(os.readlink(moved / ".bash_profile"), "/restored/target")

    def test_home_with_a_symlink_component_is_refused_by_the_walk(self):
        real = self.root / "real-home"
        real.mkdir()
        link = self.root / "link-home"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(BundleError, "symlink"):
            rollback_module()._open_parent(link, ".bashrc")

    def test_walk_starts_at_the_filesystem_root(self):
        opened = []
        real = os.open

        def record(path, flags, *args, **kwargs):
            opened.append((os.fspath(path), kwargs.get("dir_fd")))
            return real(path, flags, *args, **kwargs)

        with patch("fedora_migration.rollback.os.open", side_effect=record):
            descriptor = rollback_module()._open_parent(self.target, ".config/xfce4/x", create=True)
        os.close(descriptor)
        self.assertEqual(opened[0], ("/", None))
        self.assertTrue(all(fd is not None for _, fd in opened[1:]))

    def test_interrupt_while_walking_leaks_no_descriptors(self):
        if not Path("/proc/self/fd").is_dir():
            self.skipTest("descriptor inventory requires /proc/self/fd")
        (self.target / ".config/xfce4").mkdir(parents=True)
        real_close = os.close
        state = {"calls": 0}

        def interrupt_after_close(descriptor):
            real_close(descriptor)
            state["calls"] += 1
            if state["calls"] == 1:
                raise KeyboardInterrupt()

        before = len(os.listdir("/proc/self/fd"))
        with patch("fedora_migration.rollback.os.close", side_effect=interrupt_after_close):
            with self.assertRaises(KeyboardInterrupt):
                rollback_module()._open_parent(self.target, ".config/xfce4/x")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)


class AliasTests(RollbackFixture):
    def test_bundle_alias_cannot_hide_a_created_directory_inside_the_bundle(self):
        (self.home / ".config/xfce4/empty").mkdir(parents=True)
        self.write(self.home / ".config/xfce4/a.xml", b"a")
        self.bundle = self.target / "stored-bundle"
        self.backup()
        self.restore()
        alias = self.root / "alias"
        alias.symlink_to(self.bundle, target_is_directory=True)
        journal = self.journal()
        journal["bundle"] = str(alias)
        journal["created_directories"] = ["stored-bundle/payload/.config/xfce4/empty"]
        (self.rollback_dir / "journal.json").write_text(json.dumps(journal))
        for call in (self.plan, self.apply):
            with self.subTest(call=call.__name__):
                with self.assertRaisesRegex(BundleError, "bundle"):
                    call()
        self.assertTrue((self.bundle / "payload/.config/xfce4/empty").is_dir())
        bundle_module.verify_bundle(self.bundle)

    def test_bundle_alias_cannot_hide_a_replaced_path_inside_the_bundle(self):
        self.write(self.home / ".bashrc", b"restored")
        self.bundle = self.target / "stored-bundle"
        self.backup()
        self.restore()
        alias = self.root / "alias"
        alias.symlink_to(self.bundle, target_is_directory=True)
        journal = self.journal()
        journal["bundle"] = str(alias)
        journal["new"] = ["stored-bundle/payload/.bashrc"]
        (self.rollback_dir / "journal.json").write_text(json.dumps(journal))
        with self.assertRaisesRegex(BundleError, "bundle"):
            self.plan()
        self.assertTrue((self.bundle / "payload/.bashrc").is_file())

    def test_rollback_directory_alias_is_also_canonicalised(self):
        self.rollback_dir = self.target / ".local/state/recovery"
        self.write(self.home / ".config/xfce4/a.xml", b"a")
        self.backup()
        self.restore()
        alias = self.root / "rollback-alias"
        alias.symlink_to(self.target / ".local", target_is_directory=True)
        journal = self.journal()
        journal["new"] = journal["new"] + [".local/state/recovery/previous"]
        (self.rollback_dir / "journal.json").write_text(json.dumps(journal))
        with self.assertRaisesRegex(BundleError, "rollback directory"):
            self.plan()


class FailureDuringRollbackTests(RollbackFixture):
    def test_failure_leaves_failed_journal_undone_copy_and_no_partial_files(self):
        self.standard_setup()
        self.restore()
        destination = self.target / ".bashrc"
        fail = failing_publish(".bashrc", OSError("injected disk failure"))
        with patch("fedora_migration.rollback.os.link", side_effect=fail):
            with self.assertRaises(BundleError) as caught:
                self.apply()
        journal = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
        self.assertEqual(journal["status"], "failed")
        self.assertIn("injected disk failure", journal["error"])
        undone = self.undone_dirs()
        self.assertEqual(len(undone), 1)
        self.assertIn(str(undone[0]), str(caught.exception))
        self.assertIn(str(undone[0]), journal["undone_dir"])
        # The symlink put back before the failure was saved completely; the file
        # being replaced when it failed was returned to its place.
        self.assertEqual(os.readlink(undone[0] / ".bash_profile"), "/restored/target")
        self.assertEqual(destination.read_bytes(), b"restored bashrc")
        self.assertEqual(list(self.target.glob(".fedora-migration-*")), [])
        self.assertEqual(list(self.rollback_dir.glob("*staging*")), [])
        partial = [p for p in undone[0].rglob("*") if p.is_file() and p.stat().st_size == 0]
        self.assertEqual(partial, [])

    def test_a_failed_rollback_can_be_retried_and_completes(self):
        self.standard_setup()
        before = snapshot(self.target)
        self.restore()
        fail = failing_publish(".bashrc", OSError("injected disk failure"))
        with patch("fedora_migration.rollback.os.link", side_effect=fail):
            with self.assertRaises(BundleError):
                self.apply()
        self.apply()
        self.assertEqual(snapshot(self.target), before)
        status = json.loads((self.rollback_dir / "rollback-journal.json").read_text())["status"]
        self.assertEqual(status, "complete")

    def test_interrupt_is_reported_as_interrupted(self):
        self.standard_setup()
        self.restore()
        fail = failing_publish(".bashrc", KeyboardInterrupt())
        with patch("fedora_migration.rollback.os.link", side_effect=fail):
            with self.assertRaises(BundleInterrupted) as caught:
                self.apply()
        self.assertIn(str(self.undone_dirs()[0]), str(caught.exception))
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"restored bashrc")
        journal = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
        self.assertEqual(journal["status"], "interrupted")

    def test_failing_before_write_hook_prevents_all_writes(self):
        self.standard_setup()
        self.restore()
        everything = snapshot(self.root)

        def refuse():
            raise BundleError("guard says no")

        with self.assertRaisesRegex(BundleError, "guard says no"):
            self.apply(before_write=refuse)
        self.assertEqual(snapshot(self.root), everything)

    def test_before_write_runs_after_validation_and_before_the_first_write(self):
        self.standard_setup()
        self.restore()
        seen = []
        self.apply(before_write=lambda: seen.append(
            (self.rollback_dir / "rollback-journal.json").exists()))
        self.assertEqual(seen, [False])

    def test_partial_rollback_with_skipped_conflicts_can_be_continued(self):
        self.standard_setup()
        self.restore()
        (self.target / ".bashrc").write_bytes(b"edited")
        first = self.apply()
        self.assertEqual([c["path"] for c in first["conflicts"]], [".bashrc"])
        journal = json.loads((self.rollback_dir / "rollback-journal.json").read_text())
        self.assertEqual(journal["status"], "partial")
        plan = self.plan(include_changed=True)
        self.assertEqual(plan["put_back"], [".bashrc"])
        self.assertIn(".bash_profile", plan["unchanged"])
        self.apply(include_changed=True)
        self.assertEqual((self.target / ".bashrc").read_bytes(), b"original bashrc")


if __name__ == "__main__":
    unittest.main()
