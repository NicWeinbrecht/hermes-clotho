"""Hermes-state changes during a rehearsal: what counts as an escape and what is Hermes' own housekeeping.

    python3 eval/test_state_changes.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as R  # noqa: E402


class SplitStateChanges(unittest.TestCase):
    def test_cron_ticker_files_are_housekeeping(self):
        counted, housekeeping = R.split_state_changes(
            ["cron/ticker_heartbeat", "cron/ticker_last_success", "cron/.tick.lock", "cron/.jobs.lock"])
        self.assertEqual(counted, [])
        self.assertEqual(len(housekeeping), 4)

    def test_gateway_dispatch_hold_stamp_is_housekeeping(self):
        # Written by an idle-exit gateway's cron ticker while it drains (Hermes cron/dispatch_hold.py):
        # two timestamps of skipped ticks, no job and no user content.
        counted, housekeeping = R.split_state_changes(["cron/dispatch_held.json"])
        self.assertEqual(counted, [])
        self.assertEqual(housekeeping, ["cron/dispatch_held.json"])

    def test_real_state_still_counts(self):
        paths = ["cron/jobs.json", "cron/output/x.md", "skills/.usage.json", "skills/a/SKILL.md",
                 "memories/MEMORY.md", "cron/dispatch_held.json.tmp"]
        counted, housekeeping = R.split_state_changes(paths)
        self.assertEqual(counted, paths)
        self.assertEqual(housekeeping, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
