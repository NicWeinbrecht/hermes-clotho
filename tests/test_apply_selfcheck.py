"""The self-check that runs before `run` starts the model: a provider that can run tools outside
Hermes' tool path gets the same answer whether it is named with --provider, set in
HERMES_INFERENCE_PROVIDER, or configured as the home's default provider (D35).

Units need no Hermes. The Hermes-level tests start `hermes clotho run` (scripted provider, no
model) on each route and check: exit 3, the same reason, no rehearsal folder, no model call."""
import os
import re
import unittest
from unittest import mock

import helpers_apply as HA

CLAUDE = "claude-subscription-directsdk-experimental"
# For the Hermes-level routes: a provider the rule always refuses, whatever is installed in the test home
# (CLAUDE is allowed there once its plugin is installed and verified, D35, and then the run would start).
ALWAYS_REFUSED = "copilot-acp"


def _provider_lines(problems):
    return [p for p in problems if p.startswith("provider ")]


class Units(unittest.TestCase):

    def setUp(self):
        self.cli = HA.sub("cli")
        # as if the Claude-subscription plugin were not installed, whatever this machine has
        p = mock.patch.object(HA.sub("capture"), "_plugin_dir", lambda name: (None, None))
        p.start()
        self.addCleanup(p.stop)

    def test_env_route_gets_the_same_answer_as_the_flag(self):
        env = {k: v for k, v in os.environ.items() if k != self.cli.INFERENCE_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            flag = _provider_lines(self.cli.selfcheck(CLAUDE))
        env[self.cli.INFERENCE_ENV] = CLAUDE
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(self.cli, "effective_provider",
                                  wraps=self.cli.effective_provider) as eff:
            via_env = _provider_lines(self.cli.selfcheck(None))
        self.assertTrue(flag, "the flag route is not refused")
        self.assertTrue(via_env, "HERMES_INFERENCE_PROVIDER is not checked")
        self.assertEqual(eff.call_count, 1)
        strip = lambda ls: [re.sub(r"\(.*?\) ", "", x, count=1) for x in ls]  # noqa: E731 (route label differs)
        self.assertEqual(strip(flag), strip(via_env))

    def test_configured_default_is_checked(self):
        fake = mock.MagicMock()
        fake.resolve_requested_provider = lambda requested=None: CLAUDE
        fake.load_config = lambda: {"model": {"provider": CLAUDE}}
        env = {k: v for k, v in os.environ.items() if k != self.cli.INFERENCE_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.dict("sys.modules", {"hermes_cli.runtime_provider": fake}):
            name, where = self.cli.effective_provider(None)
            probs = _provider_lines(self.cli.selfcheck(None))
        self.assertEqual((name, where), (CLAUDE, "configured default"))
        self.assertTrue(probs, "the configured default provider is not checked")

    def test_acp_base_url_in_config_is_checked(self):
        fake = mock.MagicMock()
        fake.resolve_requested_provider = lambda requested=None: "custom"
        fake.load_config = lambda: {"model": {"provider": "custom", "base_url": "acp://copilot"}}
        with mock.patch.dict("sys.modules", {"hermes_cli.runtime_provider": fake}):
            self.assertTrue(_provider_lines(self.cli.selfcheck(None)))

    def test_ordinary_providers_pass(self):
        for name in ("openrouter", "scriptstub", "nous"):
            self.assertEqual(_provider_lines(self.cli.selfcheck(name)), [], name)

    def test_capture_owns_the_rule_when_it_has_one(self):
        capture = HA.sub("capture")
        seen = []

        def rule(name, base_url=""):
            seen.append(name)
            return "" if name == CLAUDE else "nope"
        with mock.patch.object(capture, "provider_problem", rule, create=True):
            self.assertEqual(_provider_lines(self.cli.selfcheck(CLAUDE)), [])
            self.assertTrue(_provider_lines(self.cli.selfcheck("other")))
        self.assertEqual(seen, [CLAUDE, "other"])


class HermesRoutes(unittest.TestCase):
    """Through Hermes: the three routes give one answer, before any model call."""

    @classmethod
    def setUpClass(cls):
        import harness as H
        cls.H = H
        H.require_home()
        H.start_servers()

    @classmethod
    def tearDownClass(cls):
        cls.H.stop_servers()

    def _run(self, label, home=None, provider=None, **env):
        H = self.H
        ws = H.make_ws(f"selfcheck_{label}", {"a.txt": "a\n"})
        mark = H.script(f"selfcheck_{label}", [[H.call("write_file", path="b.txt", content="b\n")]])
        args = [H.CLI, "run", f"{mark} do it", "-m", "scripted", "--no-diffs"]
        if provider:
            args += ["--provider", provider]
        stub0 = len(H.lines(H.STUB_LOG))
        data = os.path.join(home or H.HOME, "plugin-data", HA.sub("names").PLUGIN_NAME)
        ids0 = set(os.listdir(data)) if os.path.isdir(data) else set()
        extra = dict(env)
        if home:
            extra["HERMES_HOME"] = home
        p = H.hermes(args, cwd=ws, label=f"selfcheck_{label}", **extra)
        ids1 = set(os.listdir(data)) if os.path.isdir(data) else set()
        return p, ids1 - ids0, len(H.lines(H.STUB_LOG)) - stub0, ws

    def _reason(self, p):
        m = re.search(r"provider '([^']+)'(?: \([^)]*\))? (.*)", p.stderr)
        return (m.group(1), m.group(2).strip()) if m else None

    def test_three_routes_one_answer(self):
        H = self.H
        cfg = os.path.join(H.HOME, "config.yaml")
        with open(cfg) as f:
            original = f.read()
        if re.search(r"^model:", original, re.M):
            self.skipTest("the test home already configures a model section")
        results = {
            "flag": self._run("flag", provider=ALWAYS_REFUSED),
            "env": self._run("env", **{"HERMES_INFERENCE_PROVIDER": ALWAYS_REFUSED}),
        }
        # the configured default: written into the test home for this one run, then put back
        # (a home cannot simply be copied: its dependency environment is recorded by absolute path)
        try:
            with open(cfg + ".selfcheck-tmp", "w") as f:
                f.write(original.rstrip("\n") + f"\nmodel:\n  provider: {ALWAYS_REFUSED}\n")
            os.replace(cfg + ".selfcheck-tmp", cfg)
            results["config"] = self._run("config")
        finally:
            with open(cfg + ".selfcheck-tmp", "w") as f:
                f.write(original)
            os.replace(cfg + ".selfcheck-tmp", cfg)
        reasons = {}
        for route, (p, new_ids, stub_calls, ws) in results.items():
            self.assertEqual(p.returncode, 3, f"{route}: {p.stderr[-1200:]}")
            self.assertEqual(new_ids, set(), f"{route}: a rehearsal was armed although it was refused")
            self.assertEqual(stub_calls, 0, f"{route}: the model was called")
            self.assertFalse(os.path.exists(os.path.join(ws, "b.txt")), route)
            reasons[route] = self._reason(p)
            self.assertIsNotNone(reasons[route], f"{route}: {p.stderr[-800:]}")
        self.assertEqual(len(set(reasons.values())), 1, reasons)
        chk = H.hermes([H.CLI, "check", "--provider", ALWAYS_REFUSED], cwd=H.root(), label="selfcheck_check")
        self.assertEqual(chk.returncode, 3, chk.stdout + chk.stderr[-500:])
        self.assertIn(ALWAYS_REFUSED, chk.stdout)


if __name__ == "__main__":
    unittest.main()
