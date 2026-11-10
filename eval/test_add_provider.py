"""eval/setup_home.sh must not give a home's config.yaml a second top-level `providers:` key.

    python3 eval/test_add_provider.py
"""
import os
import re
import sys
import unittest

EVAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, EVAL)
from add_provider import add_provider  # noqa: E402

BASE = "plugins:\n  enabled:\n    - clotho\n  disabled: []\ngateway:\n  unmanaged_idle_exit_seconds: 60\n"
OTHER = "providers:\n  scriptstub:\n    api: http://127.0.0.1:19181/v1\n    models:\n      scripted: {}\n"


def top_level_keys(text):
    return [m.group(1) for m in re.finditer(r"^([A-Za-z_][\w-]*):", text, re.M)]


class AddProvider(unittest.TestCase):
    def test_new_block_when_none(self):
        out = add_provider(BASE, "evalstub", 19182)
        self.assertEqual(top_level_keys(out).count("providers"), 1)
        self.assertIn("  evalstub:\n    api: http://127.0.0.1:19182/v1\n", out)

    def test_joins_an_existing_block(self):
        out = add_provider(BASE + OTHER, "evalstub", 19182)
        self.assertEqual(top_level_keys(out).count("providers"), 1, out)
        self.assertIn("  scriptstub:", out)
        self.assertIn("  evalstub:", out)

    def test_idempotent(self):
        once = add_provider(BASE, "evalstub", 19182)
        self.assertEqual(add_provider(once, "evalstub", 19182), once)

    def test_other_port_refused(self):
        with self.assertRaises(ValueError):
            add_provider(add_provider(BASE, "evalstub", 19182), "evalstub", 19030)

    def test_setup_home_uses_it(self):
        with open(os.path.join(EVAL, "setup_home.sh")) as f:
            src = f.read()
        self.assertNotIn("printf 'providers:", src, "setup_home.sh appends a second providers: block")
        self.assertIn("add_provider.py", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
