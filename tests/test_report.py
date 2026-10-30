import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fedora_migration import report
from fedora_migration.bundle import verify_bundle
from test_bundle import BundleFixture

OLD = b"/home/alice"


class HomeReferenceMatcherTests(unittest.TestCase):
    def count(self, data):
        return report.count_home_references(data, OLD)

    def test_matches_followed_by_slash_end_or_non_name_byte(self):
        for data in (b"/home/alice/x", b"/home/alice", b'"/home/alice"',
                     b"/home/alice\n", b"a=/home/alice:/usr/bin", b"/home/alice, ok"):
            with self.subTest(data=data):
                self.assertEqual(self.count(data), 1)

    def test_does_not_match_longer_names(self):
        for data in (b"/home/alice2/x", b"/home/alice.bak", b"/home/alice-old/x",
                     b"/home/alice_x", b"/home/aliceX"):
            with self.subTest(data=data):
                self.assertEqual(self.count(data), 0)

    def test_matches_after_file_url_scheme(self):
        self.assertEqual(self.count(b"file:///home/alice/x"), 1)

    def test_does_not_match_inside_a_longer_path(self):
        self.assertEqual(self.count(b"/mnt/backup/home/alice/x"), 0)

    def test_counts_every_occurrence(self):
        self.assertEqual(self.count(b"/home/alice/a /home/alice2 /home/alice"), 2)

    def test_replace_changes_only_the_prefix(self):
        data = b"file:///home/alice/a /home/alice2/b /home/alice\x00\xff"
        result, count = report.replace_home_references(data, OLD, b"/home/bob")
        self.assertEqual(result, b"file:///home/bob/a /home/alice2/b /home/bob\x00\xff")
        self.assertEqual(count, 2)

    def test_replace_without_a_match_returns_the_same_bytes(self):
        self.assertEqual(report.replace_home_references(b"nothing", OLD, b"/h/b"),
                         (b"nothing", 0))


class HomeReferenceScanTests(BundleFixture):
    def scan(self):
        manifest = verify_bundle(self.bundle)
        return report.find_home_references(self.bundle, manifest, "/home/alice")

    def test_reports_eligible_files_with_counts(self):
        self.write(self.home / ".config/xfce4/xfconf/xfce-perchannel-xml/a.xml",
                   b"/home/alice/a /home/alice/b")
        self.write(self.home / ".config/xfce4/xfconf/xfce-perchannel-xml/clean.xml", b"none")
        self.write(self.home / ".local/share/applications/t.desktop",
                   b"Exec=/home/alice/bin/t\nIcon=/home/alice2/i")
        self.write(self.home / ".gtkrc-2.0", b'include "/home/alice/.gtkrc"')
        self.write(self.home / ".config/gtk-3.0/bookmarks", b"file:///home/alice/Docs")
        self.write(self.home / ".config/gtk-4.0/bookmarks", b"file:///home/alice")
        self.write(self.home / ".config/user-dirs.dirs", b'XDG_X_DIR="/home/alice/X"')
        self.write(self.home / ".config/mimeapps.list", b"x=/home/alice/y.desktop")
        self.write(self.home / ".config/Thunar/uca.xml", b"/home/alice/run")
        self.backup()
        self.assertEqual(dict(self.scan()), {
            ".config/xfce4/xfconf/xfce-perchannel-xml/a.xml": 2,
            ".local/share/applications/t.desktop": 1,
            ".gtkrc-2.0": 1,
            ".config/gtk-3.0/bookmarks": 1,
            ".config/gtk-4.0/bookmarks": 1,
            ".config/user-dirs.dirs": 1,
            ".config/mimeapps.list": 1,
            ".config/Thunar/uca.xml": 1,
        })

    def test_ineligible_paths_are_never_scanned(self):
        self.write(self.home / ".mozilla/firefox/p/prefs.js", b'"/home/alice/x"')
        self.write(self.home / ".mozilla/firefox/p/app.desktop", b"/home/alice/x")
        self.write(self.home / ".thunderbird/p/prefs.js", b'"/home/alice/x"')
        self.write(self.home / ".var/app/org.example/config/x.desktop", b"/home/alice/x")
        self.write(self.home / ".var/app/org.example/config/x.xml", b"/home/alice/x")
        self.write(self.home / ".bashrc", b"/home/alice/x")
        self.write(self.home / ".config/xfce4/notes.txt", b"/home/alice/x")
        self.write(self.home / ".config/gtk-3.0/settings.ini", b"/home/alice/x")
        self.backup()
        self.assertEqual(self.scan(), [])
        with patch.object(report, "_read_payload", side_effect=AssertionError("read")) as read:
            self.assertEqual(self.scan(), [])
        read.assert_not_called()

    def test_files_over_one_mebibyte_are_skipped(self):
        limit = 1024 * 1024
        marker = b"/home/alice/x\n"
        self.write(self.home / ".config/gtk-3.0/bookmarks",
                   marker + b"#" * (limit - len(marker) + 1))
        self.write(self.home / ".config/gtk-4.0/bookmarks",
                   marker + b"#" * (limit - len(marker)))
        self.backup()
        self.assertEqual(self.scan(), [(".config/gtk-4.0/bookmarks", 1)])

    def test_symlinks_and_binary_thunar_files_are_skipped(self):
        (self.home / ".config/gtk-3.0").mkdir(parents=True)
        (self.home / ".config/gtk-3.0/bookmarks").symlink_to("/home/alice/real")
        self.write(self.home / ".config/Thunar/blob", b"\x00/home/alice/x")
        self.backup()
        self.assertEqual(self.scan(), [])

    def test_a_lone_slash_home_is_never_scanned(self):
        self.write(self.home / ".config/gtk-3.0/bookmarks", b"file:///x")
        self.backup()
        manifest = verify_bundle(self.bundle)
        self.assertEqual(report.find_home_references(self.bundle, manifest, "/"), [])


