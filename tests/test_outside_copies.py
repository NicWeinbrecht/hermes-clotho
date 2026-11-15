"""A rehearsal keeps no copy of a file outside the workspace.

OutsideCopies  through the plugin's middleware with Hermes' real read_file, write_file and patch (one process, no
               agent loop): files outside the workspace carry a random marker each; after a rehearsed read, a
               patch, a read and overwrite, and an edit inside one long line, no file under the home's plugin-data
               holds any marker and the overlay is gone. Controls: a later read sees the rehearsed write, the
               report shows each change's diff, Apply stops on a file edited since the rehearsal, and once named
               with --only it lands every change byte for byte.
"""
import os
import secrets
import tempfile
import unittest

import harness as H
import helpers_inproc as I

DRIVER = r'''
import json, os
import helpers_inproc as I
out = os.environ["OUT_DIR"]
p = lambda name: os.path.join(out, name)
s = I.Session(os.environ["OUT_WS"])
results = {}
results["read_secret"] = s.call("read_file", path=p("secret.txt"))
results["patch"] = s.call("patch", path=p("patched.txt"), old_string="old value", new_string="new value")
results["read_over"] = s.call("read_file", path=p("overwritten.txt"))
results["write_over"] = s.call("write_file", path=p("overwritten.txt"), content="fresh\n")
results["read_back"] = s.call("read_file", path=p("overwritten.txt"))
results["patch_long"] = s.call("patch", path=p("long.txt"), old_string="the blue door", new_string="the red door")
rows = s.finish()
rep = s.m["report"]
text = rep.render_text(s.r.id, color=False, width=120)
print(json.dumps({"rid": s.r.id, "rdir": s.r.dir, "overlay": s.r.outside, "rows": rows, "results": results,
                  "report_text": text}))
'''

APPLY = r'''
import json, os
import helpers_inproc as I
m = I.modules()
ap = m["apply"]
only = [int(x) for x in os.environ["APPLY_ONLY"].split(",")]
print(json.dumps(ap.apply(os.environ["APPLY_RID"], only=only)))
'''


class OutsideCopies(unittest.TestCase):
    maxDiff = None

    def test_no_copy_of_an_outside_file_is_kept(self):
        marks = {k: f"MARK-{k}-{secrets.token_hex(8)}" for k in ("secret", "patched", "over", "long")}
        out = os.path.realpath(tempfile.mkdtemp(prefix="clotho-outside-files-"))
        files = {
            "secret.txt": f"line one\n{marks['secret']}\nline three\n",
            "patched.txt": f"keep {marks['patched']} here\nold value\n",
            "overwritten.txt": f"{marks['over']}\nprivate\n",
            "long.txt": f"a single long line: {marks['long']} and then the blue door, and more after it\n",
        }
        for name, text in files.items():
            with open(os.path.join(out, name), "w") as f:
                f.write(text)
        ws = H.make_ws("outside_copies", {"a.txt": "a\n"})
        home = I.scratch_home("outside_copies")
        res = I.result(I.run(DRIVER, home, OUT_DIR=out, OUT_WS=ws))
        self.assertNotIn("driver_error", res, res)

        # the property: nothing of an outside file is kept in the plugin's data folder, and the overlay is gone
        data = os.path.join(home, "plugin-data")
        hits = []
        for dirpath, _dirs, names in os.walk(data):
            for n in names:
                fp = os.path.join(dirpath, n)
                with open(fp, "rb") as f:
                    blob = f.read()
                hits += [f"{os.path.relpath(fp, data)} holds {k}" for k, v in marks.items() if v.encode() in blob]
        self.assertEqual(hits, [], "a copy of a file outside the workspace was kept under plugin-data")
        self.assertFalse(os.path.exists(res["overlay"]), "the outside overlay is still on disk after the rehearsal")
        for name, text in files.items():
            with open(os.path.join(out, name)) as f:
                self.assertEqual(f.read(), text, f"the rehearsal changed {name} for real")

        # controls: the rehearsal still worked, and the report still shows the changes
        self.assertIn("fresh", res["results"]["read_back"], "a read after the rehearsed write did not see it")
        self.assertIn(marks["secret"], res["results"]["read_secret"], "control: the outside read returned nothing")
        rows = {os.path.basename(r["target"]["path"]): r for r in res["rows"] if r["class"] == "file"}
        self.assertEqual(set(rows), {"patched.txt", "overwritten.txt", "long.txt"}, [r["summary"] for r in res["rows"]])
        for r in rows.values():
            self.assertIn("OUTSIDE WORKSPACE", r["badges"])
        text = res["report_text"]
        for line in ("-old value", "+new value", "+fresh", "+a single long line"):
            self.assertIn(line, text, f"the report does not show {line!r}")

        # Apply: a file edited since the rehearsal stops it before any change ...
        ids = ",".join(str(r["id"]) for r in rows.values())
        long_path = os.path.join(out, "long.txt")
        with open(long_path, "a") as f:
            f.write("edited by hand\n")
        a = I.result(I.run(APPLY, home, APPLY_RID=res["rid"], APPLY_ONLY=ids))
        self.assertEqual(a.get("status"), "drift", a)
        for name in ("patched.txt", "overwritten.txt"):
            with open(os.path.join(out, name)) as f:
                self.assertEqual(f.read(), files[name], f"a drift stop still changed {name}")
        # ... and once the file is back, the named changes land as rehearsed
        with open(long_path, "w") as f:
            f.write(files["long.txt"])
        a = I.result(I.run(APPLY, home, APPLY_RID=res["rid"], APPLY_ONLY=ids))
        self.assertEqual(a.get("status"), "ok", a)
        want = {"patched.txt": f"keep {marks['patched']} here\nnew value\n", "overwritten.txt": "fresh\n",
                "long.txt": files["long.txt"].replace("the blue door", "the red door"),
                "secret.txt": files["secret.txt"]}
        for name, text in want.items():
            with open(os.path.join(out, name)) as f:
                self.assertEqual(f.read(), text, f"{name} after apply")


if __name__ == "__main__":
    unittest.main()
