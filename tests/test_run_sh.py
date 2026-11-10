"""tests/run.sh itself: it adds the scripted provider to the home's config.yaml without a second top-level
`providers:` key (Hermes refuses a config with two: "could not parse plugin selection"), and the install
step copies the demo's data folder. Runs run.sh against a throwaway folder with every Hermes command
replaced by `true`; nothing is started."""
import os
import subprocess
import tempfile
import unittest

import harness as H
import names


def _run_sh(home: str, stub_port: int) -> subprocess.CompletedProcess:
    env = dict(os.environ, HERMES_TEST_HOME=home, HERMES_CMD="true")
    env[names.env("TEST_PYTHON")] = "true"
    env[names.env("TEST_STUB_PORT")] = str(stub_port)
    return subprocess.run(["bash", os.path.join(H.REPO, "tests", "run.sh"), "nothing"], capture_output=True,
                          text=True, timeout=60, env=env)


class RunSh(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="run-sh-", dir=H.path("scripts") if H.HOME else None)

    def tearDown(self):
        H.move_aside(self.home)

    def _config(self, text: str) -> str:
        with open(os.path.join(self.home, "config.yaml"), "w") as f:
            f.write(text)
        p = _run_sh(self.home, 19999)
        self.assertEqual(p.returncode, 0, p.stdout[-1500:] + p.stderr[-1500:])
        with open(os.path.join(self.home, "config.yaml")) as f:
            return f.read()

    def test_existing_providers_block_gets_the_entry(self):
        out = self._config("model:\n  default: x\nproviders:\n  other:\n    api: http://127.0.0.1:1/v1\n"
                           "plugins:\n  enabled: []\n")
        top = [ln for ln in out.splitlines() if ln.startswith("providers:")]
        self.assertEqual(len(top), 1, out)
        self.assertIn("  scriptstub:\n    api: http://127.0.0.1:19999/v1\n", out)
        self.assertIn("  other:\n    api: http://127.0.0.1:1/v1\n", out)

    def test_no_providers_block_gets_one(self):
        out = self._config("plugins:\n  enabled: []\n")
        self.assertEqual(sum(ln.startswith("providers:") for ln in out.splitlines()), 1, out)
        self.assertIn("  scriptstub:\n    api: http://127.0.0.1:19999/v1\n", out)
        self.assertEqual(self._config(out), out, "a second run changes nothing")

    def test_install_copies_the_demo_data(self):
        self._config("plugins:\n  enabled: []\n")
        dest = os.path.join(self.home, "plugins", names.PLUGIN_NAME)
        for rel in ("demo/seed/downloads.json", "demo/seed/task.txt", "demo/recordings/manifest.json",
                    "plugin.yaml", "__init__.py"):
            self.assertTrue(os.path.isfile(os.path.join(dest, rel)), rel)

    def test_install_from_a_plain_copy_inside_another_work_tree(self):
        """Seen in the check: a copy of the plugin without its own .git, placed inside some other git work
        tree, took the git route, `git ls-files` listed nothing there, and the demo files were missing."""
        outer = tempfile.mkdtemp(prefix="outer-tree-", dir=H.path("scripts") if H.HOME else None)
        try:
            subprocess.run(["git", "init", "-q", outer], check=True, capture_output=True)
            copy = os.path.join(outer, "vendor", names.PLUGIN_NAME)
            os.makedirs(copy)
            files = subprocess.run(["git", "ls-files", "-z"], cwd=H.REPO, capture_output=True, check=True).stdout
            subprocess.run(["rsync", "-a", "--from0", "--files-from=-", "./", copy + "/"], cwd=H.REPO, input=files,
                           check=True)
            env = dict(os.environ, HERMES_CMD="true")
            p = subprocess.run(["bash", os.path.join(copy, "tests", "install_into_home.sh"), self.home],
                               capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(p.returncode, 0, p.stdout[-1500:] + p.stderr[-1500:])
            dest = os.path.join(self.home, "plugins", names.PLUGIN_NAME)
            for rel in ("demo/seed/downloads.json", "demo/seed/task.txt", "demo/recordings/manifest.json",
                        "demo/recordings/claude-sonnet.jsonl"):
                self.assertTrue(os.path.isfile(os.path.join(dest, rel)), rel)
            self.assertFalse(os.path.exists(os.path.join(dest, "demo", "video")), "only demo/seed and recordings")
        finally:
            H.move_aside(outer)


if __name__ == "__main__":
    unittest.main()