class PanelPluginTests(BundleFixture):
    PANEL = ".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"

    def setUp(self):
        super().setUp()
        self.system_plugins = self.root / "system-plugins"
        self.system_plugins.mkdir()
        patcher = patch.object(report, "PANEL_PLUGIN_DIRS", (str(self.system_plugins),))
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self):
        manifest = verify_bundle(self.bundle)
        return report.check_panel_plugins(self.bundle, manifest, self.target)

    def test_missing_and_present_plugin_types(self):
        self.write(self.home / self.PANEL, b"""<?xml version="1.0"?>
<channel name="xfce4-panel" version="1.0">
  <property name="panels" type="array"><property name="panel-1" type="empty"/></property>
  <property name="plugins" type="empty">
    <property name="plugin-1" type="string" value="whiskermenu"/>
    <property name="plugin-2" type="string" value="tasklist">
      <property name="flat-buttons" type="bool" value="true"/>
    </property>
    <property name="plugin-3" type="string" value="weather"/>
    <property name="plugin-4" type="string" value="weather"/>
    <property name="plugin-5" type="string" value="genmon"/>
  </property>
</channel>""")
        self.backup()
        (self.system_plugins / "tasklist.desktop").write_text("[Desktop Entry]\n")
        user_dir = self.target / ".local/share/xfce4/panel/plugins"
        user_dir.mkdir(parents=True)
        (user_dir / "whiskermenu.desktop").write_text("[Desktop Entry]\n")
        result = self.check()
        self.assertEqual(result["missing"], ["weather", "genmon"])
        self.assertEqual(result["warnings"], [])

    def test_all_plugins_present(self):
        self.write(self.home / self.PANEL,
                   b'<channel><property name="plugins" type="empty">'
                   b'<property name="plugin-1" type="string" value="tasklist"/>'
                   b'</property></channel>')
        self.backup()
        (self.system_plugins / "tasklist.desktop").write_text("")
        self.assertEqual(self.check(), {"missing": [], "warnings": []})

    def test_malformed_xml_is_a_warning_not_a_crash(self):
        self.write(self.home / self.PANEL, b"<channel><property")
        self.backup()
        result = self.check()
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("xfce4-panel.xml", result["warnings"][0])

    def test_entity_declarations_are_refused(self):
        self.write(self.home / self.PANEL,
                   b'<!DOCTYPE c [<!ENTITY a "b">]><channel/>')
        self.backup()
        result = self.check()
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["warnings"]), 1)

    def test_oversized_file_is_refused(self):
        self.write(self.home / self.PANEL, b"<channel>" + b" " * (1024 * 1024) + b"</channel>")
        self.backup()
        result = self.check()
        self.assertEqual(len(result["warnings"]), 1)

    def test_bundle_without_panel_file_has_no_findings(self):
        self.write(self.home / ".bashrc")
        self.backup()
        self.assertEqual(self.check(), {"missing": [], "warnings": []})


