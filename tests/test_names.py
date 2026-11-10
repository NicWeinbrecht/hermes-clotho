"""Naming checks: every user-visible name comes from names.py, and the files that repeat a name agree with it.
No Hermes needed."""
import json
import os
import re
import subprocess
import unittest

import harness as H
import names
import sandbox

# Earlier names of this plugin, built from pieces so this file does not match its own search:
#   the first working name ("dr" + "ess"), not inside ordinary words such as "address";
#   the interim name in any spelling: "run", one optional separator, "through", any case
#   (joined, hyphen, underscore, space; lower, title or upper case: see test_old_name_pattern).
OLD_WORDS = ("dr" + "ess", "rehearse")  # names.py identifiers carry neither
OLD_NAME = re.compile(r"(?<![a-z])" + OLD_WORDS[0] + "|" + "ru" + r"n.?thr" + "ough", re.I)


def _identifiers():
    return {k: v for k, v in vars(names).items() if k.isupper() and isinstance(v, str)}


class Names(unittest.TestCase):
    def test_names_carry_no_old_words(self):
        bad = {k: v for k, v in _identifiers().items()
               if any(w in v.lower() for w in OLD_WORDS) or OLD_NAME.search(v)}
        self.assertEqual(bad, {}, "names.py still carries an old name")

    def test_old_name_pattern(self):
        """The search catches every spelling of both earlier names and leaves ordinary words alone."""
        o = "ru" + "n"
        for hit in (o + "through", o + "-through", "Ru" + "n-Through", o + "_through", "RU" + "N_THROUGH",
                    o + " through", "Dr" + "ess Rehearsal", "dr" + "ess-rehearsal", "hermes dr" + "ess demo",
                    "/dr" + "ess", "DR" + "ESS_TEST_PORT"):
            self.assertTrue(OLD_NAME.search(hit), hit)
        for miss in ("address", "IP addresses", "Address book", "rehearsal", "throughput", "run it through",
                     "hairdresser"):
            self.assertFalse(OLD_NAME.search(miss), miss)

    def test_identifier_shapes(self):
        self.assertRegex(names.PLUGIN_NAME, r"^[a-z][a-z0-9-]*$")
        self.assertRegex(names.CLI_NAME, r"^[a-z][a-z0-9]*$")
        self.assertRegex(names.SLASH_NAME, r"^[a-z][a-z0-9]*$")
        self.assertRegex(names.ENV_PREFIX, r"^[A-Z][A-Z0-9_]*_$")
        for s in (names.LEDGER_SCHEMA, names.META_SCHEMA, names.RECEIPT_SCHEMA):
            self.assertTrue(s.startswith(names.PLUGIN_NAME + "."), s)

    def test_manifest_matches_names(self):
        with open(os.path.join(H.REPO, "plugin.yaml")) as f:
            text = f.read()
        self.assertEqual(re.search(r"^name: *(\S+)", text, re.M).group(1), names.PLUGIN_NAME)
        self.assertEqual(re.search(r"^version: *(\S+)", text, re.M).group(1), names.VERSION)

    def test_sandbox_profile_named_from_names(self):
        self.assertTrue(os.path.isfile(os.path.join(H.REPO, names.SANDBOX_PROFILE)), names.SANDBOX_PROFILE)
        self.assertEqual(os.path.basename(sandbox.PROFILE), names.SANDBOX_PROFILE)

    def test_ledger_schema_and_example_match_names(self):
        with open(os.path.join(H.REPO, "docs", "ledger.schema.json")) as f:
            schema = json.load(f)
        self.assertEqual(schema["properties"]["schema"]["const"], names.LEDGER_SCHEMA)
        self.assertTrue(schema["$id"].startswith(names.REPO_URL + "/"), schema["$id"])
        with open(os.path.join(H.REPO, "docs", "ledger.example.jsonl")) as f:
            rows = [json.loads(x) for x in f if x.strip()]
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["schema"], names.LEDGER_SCHEMA)
            if r.get("simulated_result"):
                self.assertTrue(r["simulated_result"].startswith(names.RESULT_TAG + " "), r["simulated_result"][:40])

    def test_no_old_name_in_tracked_files(self):
        try:
            ls = subprocess.run(["git", "-C", H.REPO, "ls-files", "-z"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("git not available")
        if ls.returncode != 0:
            self.skipTest("not a git checkout (clean export)")
        pat = OLD_NAME
        hits = []
        for rel in filter(None, ls.stdout.split("\0")):
            if pat.search(rel):
                hits.append(rel)
            p = os.path.join(H.REPO, rel)
            if not os.path.isfile(p):
                continue
            with open(p, "rb") as f:
                text = f.read().decode("utf-8", "replace")
            for n, line in enumerate(text.splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:80]}")
        self.assertEqual(hits, [], f"{len(hits)} tracked lines still carry the old name")


if __name__ == "__main__":
    unittest.main()
