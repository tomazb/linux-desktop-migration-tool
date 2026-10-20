"""Exercise the selectively ported Bash helpers without network/credential calls."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "migration.sh"
MOCKS = r'''
log_command() { printf '%s\0' "$@" >> "$CALLS"; printf '\0' >> "$CALLS"; }
run_remote_command() {
    log_command remote "$@"
    [[ ${FAIL_REMOTE:-} != "$1" ]] || return 19
    case "$1" in
        'id -nG') printf '%s\n' "$REMOTE_GROUPS" ;;
        'command -v flatpak') return 0 ;;
        'test -d "$HOME/.gnupg"') return "${KEYRING_STATUS:-0}" ;;
        'printf "%s\n" "$HOME"') printf '/home/source\n' ;;
        'command -v nmcli &>/dev/null') [[ ${HAS_NETWORK:-false} == true ]] ;;
        'toolbox list') printf 'CONTAINER ID NAME\n' ;;
        'xdg-user-dir '*) printf '/home/source/Documents\n' ;;
        *installations.d*) printf '%s' "${CUSTOM_REMOTE:-}" ;;
        *'remotes --user --show-disabled'*) cat "$FIXTURE/user-remotes" ;;
        *'remotes --system --show-disabled'*) cat "$FIXTURE/system-remotes" ;;
        *'list --user'*) cat "$FIXTURE/user-apps" ;;
        *'list --system'*) cat "$FIXTURE/system-apps" ;;
        *) printf 'unexpected remote command: %s\n' "$1" >&2; return 97 ;;
    esac
}
flatpak() {
    log_command flatpak "$@"
    local scope=user
    [[ " $* " != *' --system '* ]] || scope=system
    if [[ $1 == remotes ]]; then
        if [[ " $* " == *' --show-disabled '* ]]; then cat "$FIXTURE/dest-$scope-remotes"
        else cat "$FIXTURE/enabled-$scope-remotes"; fi
    elif [[ $1 == install ]]; then
        [[ ${FAIL_INSTALL:-false} != true ]] || return 23
    else return 98; fi
}
id() { log_command id "$@"; printf '%s\n' "$LOCAL_GROUPS"; }
rsync_from_remote() {
    log_command rsync_from_remote "$@"
    mkdir -p "$2"
    cp -a "$FIXTURE/keyring/." "$2/"
    [[ ${FAIL_COPY:-false} != true ]] || return 27
}
restorecon_if_selinux() { log_command restorecon "$@"; }
'''


class LegacyPortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        self.calls = self.root / "calls"
        for scope, remote, app in [
            ("user", "work\thttps://user.example/repo/\n", "app/org.example.First/x86_64/stable\twork\n"),
            ("system", "work\thttps://system.example/repo/\n", "org.example.Second/x86_64/stable\twork\n"),
        ]:
            for prefix in ["", "dest-", "enabled-"]:
                (self.fixture / f"{prefix}{scope}-remotes").write_text(remote)
            (self.fixture / f"{scope}-apps").write_text(app)
        self.env = {**os.environ, "HOME": str(self.home), "USER": "testuser", "SCRIPT": str(SCRIPT),
                    "FIXTURE": str(self.fixture), "CALLS": str(self.calls), "LOCAL_GROUPS": "users wheel",
                    "REMOTE_GROUPS": "users sudo"}

    def run_bash(self, code, **env):
        return subprocess.run(["bash", "-c", 'source "$SCRIPT"\n' + MOCKS + "\n" + code],
                              env={**self.env, **env}, text=True, capture_output=True, timeout=10)

    def commands(self):
        if not self.calls.exists():
            return []
        return [entry.decode().split("\0") for entry in self.calls.read_bytes().split(b"\0\0") if entry]

    def test_sourcing_only_loads_functions(self):
        result = subprocess.run(["bash", "-c", 'source "$SCRIPT"; declare -F migrate_flatpak_applications migrate_gnupg'],
                                env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("migrate_flatpak_applications", result.stdout)

    def test_exact_admin_groups_checked_early(self):
        result = self.run_bash('detect_privileges; printf "%s %s" "$remote_is_privileged" "$local_is_privileged"',
                               LOCAL_GROUPS="users foowheel not-sudo", REMOTE_GROUPS="users sudo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "true false")
        self.assertEqual(self.commands(), [["remote", "id -nG"], ["id", "-nG"]])

    def test_rsync_helper_preserves_literal_excludes_and_protected_paths(self):
        (self.fixture / "--exclude=S.gpg-agentSHOULD_NOT_EXPAND").write_text("fixture")
        result = subprocess.run(["bash", "-c", r'''
source "$SCRIPT"
sshpass() { printf '%s\0' "$@" >> "$CALLS"; printf '\0' >> "$CALLS"; }
password=fixture
username=source
origin_ip=192.0.2.1
cd "$FIXTURE"
rsync_from_remote '/home/source/odd $(not-a-command)/' "$HOME/destination" '--no-specials --no-devices --exclude=S.gpg-agent*'
'''], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.commands()[0]
        self.assertIn("--protect-args", argv)
        self.assertIn("--no-specials", argv)
        self.assertIn("--exclude=S.gpg-agent*", argv)
        self.assertNotIn("--exclude=S.gpg-agentSHOULD_NOT_EXPAND", argv)
        self.assertIn("source@192.0.2.1:/home/source/odd $(not-a-command)/", argv)

    def test_first_app_multiple_remotes_and_scopes_preserved(self):
        (self.fixture / "user-remotes").write_text("work\thttps://user.example/repo/\nextra\thttps://extra.example/repo/\n")
        for prefix in ["dest-", "enabled-"]:
            (self.fixture / f"{prefix}user-remotes").write_text((self.fixture / "user-remotes").read_text())
        result = self.run_bash("migrate_flatpak_applications")
        self.assertEqual(result.returncode, 0, result.stderr)
        installs = [cmd for cmd in self.commands() if cmd[:2] == ["flatpak", "install"]]
        self.assertEqual(installs, [
            ["flatpak", "install", "--user", "work", "app/org.example.First/x86_64/stable"],
            ["flatpak", "install", "--system", "work", "org.example.Second/x86_64/stable"],
        ])
        self.assertFalse(any("remote-add" in cmd for cmd in self.commands()))

    def test_missing_conflicting_and_disabled_remote_block_all_installs(self):
        for mode in ["missing", "conflicting", "disabled"]:
            with self.subTest(mode=mode):
                if self.calls.exists():
                    self.calls.unlink()
                (self.fixture / "dest-system-remotes").write_text(
                    "" if mode == "missing" else "work\thttps://different.example/\n" if mode == "conflicting"
                    else "work\thttps://system.example/repo/\n")
                (self.fixture / "enabled-system-remotes").write_text("" if mode == "disabled" else "work\thttps://system.example/repo/\n")
                result = self.run_bash("migrate_flatpak_applications")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("remote", result.stderr.lower())
                self.assertFalse(any(cmd[:2] == ["flatpak", "install"] for cmd in self.commands()))

    def test_source_capture_failure_does_not_install_partial_list(self):
        result = self.run_bash("migrate_flatpak_applications", FAIL_REMOTE="flatpak list --system --app --columns=ref,origin")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(cmd[:2] == ["flatpak", "install"] for cmd in self.commands()))

    def test_flatpak_failure_propagates_and_does_not_fallback_scope(self):
        result = self.run_bash("migrate_flatpak_applications", FAIL_INSTALL="true", LOCAL_GROUPS="users")
        self.assertNotEqual(result.returncode, 0)
        installs = [cmd for cmd in self.commands() if cmd[:2] == ["flatpak", "install"]]
        self.assertEqual(len(installs), 1)
        self.assertNotIn("-y", installs[0])

    def test_system_failure_never_retries_as_user(self):
        (self.fixture / "user-apps").write_text("")
        result = self.run_bash("local_is_privileged=false; migrate_flatpak_applications", FAIL_INSTALL="true")
        self.assertNotEqual(result.returncode, 0)
        installs = [cmd for cmd in self.commands() if cmd[:2] == ["flatpak", "install"]]
        self.assertEqual(installs, [["flatpak", "install", "--system", "work", "org.example.Second/x86_64/stable"]])

    def test_custom_flatpak_installation_fails(self):
        result = self.run_bash("migrate_flatpak_applications", CUSTOM_REMOTE="/etc/flatpak/installations.d/work.conf")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("custom", result.stderr.lower())
        self.assertFalse(any(cmd[:2] == ["flatpak", "install"] for cmd in self.commands()))

    def test_flatpak_data_cannot_inject_shell_commands(self):
        marker = self.root / "injected"
        (self.fixture / "user-apps").write_text(f"app/org.example.First/x86_64/stable\t$(touch {marker})\n")
        result = self.run_bash("migrate_flatpak_applications")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertFalse(any(cmd[:2] == ["flatpak", "install"] for cmd in self.commands()))

    def test_declined_workflow_does_not_touch_flatpak_or_credentials(self):
        result = self.run_bash(r'''
require_local_bins() { :; }
require_remote_bins() { :; }
sshpass() { log_command sshpass "$@"; }
xdg-user-dir() { printf '%s/Documents\n' "$HOME"; }
toolbox() { :; }
main <<'ANSWERS'

192.0.2.1
source
fixture-password
n
n
n
n

ANSWERS
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("migration is finished", result.stdout)
        self.assertFalse(any(cmd[0] in ["flatpak", "rsync_from_remote"] for cmd in self.commands()))
        self.assertFalse(any(cmd[0] == "remote" and "flatpak" in cmd[1] for cmd in self.commands()))

    def test_three_failed_network_passwords_do_not_abort_strict_bash(self):
        result = self.run_bash(r'''
require_local_bins() { :; }
require_remote_bins() { :; }
sshpass() { :; }
sudo() { log_command sudo "$@"; return 1; }
nmcli() { :; }
xdg-user-dir() { printf '%s/Documents\n' "$HOME"; }
toolbox() { :; }
main <<'ANSWERS'

192.0.2.1
source
fixture-password
n
n
y
bad-one
bad-two
bad-three
n
n

ANSWERS
''', HAS_NETWORK="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Too many failed attempts", result.stdout)
        self.assertEqual(sum(cmd[0] == "sudo" for cmd in self.commands()), 3)

    def create_keyring(self):
        keyring = self.fixture / "keyring"
        for name in ["pubring.kbx", "trustdb.gpg", "private-keys-v1.d/secret.key", "openpgp-revocs.d/revocation.rev", "gpg.conf"]:
            path = keyring / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture {name}")
        return keyring

    def test_complete_keyring_staged_and_private(self):
        keyring = self.create_keyring()
        result = self.run_bash('user_home_origin=/home/source; migrate_gnupg')
        self.assertEqual(result.returncode, 0, result.stderr)
        for source in keyring.rglob("*"):
            target = self.home / ".gnupg" / source.relative_to(keyring)
            self.assertTrue(target.exists())
            self.assertEqual(target.stat().st_mode & 0o777, 0o700 if target.is_dir() else 0o600)
            if source.is_file():
                self.assertEqual(target.read_bytes(), source.read_bytes())
        copies = [cmd for cmd in self.commands() if cmd[0] == "rsync_from_remote"]
        self.assertEqual(len(copies), 1)
        self.assertIn("--no-specials", copies[0][3])
        self.assertIn("--no-devices", copies[0][3])
        self.assertFalse(list(self.home.glob(".gnupg-migration.*")))
        self.assertTrue(any(cmd[0] == "restorecon" for cmd in self.commands()))

    def test_existing_keyring_is_refused_and_preserved(self):
        self.create_keyring()
        target = self.home / ".gnupg"
        target.mkdir()
        existing = target / "trustdb.gpg"
        existing.write_text("existing identities and trust")
        result = self.run_bash('user_home_origin=/home/source; migrate_gnupg')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(existing.read_text(), "existing identities and trust")
        self.assertFalse(any(cmd[0] == "rsync_from_remote" for cmd in self.commands()))

    def test_failed_keyring_copy_does_not_publish_partial_data(self):
        self.create_keyring()
        result = self.run_bash('user_home_origin=/home/source; migrate_gnupg', FAIL_COPY="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".gnupg").exists())
        self.assertFalse(list(self.home.glob(".gnupg-migration.*")))

    def test_failed_copy_preserves_existing_empty_destination(self):
        self.create_keyring()
        target = self.home / ".gnupg"
        target.mkdir(mode=0o700)
        result = self.run_bash('user_home_origin=/home/source; migrate_gnupg', FAIL_COPY="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_remote_keyring_inspection_failure_is_not_absence(self):
        self.create_keyring()
        for status in ["19", "255"]:
            with self.subTest(status=status):
                result = self.run_bash('user_home_origin=/home/source; migrate_gnupg', KEYRING_STATUS=status)
                self.assertEqual(result.returncode, int(status))
                self.assertIn("Could not inspect", result.stderr)
                self.assertFalse((self.home / ".gnupg").exists())

    def test_missing_source_keyring_is_explicit_skip(self):
        result = self.run_bash('user_home_origin=/home/source; migrate_gnupg', KEYRING_STATUS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No GnuPG keyring", result.stdout)
        self.assertFalse((self.home / ".gnupg").exists())

    def test_symlink_keyring_is_rejected(self):
        self.create_keyring()
        outside = self.root / "outside"
        outside.mkdir()
        (self.home / ".gnupg").symlink_to(outside, target_is_directory=True)
        result = self.run_bash('user_home_origin=/home/source; migrate_gnupg')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_home_ancestor_is_rejected(self):
        self.create_keyring()
        alias = self.root / "home-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        result = self.run_bash('user_home_origin=/home/source; migrate_gnupg', HOME=str(alias))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".gnupg").exists())

    def test_source_keyring_symlink_is_rejected_before_publication(self):
        keyring = self.create_keyring()
        outside = self.root / "unrelated"
        outside.write_text("unrelated data")
        (keyring / "untrusted-link").symlink_to(outside)
        result = self.run_bash('user_home_origin=/home/source; migrate_gnupg')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".gnupg").exists())
        self.assertEqual(outside.read_text(), "unrelated data")


if __name__ == "__main__":
    unittest.main()
