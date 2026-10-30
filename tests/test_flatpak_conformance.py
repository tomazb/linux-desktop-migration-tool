"""Shared Flatpak inventory cases for the Python and legacy Bash validators."""

import json
from pathlib import Path
import subprocess
import unittest

from fedora_migration import packages


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tests/fixtures/flatpak_conformance.json").read_text(encoding="utf-8"))
PYTHON = {
    "refs": packages._flatpak_ref,
    "origins": packages._remote_name,
    "remote_urls": packages._remote_url,
    "scopes": packages._scope,
}
# Bash enumerates its two scopes itself and never parses a scope from data.
BASH = {
    "refs": "flatpak_ref_is_valid",
    "origins": "flatpak_name_is_valid",
    "remote_urls": "flatpak_url_is_valid",
}


class FlatpakConformanceTests(unittest.TestCase):
    def test_python_validators_match_shared_cases(self):
        for group, validate in PYTHON.items():
            for case in CASES[group]:
                with self.subTest(group=group, value=case["value"]):
                    try:
                        validate(case["value"])
                    except packages.PackageError:
                        accepted = False
                    else:
                        accepted = True
                    self.assertEqual(accepted, case["valid"])

    def test_bash_validators_match_shared_cases(self):
        for group, function in BASH.items():
            for case in CASES[group]:
                with self.subTest(group=group, value=case["value"]):
                    # Values are argv data; the validator never evaluates them.
                    result = subprocess.run(
                        ["bash", "-c", 'source "$1"; "$2" "$3"', "conformance",
                         str(ROOT / "migration.sh"), function, case["value"]],
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertIn(result.returncode, (0, 1), result.stderr)
                    self.assertEqual(result.returncode == 0, case["valid"])

    def test_every_group_is_checked_by_python(self):
        self.assertEqual(set(PYTHON), {key for key in CASES if key != "description"})


if __name__ == "__main__":
    unittest.main()
