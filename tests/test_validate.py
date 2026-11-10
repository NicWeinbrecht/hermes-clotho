"""`hermes plugins validate` on a clean export of the tracked files (what an install gets): it must pass, with
the security scan saying safe and no rebinding of Hermes core. The export is made from the working tree
(`git ls-files`), so uncommitted edits are checked too; never the repo root (validate walks .run/)."""
import os
import subprocess
import unittest

import harness as H
import names


class Validate(unittest.TestCase):
    def test_clean_export_validates(self):
        H.require_home()
        x = H.path("export", names.PLUGIN_NAME)
        os.makedirs(x, exist_ok=True)
        files = subprocess.run(["git", "ls-files", "-z"], cwd=H.REPO, capture_output=True, check=True).stdout
        subprocess.run(["rsync", "-a", "--from0", "--files-from=-", "./", x + "/"], cwd=H.REPO, input=files,
                       check=True)
        p = H.hermes(["plugins", "validate", x], cwd=H.root(), label="validate_export", timeout=300)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out[-3000:])
        self.assertIn("Validation passed", out)
        self.assertIn("security scan — safe", out)
        self.assertIn("no core override", out)
        self.assertNotIn("✗", out, out[-3000:])


if __name__ == "__main__":
    unittest.main()
