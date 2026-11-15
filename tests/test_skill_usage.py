"""A rehearsed skill_view leaves Hermes' skill usage store to Hermes.

SkillUsage  through the plugin's middleware and Hermes' real skill_view (one process, no agent loop): another
            writer bumps a second skill's counter while the rehearsed read runs, and that bump is still there
            afterwards; the rehearsed read itself is counted, as a normal read is (control: the store moved)
"""
import json
import os
import unittest

import harness as H
import helpers_inproc as I

VIEWED, OTHER = "clotho-usage-viewed", "clotho-usage-other"

DRIVER = r'''
import json, os
import helpers_inproc as I
s = I.Session(os.environ["USAGE_WS"])
import tools.skill_usage as su

def counts():
    path = os.path.join(os.environ["HERMES_HOME"], "skills", ".usage.json")
    data = json.load(open(path)) if os.path.exists(path) else {}
    return {k: (data.get(k) or {}).get("view_count", 0) for k in (os.environ["USAGE_VIEWED"], os.environ["USAGE_OTHER"])}

su.bump_view(os.environ["USAGE_OTHER"])          # both skills have a record before the rehearsed read
su.bump_view(os.environ["USAGE_VIEWED"])
before = counts()
res = s.call("skill_view", before_real=lambda: su.bump_view(os.environ["USAGE_OTHER"]), name=os.environ["USAGE_VIEWED"])
after = counts()
rows = s.finish()
print(json.dumps({"before": before, "after": after, "ok": json.loads(res).get("success"), "rows": rows}))
'''


def _skill(home, name):
    d = os.path.join(home, "skills", "testing", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(f"---\nname: {name}\ndescription: A skill the tests read.\n---\n\n# {name}\n\nRead me.\n")


class SkillUsage(unittest.TestCase):
    def test_another_writers_bump_survives_a_rehearsed_skill_view(self):
        home = I.scratch_home("skill_usage")
        for name in (VIEWED, OTHER):
            _skill(home, name)
        ws = H.make_ws("skill_usage", {"a.txt": "a\n"})
        out = I.result(I.run(DRIVER, home, USAGE_WS=ws, USAGE_VIEWED=VIEWED, USAGE_OTHER=OTHER))
        self.assertNotIn("driver_error", out, out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["rows"], [], "a read is not an effect")
        self.assertEqual(out["after"][OTHER], out["before"][OTHER] + 1,
                         f"the bump another writer made during the rehearsed read was undone: {out}")
        self.assertEqual(out["after"][VIEWED], out["before"][VIEWED] + 1,
                         f"control: the rehearsed skill_view should count as a read, as Hermes counts one: {out}")


if __name__ == "__main__":
    unittest.main()
