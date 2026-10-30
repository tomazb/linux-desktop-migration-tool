"""Schema 2 repository/group records, destination triage and Flatpak batching."""

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fedora_migration import packages

from test_packages import (
    ENVIRONMENTS_TOML, GROUPS_TOML, REPO, REPO_INFO_JSON, manifest, manifest_v1,
)


def repo_json(*repos):
    return json.dumps(list(repos))


def dnf_repo(**overrides):
    entry = {"id": "fedora", "name": "Fedora 43", "base_url": [], "metalink": "",
             "mirrorlist": "", "gpg_key": [], "repo_file_path": "/etc/yum.repos.d/fedora.repo"}
    entry.update(overrides)
    return entry


class CollectionFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.groups_file = root / "groups.toml"
        self.environments_file = root / "environments.toml"
        self.groups_file.write_text(GROUPS_TOML)
        self.environments_file.write_text(ENVIRONMENTS_TOML)
        for name, value in (("GROUPS_STATE", self.groups_file),
                            ("ENVIRONMENTS_STATE", self.environments_file),
                            ("INSTALLATIONS_DIR", root / "installations.d")):
            patcher = patch.object(packages, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.repo_output = REPO_INFO_JSON

    def run_output(self, argv, **kwargs):
        if argv[:3] == ["dnf5", "repo", "info"]:
            if isinstance(self.repo_output, Exception):
                raise self.repo_output
            return self.repo_output
        if "--userinstalled" in argv:
            return "gcc.x86_64\n"
        return "gcc.x86_64\tUser\tfedora\tgcc-0:15.2-1.fc43.x86_64\n"

    def collect(self, **kwargs):
        with patch.object(packages, "_run", side_effect=self.run_output) as run:
            data = packages.collect_packages(skip_flatpak=True, **kwargs)
        self.run = run
        return data

    def warnings_text(self, data):
        return "\n".join(data["warnings"])


class RepositoryCollectionTests(CollectionFixture):
    def test_repo_info_is_queried_with_the_enabled_json_form(self):
        self.collect()
        self.assertIn(["dnf5", "repo", "info", "--enabled", "--json"],
                      [call.args[0] for call in self.run.call_args_list])

    def test_repo_info_list_fields_are_parsed(self):
        self.repo_output = repo_json(dnf_repo(
            id="updates", name="Fedora 43 - Updates",
            base_url=["https://a.example/f43/", "https://b.example/f43/"],
            metalink="https://m.example/metalink?repo=updates", mirrorlist="https://m.example/list",
            gpg_key=["file:///k1", "file:///k2"], repo_file_path="/etc/yum.repos.d/u.repo"))
        data = self.collect()
        self.assertEqual(data["rpm"]["repos"], [{
            "id": "updates", "name": "Fedora 43 - Updates",
            "base_urls": ["https://a.example/f43/", "https://b.example/f43/"],
            "metalink": "https://m.example/metalink?repo=updates",
            "mirrorlist": "https://m.example/list",
            "gpg_keys": ["file:///k1", "file:///k2"],
            "repo_file_path": "/etc/yum.repos.d/u.repo"}])

    def test_default_fixture_matches_the_recorded_repo(self):
        self.assertEqual(self.collect()["rpm"]["repos"], [REPO])

    def test_missing_optional_fields_become_empty(self):
        self.repo_output = repo_json({"id": "bare"})
        self.assertEqual(self.collect()["rpm"]["repos"], [{
            "id": "bare", "name": "", "base_urls": [], "metalink": "", "mirrorlist": "",
            "gpg_keys": [], "repo_file_path": ""}])

    def test_url_credentials_are_removed_everywhere(self):
        self.repo_output = repo_json(dnf_repo(
            base_url=["https://alice:s3cret@mirror.example/f43/", "http://token@host.example:8080/x@y/"],
            metalink="https://bob:pw@m.example/metalink?repo=a@b",
            mirrorlist="ftp://carol:pw2@m.example/list",
            gpg_key=["https://dave:pw3@keys.example/key"]))
        data = self.collect()
        repo = data["rpm"]["repos"][0]
        self.assertEqual(repo["base_urls"], ["https://mirror.example/f43/",
                                             "http://host.example:8080/x@y/"])
        self.assertEqual(repo["metalink"], "https://m.example/metalink?repo=a@b")
        self.assertEqual(repo["mirrorlist"], "ftp://m.example/list")
        self.assertEqual(repo["gpg_keys"], ["https://keys.example/key"])
        self.assertNotIn("s3cret", json.dumps(data))
        for secret in ("alice", "bob", "carol", "dave", "pw", "token@"):
            self.assertNotIn(secret, json.dumps(data["rpm"]["repos"]))

    def test_command_failure_is_a_warning_with_no_repos(self):
        self.repo_output = packages.PackageError("repo info failed")
        data = self.collect()
        self.assertEqual(data["rpm"]["repos"], [])
        self.assertIn("repo", self.warnings_text(data).lower())
        self.assertEqual(data["rpm"]["requests"], ["gcc.x86_64"])

    def test_unusable_json_is_a_warning_with_no_repos(self):
        for output in ("not json", "", "{}", '{"id": "x"}', "[1]", '[{"name": "no id"}]', "null"):
            with self.subTest(output=output):
                self.repo_output = output
                data = self.collect()
                self.assertEqual(data["rpm"]["repos"], [])
                self.assertIn("repo", self.warnings_text(data).lower())

    def test_one_bad_repo_does_not_discard_the_others(self):
        self.repo_output = repo_json(dnf_repo(id="good"), dnf_repo(id="bad id with spaces"),
                                     dnf_repo(id="alsogood"))
        data = self.collect()
        self.assertEqual([repo["id"] for repo in data["rpm"]["repos"]], ["good", "alsogood"])
        self.assertIn("repo", self.warnings_text(data).lower())

    def test_repos_with_control_characters_or_huge_values_are_skipped(self):
        self.repo_output = repo_json(dnf_repo(id="a", name="bad\nname"),
                                     dnf_repo(id="b", metalink="https://x/" + "a" * 5000),
                                     dnf_repo(id="c"))
        data = self.collect()
        self.assertEqual([repo["id"] for repo in data["rpm"]["repos"]], ["c"])
        packages.validate_package_manifest(data)

    def test_duplicate_repo_ids_keep_the_first(self):
        self.repo_output = repo_json(dnf_repo(id="x", name="one"), dnf_repo(id="x", name="two"))
        data = self.collect()
        self.assertEqual([repo["name"] for repo in data["rpm"]["repos"]], ["one"])
        packages.validate_package_manifest(data)

    def test_skipped_rpm_has_empty_new_lists_and_runs_no_commands(self):
        with patch.object(packages, "_run") as run:
            data = packages.collect_packages(skip_rpm=True, skip_flatpak=True)
        run.assert_not_called()
        self.assertEqual((data["rpm"]["repos"], data["rpm"]["groups"], data["rpm"]["environments"]),
                         ([], [], []))
        packages.validate_package_manifest(data)


class GroupStateCollectionTests(CollectionFixture):
    def test_groups_and_environments_are_parsed(self):
        self.groups_file.write_text(
            'version = "1.0"\n[groups.core]\npackages = ["bash"]\nuserinstalled = true\n'
            '[groups.xfce-desktop]\npackages = []\nuserinstalled = false\n'
            '[groups.no-flag]\npackages = []\n')
        self.environments_file.write_text(
            'version = "1.0"\n[environments.xfce-desktop-environment]\ngroups = ["xfce-desktop", "core"]\n'
            '[environments.a-env]\ngroups = []\n')
        data = self.collect()
        self.assertEqual(data["rpm"]["groups"], [
            {"id": "core", "userinstalled": True}, {"id": "no-flag", "userinstalled": False},
            {"id": "xfce-desktop", "userinstalled": False}])
        self.assertEqual(data["rpm"]["environments"], [
            {"id": "a-env", "groups": []},
            {"id": "xfce-desktop-environment", "groups": ["xfce-desktop", "core"]}])
        packages.validate_package_manifest(data)

    def test_default_fixture_files(self):
        data = self.collect()
        self.assertEqual(data["rpm"]["groups"], [{"id": "core", "userinstalled": True}])
        self.assertEqual(data["rpm"]["environments"],
                         [{"id": "xfce-desktop-environment", "groups": ["xfce-desktop"]}])

    def test_missing_files_give_empty_lists_and_warnings(self):
        self.groups_file.unlink()
        self.environments_file.unlink()
        data = self.collect()
        self.assertEqual((data["rpm"]["groups"], data["rpm"]["environments"]), ([], []))
        text = self.warnings_text(data)
        self.assertIn("groups.toml", text)
        self.assertIn("environments.toml", text)

    def test_unreadable_file_is_a_warning(self):
        self.groups_file.unlink()
        self.groups_file.mkdir()
        data = self.collect()
        self.assertEqual(data["rpm"]["groups"], [])
        self.assertIn("groups.toml", self.warnings_text(data))

    def test_unexpected_version_is_a_warning_per_file(self):
        self.groups_file.write_text('version = "2.0"\n[groups.core]\nuserinstalled = true\n')
        data = self.collect()
        self.assertEqual(data["rpm"]["groups"], [])
        self.assertEqual(len(data["rpm"]["environments"]), 1)
        self.assertIn("groups.toml", self.warnings_text(data))
        self.assertNotIn("environments.toml", self.warnings_text(data))

    def test_garbage_is_a_warning_not_an_error(self):
        cases = {
            "toml": "this is = = not toml [",
            "binary": "\xff\xfe\x00",
            "no-version": "[groups.core]\nuserinstalled = true\n",
            "groups-not-table": 'version = "1.0"\ngroups = ["core"]\n',
            "bad-id": 'version = "1.0"\n[groups."bad id"]\nuserinstalled = true\n',
            "flag-type": 'version = "1.0"\n[groups.core]\nuserinstalled = "yes"\n',
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.groups_file.write_text(text, encoding="latin-1", errors="replace")
                data = self.collect()
                self.assertEqual(data["rpm"]["groups"], [])
                self.assertIn("groups.toml", self.warnings_text(data))
                packages.validate_package_manifest(data)

    def test_garbage_environments_is_a_warning(self):
        cases = {
            "groups-type": 'version = "1.0"\n[environments.e]\ngroups = "core"\n',
            "group-id": 'version = "1.0"\n[environments.e]\ngroups = ["bad id"]\n',
            "not-table": 'version = "1.0"\nenvironments = 3\n',
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.environments_file.write_text(text)
                data = self.collect()
                self.assertEqual(data["rpm"]["environments"], [])
                self.assertIn("environments.toml", self.warnings_text(data))

    def test_empty_state_with_valid_version_is_fine(self):
        self.groups_file.write_text('version = "1.0"\n')
        self.environments_file.write_text('version = "1.0"\n')
        data = self.collect()
        self.assertEqual((data["rpm"]["groups"], data["rpm"]["environments"]), ([], []))
        self.assertNotIn("groups.toml", self.warnings_text(data))

    def test_requests_and_inventory_are_unchanged_by_group_state(self):
        data = self.collect()
        self.assertEqual(data["rpm"]["requests"], ["gcc.x86_64"])
        self.assertEqual(data["rpm"]["inventory"][0]["spec"], "gcc.x86_64")


class LocaleTests(unittest.TestCase):
    def run_env(self, argv, **kwargs):
        with patch.dict(os.environ, {"LC_ALL": "sl_SI.UTF-8", "KEEP_ME": "1"}), \
             patch.object(packages.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, stdout="")) as run:
            packages._run(argv, **kwargs)
        return run.call_args.kwargs

    def test_dnf5_and_flatpak_run_with_the_c_utf8_locale_and_the_rest_of_the_environment(self):
        for argv in (["dnf5", "repoquery"], ["flatpak", "list"],
                     ["sudo", "dnf5", "install", "bash.x86_64"], ["flatpak", "install", "--user", "r", "a/b/c"]):
            with self.subTest(argv=argv):
                env = self.run_env(argv, capture=argv[0] != "sudo" and argv[1] != "install")["env"]
                self.assertEqual(env["LC_ALL"], "C.UTF-8")
                self.assertEqual(env["KEEP_ME"], "1")
                self.assertIn("PATH", env)

    def test_the_process_environment_itself_is_not_modified(self):
        with patch.dict(os.environ, {"LC_ALL": "sl_SI.UTF-8"}):
            with patch.object(packages.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0, stdout="")):
                packages._run(["dnf5", "repoquery"])
            self.assertEqual(os.environ["LC_ALL"], "sl_SI.UTF-8")


class SchemaTwoValidationTests(unittest.TestCase):
    def mutate(self, edit, schema_version=2):
        data = manifest()
        edit(data["rpm"])
        packages.validate_package_manifest(data, schema_version)

    def test_valid_v2_manifest(self):
        packages.validate_package_manifest(manifest())

    def test_v2_requires_every_new_key(self):
        for key in ("repos", "groups", "environments"):
            data = manifest()
            del data["rpm"][key]
            with self.subTest(key=key), self.assertRaises(packages.PackageError):
                packages.validate_package_manifest(data)

    def test_v1_keeps_exactly_its_old_keys(self):
        packages.validate_package_manifest(manifest_v1(), schema_version=1)
        self.assertEqual(packages.PACKAGE_KEYS[1]["rpm"], ("status", "requests", "inventory"))
        for key in ("repos", "groups", "environments"):
            data = manifest_v1()
            data["rpm"][key] = []
            with self.subTest(key=key), self.assertRaises(packages.PackageError):
                packages.validate_package_manifest(data, schema_version=1)
        with self.assertRaises(packages.PackageError):
            packages.validate_package_manifest(manifest_v1(), schema_version=2)

    def test_new_fields_must_be_lists(self):
        for key in ("repos", "groups", "environments"):
            for value in ({}, "x", None, 3):
                with self.subTest(key=key, value=value), self.assertRaises(packages.PackageError):
                    self.mutate(lambda rpm: rpm.__setitem__(key, value))

    def test_bad_repo_entries(self):
        def with_repo(**changes):
            def edit(rpm):
                rpm["repos"][0].update(changes)
            return edit
        cases = {
            "id-space": {"id": "a b"}, "id-empty": {"id": ""}, "id-int": {"id": 3},
            "id-slash": {"id": "a/b"}, "name-int": {"name": 3}, "name-control": {"name": "a\nb"},
            "name-del": {"name": "a\x7fb"}, "name-long": {"name": "n" * 5000},
            "base_urls-str": {"base_urls": "https://x/"}, "base_urls-item": {"base_urls": [3]},
            "base_urls-control": {"base_urls": ["https://x/\t"]},
            "base_urls-many": {"base_urls": ["https://x/"] * 1000},
            "metalink-list": {"metalink": []}, "metalink-long": {"metalink": "h" * 5000},
            "mirrorlist-control": {"mirrorlist": "a\x00b"}, "gpg_keys-str": {"gpg_keys": "k"},
            "gpg_keys-control": {"gpg_keys": ["k\n"]}, "path-int": {"repo_file_path": 1},
            "path-control": {"repo_file_path": "/etc/\r"},
        }
        for label, changes in cases.items():
            with self.subTest(label=label), self.assertRaises(packages.PackageError):
                self.mutate(with_repo(**changes))

    def test_repo_entry_keys_are_exact_and_ids_unique(self):
        for edit in (lambda rpm: rpm["repos"][0].update(extra="x"),
                     lambda rpm: rpm["repos"][0].pop("name"),
                     lambda rpm: rpm["repos"].__setitem__(0, "fedora"),
                     lambda rpm: rpm["repos"].append(copy.deepcopy(rpm["repos"][0]))):
            with self.assertRaises(packages.PackageError):
                self.mutate(edit)

    def test_good_repo_id_characters(self):
        for repo_id in ("copr:user:proj", "rpmfusion-free-updates", "a.b_c+d@e-f", "fedora-cisco-openh264"):
            self.mutate(lambda rpm: rpm["repos"][0].update(id=repo_id))

    def test_bad_group_and_environment_entries(self):
        cases = [
            lambda rpm: rpm["groups"][0].update(userinstalled="yes"),
            lambda rpm: rpm["groups"][0].update(userinstalled=1),
            lambda rpm: rpm["groups"][0].update(id="bad id"),
            lambda rpm: rpm["groups"][0].update(id="a\nb"),
            lambda rpm: rpm["groups"][0].update(extra=1),
            lambda rpm: rpm["groups"][0].pop("userinstalled"),
            lambda rpm: rpm["groups"].append(copy.deepcopy(rpm["groups"][0])),
            lambda rpm: rpm["groups"].__setitem__(0, "core"),
            lambda rpm: rpm["environments"][0].update(groups="core"),
            lambda rpm: rpm["environments"][0].update(groups=["bad id"]),
            lambda rpm: rpm["environments"][0].update(groups=[3]),
            lambda rpm: rpm["environments"][0].update(id=""),
            lambda rpm: rpm["environments"][0].update(extra=1),
            lambda rpm: rpm["environments"][0].update(groups=["g"] * 100000),
            lambda rpm: rpm["environments"].append(copy.deepcopy(rpm["environments"][0])),
        ]
        for index, edit in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(packages.PackageError):
                self.mutate(edit)

    def test_skipped_rpm_must_have_empty_new_lists(self):
        data = manifest()
        data["rpm"].update(status="skipped", requests=[], inventory=[])
        with self.assertRaises(packages.PackageError):
            packages.validate_package_manifest(data)
        data["rpm"].update(repos=[], groups=[], environments=[])
        packages.validate_package_manifest(data)


def entry(spec, repo, reason="User", nevra=None):
    name = spec.rpartition(".")[0]
    return {"spec": spec, "reason": reason, "from_repo": repo,
            "full_nevra": nevra or f"{name}-0:1.0-1.fc43.{spec.rpartition('.')[2]}"}


def rpm_data(requests, inventory, repos=(), v1=False):
    data = manifest_v1() if v1 else manifest()
    data["rpm"]["requests"] = list(requests)
    data["rpm"]["inventory"] = list(inventory)
    if not v1:
        data["rpm"]["repos"] = list(repos)
    return data


class TriageTests(unittest.TestCase):
    def test_classification(self):
        data = rpm_data(
            ["zsh.x86_64", "bash.x86_64", "htop.x86_64", "ghost.x86_64"],
            [entry("zsh.x86_64", "fedora"), entry("bash.x86_64", "fedora"),
             entry("htop.x86_64", "fedora"), entry("ghost.x86_64", "copr:u:p")])
        result = packages.triage_rpm(data, installed={"bash.x86_64"},
                                     available={"htop.x86_64", "zsh.x86_64", "bash.x86_64"})
        self.assertEqual(result["installed"], ["bash.x86_64"])
        self.assertEqual(result["installable"], ["htop.x86_64", "zsh.x86_64"])
        self.assertEqual(result["manual"], [])
        self.assertEqual(result["unavailable"], {"copr:u:p": ["ghost.x86_64"]})

    def test_every_request_gets_exactly_one_class(self):
        requests = [f"p{i}.x86_64" for i in range(8)]
        inventory = [entry(spec, ["fedora", "@commandline", "<unknown>", "", "x"][i % 5])
                     for i, spec in enumerate(requests)]
        result = packages.triage_rpm(rpm_data(requests, inventory),
                                     installed={"p0.x86_64", "p5.x86_64"}, available={"p1.x86_64"})
        seen = (result["installed"] + result["installable"] + [m["spec"] for m in result["manual"]]
                + [s for specs in result["unavailable"].values() for s in specs])
        self.assertEqual(sorted(seen), sorted(requests))

    def test_external_user_inventory_is_classified_like_any_other(self):
        data = rpm_data(["hello.x86_64", "other.x86_64"],
                        [entry("hello.x86_64", "copr:u:p", reason="External User"),
                         entry("other.x86_64", "fedora", reason="External User")])
        result = packages.triage_rpm(data, installed=set(), available={"other.x86_64"})
        self.assertEqual(result["installable"], ["other.x86_64"])
        self.assertEqual(result["unavailable"], {"copr:u:p": ["hello.x86_64"]})

    def test_commandline_unknown_and_empty_repos_are_manual_and_never_unavailable(self):
        data = rpm_data(["a.x86_64", "b.x86_64", "c.x86_64"],
                        [entry("a.x86_64", "@commandline", nevra="a-0:2.0-1.x86_64"),
                         entry("b.x86_64", "<unknown>"), entry("c.x86_64", "")])
        result = packages.triage_rpm(data, installed=set(), available=set())
        self.assertEqual(result["unavailable"], {})
        self.assertEqual([item["spec"] for item in result["manual"]],
                         ["a.x86_64", "b.x86_64", "c.x86_64"])
        self.assertEqual(result["manual"][0]["full_nevra"], "a-0:2.0-1.x86_64")
        self.assertEqual(result["manual"][0]["from_repo"], "@commandline")
        self.assertEqual(result["installable"], [])

    def test_manual_packages_that_are_installed_or_available_are_not_manual(self):
        data = rpm_data(["a.x86_64", "b.x86_64"],
                        [entry("a.x86_64", "@commandline"), entry("b.x86_64", "<unknown>")])
        result = packages.triage_rpm(data, installed={"a.x86_64"}, available={"b.x86_64"})
        self.assertEqual((result["installed"], result["installable"], result["manual"]),
                         (["a.x86_64"], ["b.x86_64"], []))

    def test_request_without_inventory_entry_is_manual(self):
        result = packages.triage_rpm(rpm_data(["lonely.x86_64"], []), installed=set(), available=set())
        self.assertEqual([item["spec"] for item in result["manual"]], ["lonely.x86_64"])
        self.assertEqual(result["unavailable"], {})

    def test_unavailable_is_grouped_by_repository_and_sorted(self):
        data = rpm_data(["z.x86_64", "a.x86_64", "m.noarch", "n.x86_64"],
                        [entry("z.x86_64", "r2"), entry("a.x86_64", "r2"),
                         entry("m.noarch", "r1"), entry("n.x86_64", "fedora")])
        result = packages.triage_rpm(data, installed=set(), available={"n.x86_64"})
        self.assertEqual(result["unavailable"], {"r1": ["m.noarch"], "r2": ["a.x86_64", "z.x86_64"]})
        self.assertEqual(list(result["unavailable"]), ["r1", "r2"])

    def test_hint_for_a_recorded_repository_names_where_to_enable_it(self):
        repo = dict(REPO, id="rpmfusion", base_urls=["https://dl.example/rpmfusion/"],
                    metalink="https://mirrors.example/metalink?repo=rpmfusion",
                    gpg_keys=["https://keys.example/RPM-GPG-KEY-rpmfusion"])
        data = rpm_data(["x.x86_64"], [entry("x.x86_64", "rpmfusion")], repos=[repo])
        hint = packages.unavailable_hint(data, "rpmfusion")
        self.assertIn("enable repository rpmfusion", hint)
        self.assertIn("https://dl.example/rpmfusion/", hint)
        self.assertIn("https://mirrors.example/metalink?repo=rpmfusion", hint)
        self.assertIn("retry", hint)
        self.assertNotIn("RPM-GPG-KEY", hint)
        self.assertNotIn("not recorded", hint)

    def test_hint_for_an_unrecorded_repository_says_so(self):
        data = rpm_data(["x.x86_64"], [entry("x.x86_64", "copr:u:p")], repos=[REPO])
        hint = packages.unavailable_hint(data, "copr:u:p")
        self.assertIn("repository copr:u:p from the source system is not recorded", hint)
        self.assertIn("configure it or drop these packages", hint)

    def test_v1_bundles_triage_with_the_not_recorded_hint(self):
        data = rpm_data(["x.x86_64", "y.x86_64"],
                        [entry("x.x86_64", "fedora"), entry("y.x86_64", "fedora")], v1=True)
        self.assertNotIn("repos", data["rpm"])
        result = packages.triage_rpm(data, installed={"y.x86_64"}, available=set())
        self.assertEqual(result["unavailable"], {"fedora": ["x.x86_64"]})
        self.assertIn("not recorded", packages.unavailable_hint(data, "fedora"))

    def test_triage_does_not_mutate_its_input(self):
        data = rpm_data(["x.x86_64"], [entry("x.x86_64", "r")])
        original = copy.deepcopy(data)
        packages.triage_rpm(data, installed=set(), available=set())
        self.assertEqual(data, original)


class DestinationCollectorTests(unittest.TestCase):
    def test_runs_each_read_only_query_once(self):
        def output(argv, **kwargs):
            return "bash.x86_64\nzsh.x86_64\nbash.x86_64\n" if "--installed" in argv \
                else "bash.x86_64\nhtop.x86_64\n\n"
        with patch.object(packages, "_run", side_effect=output) as run:
            installed, available = packages.collect_destination_rpms()
        self.assertEqual(installed, {"bash.x86_64", "zsh.x86_64"})
        self.assertEqual(available, {"bash.x86_64", "htop.x86_64"})
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(sorted(calls), sorted([
            # Excludes must not hide what is already installed; they still apply
            # to what can be installed.
            ["dnf5", "repoquery", "--installed", "--setopt=disable_excludes=*",
             "--queryformat", "%{name}.%{arch}\n"],
            ["dnf5", "repoquery", "--available", "--queryformat", "%{name}.%{arch}\n"]]))

    def test_failure_is_a_package_error(self):
        with patch.object(packages, "_run", side_effect=packages.PackageError("no metadata")):
            with self.assertRaisesRegex(packages.PackageError, "no metadata"):
                packages.collect_destination_rpms()

    def test_missing_dnf_is_a_package_error(self):
        with patch.object(packages, "_run", side_effect=FileNotFoundError("dnf5")):
            with self.assertRaisesRegex(packages.PackageError, "dnf5"):
                packages.collect_destination_rpms()


class TriagedCommandTests(unittest.TestCase):
    def triaged(self, **overrides):
        result = {"installed": ["a.x86_64"], "installable": ["zsh.x86_64", "htop.x86_64"],
                  "manual": [], "unavailable": {}}
        result.update(overrides)
        return result

    def test_rpm_command_has_only_sorted_installable_packages(self):
        commands = packages.package_commands(manifest(), skip_flatpak=True, triage=self.triaged())
        self.assertEqual(commands, [["sudo", "dnf5", "install", "htop.x86_64", "zsh.x86_64"]])

    def test_skip_unavailable_is_passed_only_when_requested(self):
        plain = packages.package_commands(manifest(), skip_flatpak=True, triage=self.triaged())
        self.assertNotIn("--skip-unavailable", plain[0])
        skipping = packages.package_commands(
            manifest(), skip_flatpak=True, triage=self.triaged(unavailable={"r": ["g.x86_64"]}),
            skip_unavailable=True)
        self.assertEqual(skipping, [["sudo", "dnf5", "install", "--skip-unavailable",
                                     "htop.x86_64", "zsh.x86_64"]])

    def test_no_rpm_command_without_installable_packages(self):
        self.assertEqual(packages.package_commands(
            manifest(), skip_flatpak=True, triage=self.triaged(installable=[])), [])

    def test_manual_and_unavailable_packages_are_never_in_the_command(self):
        triage = self.triaged(manual=[{"spec": "m.x86_64", "from_repo": "@commandline", "full_nevra": "m-1"}],
                              unavailable={"r": ["u.x86_64"]})
        flat = [arg for command in packages.package_commands(
            manifest(), skip_flatpak=True, triage=triage, skip_unavailable=True) for arg in command]
        self.assertNotIn("m.x86_64", flat)
        self.assertNotIn("u.x86_64", flat)
        self.assertNotIn("gcc-c++.x86_64", flat)

    def test_without_triage_every_request_is_installed_as_before(self):
        self.assertEqual(packages.package_commands(manifest(), skip_flatpak=True),
                         [["sudo", "dnf5", "install", "gcc-c++.x86_64", "lib_example.i686"]])

    def test_install_refuses_unavailable_packages_unless_skipped_and_runs_nothing(self):
        triage = self.triaged(unavailable={"r": ["u.x86_64"]})
        with patch.object(packages, "_run") as run:
            with self.assertRaisesRegex(packages.PackageError, "skip-unavailable"):
                packages.install_packages(manifest(), triage=triage)
        run.assert_not_called()

    def test_install_with_skip_unavailable_runs_the_triaged_command(self):
        triage = self.triaged(unavailable={"r": ["u.x86_64"]})
        with patch.object(packages, "_run", return_value="") as run:
            packages.install_packages(manifest(), skip_flatpak=True, triage=triage,
                                      skip_unavailable=True)
        run.assert_called_once_with(
            ["sudo", "dnf5", "install", "--skip-unavailable", "htop.x86_64", "zsh.x86_64"],
            capture=False)


class FlatpakBatchingTests(unittest.TestCase):
    def data(self):
        data = manifest()
        data["flatpak"]["remotes"] = [
            {"scope": "user", "name": "flathub", "url": "https://example.org/repo/"},
            {"scope": "user", "name": "other", "url": "https://other.example/repo/"},
            {"scope": "system", "name": "flathub", "url": "https://example.org/repo/"},
            {"scope": "system", "name": "work", "url": "https://work.example/repo/"}]
        data["flatpak"]["apps"] = [
            {"scope": "system", "origin": "flathub", "ref": "org.example.Zed/x86_64/stable"},
            {"scope": "user", "origin": "flathub", "ref": "app/org.example.Zeta/x86_64/stable"},
            {"scope": "user", "origin": "other", "ref": "org.example.Other/x86_64/stable"},
            {"scope": "user", "origin": "flathub", "ref": "app/org.example.Alpha/x86_64/stable"},
            {"scope": "system", "origin": "flathub", "ref": "org.example.Beta/x86_64/stable"},
            {"scope": "system", "origin": "work", "ref": "org.example.Work/x86_64/stable"}]
        return data

    def test_one_install_per_scope_and_origin_with_sorted_refs(self):
        commands = packages.package_commands(self.data(), skip_rpm=True)
        self.assertEqual(commands, [
            ["flatpak", "install", "--user", "flathub",
             "app/org.example.Alpha/x86_64/stable", "app/org.example.Zeta/x86_64/stable"],
            ["flatpak", "install", "--user", "other", "org.example.Other/x86_64/stable"],
            ["flatpak", "install", "--system", "flathub",
             "org.example.Beta/x86_64/stable", "org.example.Zed/x86_64/stable"],
            ["flatpak", "install", "--system", "work", "org.example.Work/x86_64/stable"],
        ])

    def test_confirmations_are_never_suppressed(self):
        for command in packages.package_commands(self.data()):
            for flag in ("-y", "--assumeyes", "--noninteractive"):
                self.assertNotIn(flag, command)

    def test_same_ref_in_both_scopes_stays_in_separate_commands(self):
        data = manifest()
        data["flatpak"]["remotes"] = [
            {"scope": "user", "name": "flathub", "url": "https://example.org/repo/"},
            {"scope": "system", "name": "flathub", "url": "https://example.org/repo/"}]
        data["flatpak"]["apps"] = [
            {"scope": "user", "origin": "flathub", "ref": "org.example.A/x86_64/stable"},
            {"scope": "system", "origin": "flathub", "ref": "org.example.A/x86_64/stable"}]
        self.assertEqual(len(packages.package_commands(data, skip_rpm=True)), 2)

    def test_install_runs_each_batch_interactively_after_the_preflight(self):
        data = self.data()
        remotes = {"user": "flathub\thttps://example.org/repo/\nother\thttps://other.example/repo/\n",
                   "system": "flathub\thttps://example.org/repo/\nwork\thttps://work.example/repo/\n"}

        def output(argv, **kwargs):
            return remotes["user" if "--user" in argv else "system"]
        with patch.object(packages, "INSTALLATIONS_DIR", Path("/nonexistent/installations.d")), \
             patch.object(packages, "_run", side_effect=output) as run:
            packages.install_packages(data, skip_rpm=True)
        installs = [call for call in run.call_args_list if call.args[0][1] == "install"]
        self.assertEqual([call.args[0] for call in installs],
                         packages.package_commands(data, skip_rpm=True))
        self.assertTrue(all(call.kwargs["capture"] is False for call in installs))


class ReviewFindingTriageTests(unittest.TestCase):
    def test_available_packages_from_a_local_source_are_installed_but_flagged(self):
        data = rpm_data(["a.x86_64", "b.x86_64"],
                        [entry("a.x86_64", "@commandline"), entry("b.x86_64", "fedora")])
        result = packages.triage_rpm(data, installed=set(), available={"a.x86_64", "b.x86_64"})
        self.assertEqual(result["installable"], ["a.x86_64", "b.x86_64"])
        self.assertEqual(result["installable_from_local"], ["a.x86_64"])

    def test_duplicate_inventory_rows_do_not_depend_on_order(self):
        rows = [entry("kernel-devel.x86_64", "updates", nevra="kernel-devel-0:6.1-1.x86_64"),
                entry("kernel-devel.x86_64", "@commandline", nevra="kernel-devel-0:6.2-1.x86_64")]
        for inventory in (rows, list(reversed(rows))):
            with self.subTest(order=[row["from_repo"] for row in inventory]):
                result = packages.triage_rpm(rpm_data(["kernel-devel.x86_64"], inventory),
                                             installed=set(), available=set())
                self.assertEqual(result["unavailable"], {"updates": ["kernel-devel.x86_64"]})
                self.assertEqual(result["manual"], [])

    def test_duplicate_local_rows_keep_every_nevra(self):
        rows = [entry("x.x86_64", "@commandline", nevra="x-0:2-1.x86_64"),
                entry("x.x86_64", "<unknown>", nevra="x-0:1-1.x86_64")]
        for inventory in (rows, list(reversed(rows))):
            result = packages.triage_rpm(rpm_data(["x.x86_64"], inventory),
                                         installed=set(), available=set())
            self.assertEqual(len(result["manual"]), 1)
            self.assertEqual(result["manual"][0]["full_nevra"], "x-0:1-1.x86_64, x-0:2-1.x86_64")


class DeepStateFileTests(CollectionFixture):
    def test_deeply_nested_state_file_is_a_warning(self):
        self.groups_file.write_text('version = "1.0"\n[groups.core]\npackages = '
                                    + "{a = " * 600 + "1" + "}" * 600 + "\n")
        data = self.collect()
        self.assertEqual(data["rpm"]["groups"], [])
        self.assertTrue(any("groups" in warning for warning in data["warnings"]))


if __name__ == "__main__":
    unittest.main()