class AutostartTests(BundleFixture):
    AUTOSTART = ".config/autostart"

    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        environment = patch.dict(os.environ, {"PATH": str(self.bin)})
        environment.start()
        self.addCleanup(environment.stop)

    def program(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    def entry(self, name, body):
        self.write(self.home / self.AUTOSTART / name, body.encode())

    def check(self):
        self.backup()
        manifest = verify_bundle(self.bundle)
        return report.check_autostart(self.bundle, manifest, self.target)

    def test_missing_program_is_reported_per_desktop_file(self):
        self.entry("gone.desktop", "[Desktop Entry]\nExec=ghost-app --flag %u\n")
        self.entry("here.desktop", "[Desktop Entry]\nExec=present-app\n")
        self.program("present-app")
        result = self.check()
        self.assertEqual(result["missing"],
                         [(".config/autostart/gone.desktop", "ghost-app")])
        self.assertEqual(result["warnings"], [])

    def test_absolute_executable_path(self):
        tool = self.program("abs-tool")
        self.entry("a.desktop", f"[Desktop Entry]\nExec={tool} x\n")
        self.entry("b.desktop", f"[Desktop Entry]\nExec={self.bin}/nope\n")
        self.assertEqual(self.check()["missing"],
                         [(".config/autostart/b.desktop", f"{self.bin}/nope")])

    def test_non_executable_file_is_not_available(self):
        (self.bin / "plain").write_text("data")
        self.entry("a.desktop", "[Desktop Entry]\nExec=plain\n")
        self.assertEqual(self.check()["missing"], [(".config/autostart/a.desktop", "plain")])

    def test_hidden_entries_are_skipped(self):
        self.entry("off.desktop", "[Desktop Entry]\nExec=ghost\nHidden=true\n")
        self.assertEqual(self.check(), {"missing": [], "warnings": []})

    def test_env_prefix_and_assignments_are_skipped(self):
        self.entry("env.desktop", "[Desktop Entry]\nExec=env FOO=1 BAR='a b' wrapped-prog\n")
        self.entry("assign.desktop", "[Desktop Entry]\nExec=FOO=1 other-prog\n")
        self.program("wrapped-prog")
        self.assertEqual(self.check()["missing"],
                         [(".config/autostart/assign.desktop", "other-prog")])

    def test_try_exec_is_preferred(self):
        self.entry("t.desktop", "[Desktop Entry]\nExec=present-app\nTryExec=ghost-try\n")
        self.entry("u.desktop", "[Desktop Entry]\nExec=ghost-exec\nTryExec=present-app\n")
        self.program("present-app")
        self.assertEqual(self.check()["missing"],
                         [(".config/autostart/t.desktop", "ghost-try")])

    def test_only_the_desktop_entry_group_is_read(self):
        self.entry("g.desktop", "[Desktop Action x]\nExec=ghost\n\n[Desktop Entry]\n"
                                "Name=g\nExec=present-app\n")
        self.program("present-app")
        self.assertEqual(self.check(), {"missing": [], "warnings": []})

    def test_unparseable_exec_is_reported(self):
        self.entry("bad.desktop", "[Desktop Entry]\nExec=prog 'unterminated\n")
        result = self.check()
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("unparseable Exec", result["warnings"][0])
        self.assertIn("bad.desktop", result["warnings"][0])

    def test_program_restored_into_the_destination_home_counts_as_available(self):
        self.write(self.home / ".local/bin/mine", b"#!/bin/sh\n", 0o755)
        self.entry("m.desktop", f"[Desktop Entry]\nExec={self.target}/.local/bin/mine\n")
        self.assertEqual(self.check(), {"missing": [], "warnings": []})

    def test_nothing_is_executed(self):
        marker = self.root / "ran"
        tool = self.bin / "touch-marker"
        tool.write_text(f"#!/bin/sh\ntouch {marker}\n")
        tool.chmod(0o755)
        self.entry("x.desktop", "[Desktop Entry]\nExec=touch-marker\nTryExec=touch-marker\n")
        self.check()
        self.assertFalse(marker.exists())


class ReviewFindingTests(BundleFixture):
    PANEL = ".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"

    def setUp(self):
        super().setUp()
        self.system_plugins = self.root / "system-plugins"
        self.system_plugins.mkdir()
        for patcher in (patch.object(report, "PANEL_PLUGIN_DIRS", (str(self.system_plugins),)),
                        patch.dict(os.environ, {"PATH": str(self.root / "bin")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.root / "bin").mkdir()

    def panel(self, body):
        self.write(self.home / self.PANEL, body)
        self.backup()
        return report.check_panel_plugins(self.bundle, verify_bundle(self.bundle), self.target)

    def autostart(self, exec_line, **kwargs):
        self.write(self.home / ".config/autostart/a.desktop",
                   f"[Desktop Entry]\nExec={exec_line}\n".encode())
        self.backup()
        manifest = verify_bundle(self.bundle)
        return report.check_autostart(self.bundle, manifest, self.target, **kwargs)

    def test_unknown_xml_encoding_is_a_warning(self):
        result = self.panel(b'<?xml version="1.0" encoding="unknown-encoding"?><channel/>')
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("Could not check panel plugins", result["warnings"][0])

    def test_nested_plugins_properties_are_checked_linearly(self):
        depth = 6000
        body = (b'<channel name="xfce4-panel">' + b'<property name="plugins" type="empty">' * depth
                + b'<property name="plugin-1" type="string" value="ghost"/>'
                + b"</property>" * depth + b"</channel>")
        import time
        started = time.monotonic()
        result = self.panel(body)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(result["missing"], ["ghost"])

    def test_long_plugin_names_are_truncated(self):
        name = "a" * 5000
        result = self.panel(f'<channel><property name="plugins" type="empty">'
                            f'<property name="plugin-1" type="string" value="{name}"/>'
                            f'</property></channel>'.encode())
        rendered = " ".join(result["missing"] + result["warnings"])
        self.assertLess(len(rendered), 400)
        self.assertIn("...", rendered)

    def test_non_executable_bundled_program_is_missing(self):
        self.write(self.home / ".local/bin/tool", b"#!/bin/sh\n", 0o600)
        result = self.autostart(f"{self.target}/.local/bin/tool")
        self.assertEqual(result["missing"], [(".config/autostart/a.desktop",
                                              f"{self.target}/.local/bin/tool")])

    def test_old_home_program_counts_only_when_rewritten(self):
        self.write(self.home / ".local/bin/tool", b"#!/bin/sh\n", 0o755)
        program = "/home/alice/.local/bin/tool"
        missing = [(".config/autostart/a.desktop", program)]
        self.assertEqual(self.autostart(program, source_home="/home/alice")["missing"], missing)
        self.bundle_cleanup()
        self.assertEqual(self.autostart(program, source_home="/home/alice",
                                        rewrite=True)["missing"], [])

    def bundle_cleanup(self):
        import shutil
        shutil.rmtree(self.bundle)

    def test_env_wrappers_and_options_are_skipped(self):
        present = self.root / "bin/present"
        present.write_text("#!/bin/sh\n")
        present.chmod(0o755)
        cases = {
            "/usr/bin/env FOO=1 ghost-program": [(".config/autostart/a.desktop", "ghost-program")],
            "env -u SESSION_MANAGER present": [],
            "env -i --unset=X -C /tmp FOO=1 present --flag": [],
        }
        for exec_line, expected in cases.items():
            with self.subTest(exec_line=exec_line):
                if self.bundle.exists():
                    self.bundle_cleanup()
                self.assertEqual(self.autostart(exec_line)["missing"], expected)

    def test_desktop_files_outside_settings_locations_are_not_rewrite_candidates(self):
        for relative, expected in (
                (".local/share/applications/a.desktop", True),
                (".config/autostart/a.desktop", True),
                (".config/xfce4/panel/launcher-3/16.desktop", True),
                ("Desktop/launcher.desktop", True),
                ("Desktop/sub/launcher.desktop", False),
                ("Projects/app/data/app.desktop", False),
                ("Documents/notes.desktop", False)):
            with self.subTest(relative=relative):
                self.assertEqual(report.is_rewrite_candidate(relative), expected)


if __name__ == "__main__":
    unittest.main()
