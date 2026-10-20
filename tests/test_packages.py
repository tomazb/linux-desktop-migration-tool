import copy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fedora_migration import packages


def manifest():
    return {
        "rpm": {"status": "captured", "requests": ["gcc-c++.x86_64", "lib_example.i686"],
                "inventory": [{"spec": "gcc-c++.x86_64", "reason": "User", "from_repo": "fedora",
                               "full_nevra": "gcc-c++-0:15.2-1.fc43.x86_64"}]},
        "flatpak": {"status": "captured", "apps": [
            {"scope": "user", "origin": "flathub", "ref": "app/org.example.First/x86_64/stable"},
            {"scope": "system", "origin": "work", "ref": "org.example.Second/x86_64/stable"}],
            "remotes": [{"scope": "user", "name": "flathub", "url": "https://example.org/repo/"},
                        {"scope": "system", "name": "work", "url": "https://work.example/repo/"}]},
        "warnings": [],
    }


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.installations = Path(self.temp.name) / "installations.d"
        self.installations.mkdir()
        self.path_patch = patch.object(packages, "INSTALLATIONS_DIR", self.installations)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def capture_output(self, argv, **kwargs):
        if argv[0] == "dnf5":
            if "--userinstalled" in argv:
                return "gcc-c++.x86_64\nlib_example.i686\ngcc-c++.x86_64\n"
            return "gcc-c++.x86_64\tUser\tfedora\tgcc-c++-0:15.2-1.fc43.x86_64\n"
        scope = "user" if "--user" in argv else "system"
        if argv[1] == "list":
            return ("app/org.example.First/x86_64/stable\tflathub\n" if scope == "user"
                    else "org.example.Second/x86_64/stable\twork\n")
        return "flathub\thttps://example.org/repo/\n" if scope == "user" else "work\thttps://work.example/repo/\n"

    def test_collect_rpm_and_first_flatpak_in_each_scope(self):
        with patch.object(packages, "_run", side_effect=self.capture_output) as run:
            data = packages.collect_packages()
        self.assertEqual(data["rpm"], manifest()["rpm"])
        self.assertEqual(data["flatpak"], manifest()["flatpak"])
        self.assertTrue(data["warnings"])
        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(["dnf5", "repoquery", "--installed", "--userinstalled", "--queryformat", "%{name}.%{arch}\n"], calls)
        self.assertIn(["dnf5", "repoquery", "--installed", "--queryformat", "%{name}.%{arch}\t%{reason}\t%{from_repo}\t%{full_nevra}\n"], calls)
        self.assertIn(["flatpak", "list", "--user", "--app", "--columns=ref,origin"], calls)
        self.assertIn(["flatpak", "remotes", "--system", "--show-disabled", "--columns=name,url"], calls)

    def test_collect_skip_does_not_call_commands(self):
        (self.installations / "work.conf").write_text("[Installation work]\nPath=/mnt/work\n")
        with patch.object(packages, "_run") as run:
            data = packages.collect_packages(skip_rpm=True, skip_flatpak=True)
        run.assert_not_called()
        self.assertEqual(data["rpm"]["status"], "skipped")
        self.assertEqual(data["flatpak"]["status"], "skipped")

    def test_missing_flatpak_is_explicit(self):
        with patch.object(packages, "_run", side_effect=FileNotFoundError("flatpak")):
            data = packages.collect_packages(skip_rpm=True)
        self.assertEqual(data["flatpak"]["status"], "not-installed")
        self.assertEqual(data["flatpak"]["apps"], [])
        self.assertTrue(data["warnings"])

    def test_missing_dnf_is_error(self):
        with patch.object(packages, "_run", side_effect=FileNotFoundError("dnf5")):
            with self.assertRaisesRegex(packages.PackageError, "dnf5"):
                packages.collect_packages(skip_flatpak=True)

    def test_custom_installation_rejected_before_commands(self):
        (self.installations / "work.conf").write_text("[Installation work]\nPath=/mnt/work\n")
        with patch.object(packages, "_run") as run:
            with self.assertRaisesRegex(packages.PackageError, "custom.*skip-flatpak|skip-flatpak.*custom"):
                packages.collect_packages()
        run.assert_not_called()

    def test_absent_custom_installation_directory_is_supported(self):
        self.installations.rmdir()
        with patch.object(packages, "_run", side_effect=self.capture_output):
            packages.collect_packages(skip_rpm=True)

    def test_uninspectable_custom_installations_fail_closed(self):
        self.installations.rmdir()
        self.installations.write_text("not a directory")
        with patch.object(packages, "_run") as run:
            with self.assertRaisesRegex(packages.PackageError, "Cannot inspect Flatpak"):
                packages.collect_packages(skip_rpm=True)
        run.assert_not_called()

    def test_capture_command_failure_is_not_empty_inventory(self):
        with patch.object(packages, "_run", side_effect=packages.PackageError("repo unavailable")):
            with self.assertRaisesRegex(packages.PackageError, "repo unavailable"):
                packages.collect_packages()

    def test_bad_capture_output_is_error(self):
        for output in ["unexpected header\n", "foo.x86_64\textra\n"]:
            with self.subTest(output=output), patch.object(packages, "_run", return_value=output):
                with self.assertRaises(packages.PackageError):
                    packages.collect_packages(skip_flatpak=True)

    def test_validate_valid_manifest(self):
        packages.validate_package_manifest(manifest())

    def test_invalid_rpm_specs_rejected(self):
        for spec in ["-y", "--allowerasing.x86_64", "bash", "bash.*", "pkg.x86_64;id", "pkg.x86_64\n", "./file.rpm", "https://host/pkg.x86_64", "a b.x86_64", "a.x86_64=1"]:
            with self.subTest(spec=spec):
                data = manifest()
                data["rpm"]["requests"] = [spec]
                with self.assertRaises(packages.PackageError):
                    packages.validate_package_manifest(data)

    def test_invalid_shapes_and_status_rejected(self):
        cases = [None, [], {}, {**manifest(), "warnings": "oops"}]
        for section, key, value in [("rpm", "requests", "bash.x86_64"), ("rpm", "inventory", ["bad"]),
                                    ("rpm", "status", "missing"), ("flatpak", "apps", {}),
                                    ("flatpak", "remotes", [None])]:
            data = manifest()
            data[section][key] = value
            cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(packages.PackageError):
                packages.validate_package_manifest(data)

    def test_skipped_sections_cannot_hide_package_requests(self):
        for section in ["rpm", "flatpak"]:
            data = manifest()
            data[section]["status"] = "skipped"
            with self.subTest(section=section), self.assertRaises(packages.PackageError):
                packages.validate_package_manifest(data)

    def test_invalid_flatpak_entries_rejected(self):
        for key, value in [("scope", "custom"), ("ref", "--assumeyes"), ("ref", "org.example.First"),
                           ("ref", "runtime/org.example.First/x86_64/stable"), ("origin", "--from"),
                           ("origin", "https://untrusted.example/")]:
            data = manifest()
            data["flatpak"]["apps"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(packages.PackageError):
                packages.validate_package_manifest(data)

    def test_flatpak_origin_must_have_source_remote(self):
        data = manifest()
        data["flatpak"]["remotes"] = []
        with self.assertRaisesRegex(packages.PackageError, "remote"):
            packages.validate_package_manifest(data)

    def test_duplicate_conflicting_remote_is_error(self):
        data = manifest()
        data["flatpak"]["remotes"].append({"scope": "user", "name": "flathub", "url": "https://evil.example/"})
        with self.assertRaises(packages.PackageError):
            packages.validate_package_manifest(data)

    def test_command_preview_is_pure_and_preserves_scopes(self):
        data = manifest()
        original = copy.deepcopy(data)
        with patch.object(packages, "_run") as run:
            commands = packages.package_commands(data)
        run.assert_not_called()
        self.assertEqual(data, original)
        self.assertEqual(commands, [
            ["sudo", "dnf5", "install", "gcc-c++.x86_64", "lib_example.i686"],
            ["flatpak", "install", "--user", "flathub", "app/org.example.First/x86_64/stable"],
            ["flatpak", "install", "--system", "work", "org.example.Second/x86_64/stable"],
        ])

    def test_skip_flags_remove_only_selected_commands(self):
        self.assertEqual(packages.package_commands(manifest(), skip_rpm=True, skip_flatpak=True), [])
        self.assertEqual(len(packages.package_commands(manifest(), skip_flatpak=True)), 1)
        self.assertEqual(len(packages.package_commands(manifest(), skip_rpm=True)), 2)

    def test_install_preflights_both_scopes_before_rpm(self):
        with patch.object(packages, "_run", side_effect=self.capture_output) as run:
            packages.install_packages(manifest())
        calls = [call.args[0] for call in run.call_args_list]
        rpm_position = next(i for i, cmd in enumerate(calls) if cmd[0] == "sudo")
        before = calls[:rpm_position]
        self.assertTrue(any("--user" in cmd for cmd in before))
        self.assertTrue(any("--system" in cmd for cmd in before))
        self.assertEqual(calls[rpm_position:], packages.package_commands(manifest()))
        for call in run.call_args_list[rpm_position:]:
            self.assertFalse(call.kwargs["capture"])

    def test_missing_changed_or_disabled_remote_blocks_all_mutation(self):
        for mode in ["missing", "changed", "disabled"]:
            def output(argv, **kwargs):
                if "--system" in argv:
                    if mode == "missing" or (mode == "disabled" and "--show-disabled" not in argv):
                        return ""
                    if mode == "changed":
                        return "work\thttps://different.example/\n"
                return self.capture_output(argv, **kwargs)
            with self.subTest(mode=mode), patch.object(packages, "_run", side_effect=output) as run:
                with self.assertRaisesRegex(packages.PackageError, "remote"):
                    packages.install_packages(manifest())
                self.assertTrue(all(call.args[0][1] == "remotes" for call in run.call_args_list))

    def test_install_custom_installation_fails_before_rpm(self):
        (self.installations / "work.conf").write_text("[Installation work]\nPath=/mnt/work\n")
        with patch.object(packages, "_run") as run:
            with self.assertRaisesRegex(packages.PackageError, "skip-flatpak"):
                packages.install_packages(manifest())
        run.assert_not_called()

    def test_install_skip_flatpak_bypasses_custom_installation(self):
        (self.installations / "work.conf").write_text("[Installation work]\nPath=/mnt/work\n")
        with patch.object(packages, "_run", return_value="") as run:
            packages.install_packages(manifest(), skip_flatpak=True)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:3], ["sudo", "dnf5", "install"])

    def test_transaction_failure_stops_later_commands(self):
        def output(argv, **kwargs):
            if argv[0] == "sudo":
                raise packages.PackageError("DNF transaction failed")
            return self.capture_output(argv, **kwargs)
        with patch.object(packages, "_run", side_effect=output) as run:
            with self.assertRaisesRegex(packages.PackageError, "transaction failed"):
                packages.install_packages(manifest())
        self.assertEqual(run.call_args.args[0][0], "sudo")

    def test_flatpak_transaction_failure_stops_following_apps(self):
        def output(argv, **kwargs):
            if argv[:2] == ["flatpak", "install"]:
                raise packages.PackageError("Flatpak transaction failed")
            return self.capture_output(argv, **kwargs)
        with patch.object(packages, "_run", side_effect=output) as run:
            with self.assertRaisesRegex(packages.PackageError, "Flatpak transaction failed"):
                packages.install_packages(manifest(), skip_rpm=True)
        self.assertIn("--user", run.call_args.args[0])
        self.assertEqual(sum(call.args[0][1] == "install" for call in run.call_args_list), 1)

    def test_subprocess_errors_become_actionable_package_errors(self):
        with patch.object(packages.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["dnf5"], stderr="cannot resolve package")):
            with self.assertRaisesRegex(packages.PackageError, "dnf5.*cannot resolve package"):
                packages._run(["dnf5", "repoquery"])

    def test_interactive_subprocess_preserves_confirmation(self):
        with patch.object(packages.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="")) as run:
            packages._run(["sudo", "dnf5", "install", "bash.x86_64"], capture=False)
        kwargs = run.call_args.kwargs
        self.assertNotIn("input", kwargs)
        self.assertFalse(kwargs.get("shell", False))
        self.assertFalse(kwargs.get("capture_output", False))
        self.assertIsNone(kwargs.get("stdin"))


if __name__ == "__main__":
    unittest.main()
