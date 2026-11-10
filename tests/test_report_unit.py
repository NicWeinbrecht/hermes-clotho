"""The terminal report on synthetic rehearsals: 58 columns, the ANSI contract, plain output,
header counts, collapsed groups, generic rendering of unknown badges and fields, the compact
card, --json shape, and render time on 300 effects. No Hermes process, no network."""
import io
import json
import os
import re
import sys
import time
import unittest
from unittest import mock

import helpers_apply as HA

import ansi_contract as AC

BADGE = AC.BADGE


def _check_ansi(test, text, red=()):
    """The colour contract (report.py's docstring): no hue in a report, the badge in reverse video."""
    return AC.check(test, text, red=red)


class Report300(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fr = HA.big_run(300)
        cls.apply, cls.report = HA.setup(cls.fr)

    def test_render_time_and_58_columns(self):
        for color in (True, False):
            t0 = time.perf_counter()
            text = self.report.render_text(self.fr.rid, color=color)
            dt = time.perf_counter() - t0
            print(f"\n[measure] 300-effect report, colour={color}: {dt * 1000:.1f} ms, "
                  f"{len(text.splitlines())} lines", file=sys.stderr)
            self.assertLess(dt, 1.0)
            wide = [ln for ln in text.splitlines() if HA.width(ln) > 58]
            self.assertEqual(wide, [], f"{len(wide)} lines wider than 58 columns, e.g. {wide[:3]}")
        t0 = time.perf_counter()
        text = self.report.render_text(self.fr.rid, color=True, expand=True)
        dt = time.perf_counter() - t0
        print(f"[measure] 300-effect report expanded: {dt * 1000:.1f} ms, {len(text.splitlines())} lines",
              file=sys.stderr)
        self.assertLess(dt, 1.0)
        self.assertEqual([ln for ln in text.splitlines() if HA.width(ln) > 58], [])

    def test_ansi_contract(self):
        text = self.report.render_text(self.fr.rid, color=True)
        self.assertIn(BADGE, text)
        _check_ansi(self, text)
        _check_ansi(self, self.report.render_text(self.fr.rid, color=True, expand=True))

    def test_badge_plain_without_colour(self):
        plain = self.report.render_text(self.fr.rid, color=False)
        self.assertIn(AC.PLAIN_BADGE, plain)
        self.assertNotIn("✂", plain)
        self.assertNotIn(AC.BADGE_TEXT.strip(), plain)

    def test_no_red_in_a_report(self):
        for expand in (False, True):
            self.assertEqual(_check_ansi(self, self.report.render_text(self.fr.rid, color=True, expand=expand)), [])

    def test_plain_when_no_color_or_not_a_tty(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1", "FORCE_COLOR": "1"}):
            self.assertNotIn("\x1b", self.report.render_text(self.fr.rid))
        env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "FORCE_COLOR")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertNotIn("\x1b", self.report.render_text(self.fr.rid))

    def test_header_counts_by_class_and_irreversible(self):
        text = HA.visible(self.report.render_text(self.fr.rid, color=False))
        head = "\n".join(text.splitlines()[:8])
        self.assertIn("299 effects", head)  # the mv group counts through its 214 moves
        self.assertRegex(head, r"\b24 irreversible\b")  # 12 web + 10 messages + rsync + the unknown tool
        self.assertIn("files 275", head)
        self.assertIn("web 12", head)
        self.assertIn("messages 10", head)

    def test_similar_effects_collapse_and_expand(self):
        short = HA.visible(self.report.render_text(self.fr.rid, color=False))
        full = HA.visible(self.report.render_text(self.fr.rid, color=False, expand=True))
        self.assertIn("2-215", short)
        self.assertIn("214 files", short)
        self.assertNotIn("IMG_0150", short)
        self.assertIn("IMG_0150", full)
        self.assertLess(len(short.splitlines()), 120)
        self.assertGreater(len(full.splitlines()), 300)
        self.assertIn("--expand", short)
        # the range shown is the range --only takes
        cli = HA.sub("cli")
        rows = HA.sub("ledger").load(os.path.join(self.fr.dir, "ledger.jsonl"))
        sel, _ = self.apply.select(rows, only=cli._ids("2-215"))
        self.assertEqual(sel - {1}, set(range(2, 216)))

    def test_unknown_badges_and_fields_render_generically(self):
        text = HA.visible(self.report.render_text(self.fr.rid, color=False))
        self.assertIn("SOME NEW BADGE", text)
        colored = self.report.render_text(self.fr.rid, color=True)
        _check_ansi(self, colored)

    def test_json_shape_unchanged(self):
        j = self.report.to_json(self.fr.rid)
        self.assertEqual(set(j), {"rehearsal", "summary", "counts", "default_selection", "applied", "effects"})
        self.assertEqual(len(j["effects"]), 300)
        json.dumps(j)

    def test_card_is_compact_and_plain(self):
        card = self.report.render_card(self.fr.rid)
        lines = card.splitlines()
        self.assertLessEqual(len(lines), 14, card)
        self.assertNotIn("\x1b", card)
        self.assertIn(self.fr.rid, card)
        self.assertIn("299 effects", card)
        self.assertIn("irreversible", card)
        self.assertIn("apply", card)

    def test_effect_summaries_are_reusable(self):
        rows = HA.sub("ledger").load(os.path.join(self.fr.dir, "ledger.jsonl"))
        views = [self.report.describe(r) for r in rows]
        self.assertEqual(len({v["key"] for v in views}), 300, "each effect gets a stable key")
        t = self.report.tally(rows)
        self.assertEqual(t["effects"], 299)
        self.assertEqual(t["by_class"]["file"], 275)
        self.assertEqual(t["irreversible"], 24)


class ReportSmall(unittest.TestCase):

    def test_guessed_dependency_is_called_a_guess(self):
        run = HA.FakeRun()
        count = run.file("count.txt", "3\n")
        grp = run.command("ls > listing.txt")
        run.file("listing.txt", "count.txt\n", parent=grp)
        post = run.http("curl -s -X POST http://127.0.0.1:9/hook --data-binary @count.txt",
                        "POST", "http://127.0.0.1:9/hook", body="3\n")
        run.depends(post, count)
        run.finish()
        _, report = HA.setup(run)
        text = HA.visible(report.render_text(run.rid, color=False))
        self.assertRegex(text, r"uses the output of #1\b")
        self.assertRegex(text, r"(?s)listing\.txt.*may use the output of #1 \(guess")

    def test_long_task_and_workspace_fit(self):
        run = HA.FakeRun()
        run.http("curl -s -X POST https://example.invalid/" + "x" * 120 + " -d @" + "y" * 80,
                 "POST", "https://example.invalid/" + "x" * 120, body="z" * 300)
        run.finish(task="a very long task " * 20, workspace="/Users/someone/" + "deep/" * 30 + "ws")
        _, report = HA.setup(run)
        for color in (True, False):
            text = report.render_text(run.rid, color=color)
            self.assertEqual([ln for ln in text.splitlines() if HA.width(ln) > 58], [])
        self.assertEqual([ln for ln in report.render_text(run.rid, color=False, width=100).splitlines()
                          if HA.width(ln) > 100], [])


class ReportFields(unittest.TestCase):
    """Fields the capture side records for the reader: the code an execute_code call ran, what a
    can't-preview command was seen doing before its changes were thrown away, the path the agent
    named when a symlink sent the write elsewhere, and what a blocked call tried to touch."""

    def _render(self, run):
        run.finish()
        _, report = HA.setup(run)
        colored = report.render_text(run.rid, color=True)
        _check_ansi(self, colored)
        text = HA.visible(report.render_text(run.rid, color=False))
        self.assertEqual([ln for ln in text.splitlines() if HA.width(ln) > 58], [], text)
        return text

    def test_execute_code_rows_show_their_code(self):
        run = HA.FakeRun()
        code = ("import os\nimport shutil\n\nroot = 'cache'\nkept = []\nfor name in sorted(os.listdir(root)):\n"
                "    if name.endswith('.keep'):\n        kept.append(name)\n        continue\n"
                "    os.remove(os.path.join(root, name))\nprint('cleared', len(kept))\n")
        run._base("command", "execute_code", {"code": code}, "run code `import os` - can't preview: needs the network",
                  target={"kind": "command", "command": code, "cwd": run.ws}, irreversible=True, previewable=False,
                  default_selected=False, badges=["CAN'T PREVIEW", "IRREVERSIBLE"],
                  preview={"reasons": ["needs the network"], "code": code, "stdout": "", "exit_code": 1},
                  apply={"method": "command", "appliable": True, "reason": "runs for real only when you name it"})
        text = self._render(run)
        self.assertIn("os.remove(os.path.join(root, name))", text)  # line 10: the line that deletes
        self.assertIn("print('cleared', len(kept))", text)

    def test_discarded_partial_changes_are_counted(self):
        run = HA.FakeRun()
        partial = [{"path": f"logs/old_{i:02d}.log", "change": "deleted"} for i in range(11)]
        partial += [{"path": "logs/index.txt", "change": "created"}, {"path": "notes.txt", "change": "modified"}]
        run.cant_preview("find logs -mtime +7 -delete && curl -s https://example.invalid/ping")
        run.rows[-1]["preview"].update(partial_changes_discarded=partial, stdout="", exit_code=0)
        text = self._render(run)
        self.assertIn("would delete 11 files", text)
        self.assertIn("would create 1 file", text)
        self.assertIn("would modify 1 file", text)
        self.assertIn("logs/old_00.log", text)
        self.assertIn("discarded", text)

    def test_via_symlink_rows_name_the_requested_path(self):
        run = HA.FakeRun()
        r = run.file("real/target/notes.md", "hi\n")
        r["badges"] = ["VIA SYMLINK"]
        r["target"]["requested_path"] = "link_to_notes/notes.md"
        text = self._render(run)
        self.assertIn("link_to_notes/notes.md", text)
        self.assertRegex(text, r"(?s)VIA SYMLINK.*link_to_notes/notes\.md")

    def test_blocked_rows_show_what_they_tried(self):
        run = HA.FakeRun()
        blocked = dict(irreversible=False, previewable=False, default_selected=False, badges=["BLOCKED"],
                       apply={"method": "none", "appliable": False, "reason": "blocked during the rehearsal"})
        run._base("blocked", "write_file", {"path": "drafts/plan.md"}, "blocked write_file: refused",
                  target={"kind": "file", "path": "drafts/plan.md", "abs_path": run.ws + "/drafts/plan.md",
                          "in_workspace": True},
                  preview={"error": "PermissionError: [Errno 1] Operation not permitted", "traceback": None}, **blocked)
        run._base("blocked", "terminal", {"command": "rm -rf build"}, "blocked terminal: late call",
                  target={"kind": "command", "command": "rm -rf build", "cwd": run.ws},
                  preview={"error": "the rehearsal had already ended"}, **blocked)
        run._base("blocked", "send_message", {"to": "@sam"}, "blocked send_message: backstop",
                  target={"kind": "tool", "name": "send_message", "recipients": ["to=@sam"]},
                  preview={"error": "reached real dispatch without the rehearsal's stamp"}, **blocked)
        run._base("blocked", "terminal", {"command": "curl -X POST https://example.invalid/x"}, "blocked terminal",
                  target={"kind": "http", "method": "POST", "url": "https://example.invalid/x"},
                  preview={"error": "RuntimeError: injected fault"}, **blocked)
        run._base("blocked", "write_file", {"path": "x"}, "blocked write_file: old row", target=None,
                  preview={"error": "RuntimeError: injected fault (middleware)", "traceback": None}, **blocked)
        text = " ".join(self._render(run).split())  # wrapped lines joined
        self.assertRegex(text, r"(?s)tried: .*drafts/plan\.md.*Operation not permitted")
        self.assertRegex(text, r"(?s)tried: .*rm -rf build.*already ended")
        self.assertRegex(text, r"(?s)tried: .*send_message.*@sam")
        self.assertRegex(text, r"(?s)tried: .*POST https://example\.invalid/x")
        self.assertIn("injected fault (middleware)", text)


class Receipt(unittest.TestCase):
    """The receipt's "0 model calls" is the one red number of an apply; stops and dry runs have none."""

    RC = {"status": "applied", "applied": 4, "total": 4, "skipped": 0, "model_calls": 0, "seconds": 0.4,
          "results": [{"id": i, "summary": f"create note_{i}.md", "result": "applied"} for i in range(1, 5)]}

    def setUp(self):
        self.report = HA.sub("report")

    def test_zero_model_calls_is_red(self):
        text = self.report.render_receipt(self.RC, color=True)
        self.assertEqual(_check_ansi(self, text, red=[r"0 model calls"]), ["0 model calls"])
        self.assertTrue(HA.visible(text).splitlines()[-1].startswith("applied 4/4 · skipped 0 · 0 model calls"))
        self.assertNotIn("\x1b", self.report.render_receipt(self.RC, color=False))

    def test_stops_and_dry_runs_have_no_red(self):
        for rc in ({"status": "drift", "model_calls": 0, "drift": [{"path": "todo.md", "why": "changed"}],
                    "results": []},
                   {"status": "dependency", "model_calls": 0, "results": [],
                    "conflicts": [{"id": 2, "summary": "POST x", "needs": 1, "needs_summary": "create y",
                                   "sure": True, "why": "same file"}]},
                   {"status": "dry-run", "selected": [1, 2], "results": []}):
            self.assertEqual(_check_ansi(self, self.report.render_receipt(rc, color=True)), [], rc["status"])


class ExampleLedger(unittest.TestCase):
    """Every line of the frozen example ledger (docs/ledger.example.jsonl) renders, within 58 columns,
    with whatever badges and fields a later version adds."""

    def test_every_example_line_renders(self):
        src = os.path.join(HA.REPO, "docs", "ledger.example.jsonl")
        rows = [json.loads(x) for x in open(src) if x.strip()]
        run = HA.FakeRun()
        for n, r in enumerate(rows, 1):
            r = dict(r, id=n, parent_id=None, children=[], depends_on=[], dependents=[])
            run.rows.append(r)
        run.finish()
        _, report = HA.setup(run)
        for color in (True, False):
            text = report.render_text(run.rid, color=color)
            self.assertEqual([ln for ln in text.splitlines() if HA.width(ln) > 58], [])
            if color:
                _check_ansi(self, text)
        for r in rows:
            d = report.describe(r)
            self.assertTrue(d["key"] and d["kind"], d)
        self.assertNotIn("\x1b", report.render_card(run.rid))


if __name__ == "__main__":
    unittest.main()
