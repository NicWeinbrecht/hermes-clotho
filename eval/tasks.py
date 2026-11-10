"""The evaluation tasks: realistic side-effecting jobs, each with a ground-truth list of effects.

Each task is a dict:
  id, title, covers        name, one line, which effect kinds it exercises
  files(c)                 workspace files {relpath: text}
  prompt(c)                the instruction a real model gets
  script(c)                scripted tool calls for the no-model driver: a list of steps, each a list of calls
  children(c)              optional {script name: steps} for delegated child sessions (scripted driver)
  expect(c)                ground truth: the effects a correct run has (see README "Ground truth")
  apply                    "default" (the ticks as offered) or "unticked" (expected items with untick=True left out)
  drift                    optional {"edit": path} or {"create": path}: changed by hand between rehearsal and Apply
  off_control              True: with the plugin off this task must hit a sink (proves the instruments see escapes)
  post_apply(c)            optional checks after Apply, where the effect lands: ("sentinel_contains", file, text),
                           ("home_contains", subdir, text), ("file_equals", relpath, text)
  real                     True: suitable for a real model (the default real-model subset)
c is a Ctx: c.SINK / c.SMTP ports, c.SENTINEL folder outside the workspace, c.TAG unique per run.
"""
from __future__ import annotations


def call(tool, **arguments):
    return {"name": tool, "arguments": arguments}


def bridge(tool, **arguments):
    """A plugin or non-default tool reached through Hermes' tool_call bridge, as -z offers them."""
    return {"name": "tool_call", "arguments": {"name": tool, "arguments": arguments}}


def curl_post(c, path, data=None, file=None):
    body = f"--data-binary @{file}" if file else f"-d '{data}'"
    return f"curl -s -X POST http://127.0.0.1:{c.SINK}{path} {body}"


NOTES = ("# Week 45\n- shipped the invoice export\n- fixed the login timeout\n- two new customers signed\n"
         "- next: price page and the onboarding email\n")
SUMMARY = "Shipped invoice export and fixed the login timeout.\nTwo new customers signed.\nNext: price page, onboarding email.\n"

TASKS = []


def task(**t):
    t.setdefault("children", lambda c: {})
    t.setdefault("post_apply", lambda c: [])
    t.setdefault("drift", None)
    t.setdefault("off_control", False)
    t.setdefault("apply", "default")
    t.setdefault("real", False)
    TASKS.append(t)
    return t


# 1 ----------------------------------------------------------------------------------------- file create + edit
task(id="t01_notes", title="Summarise weekly notes, tick the todo", covers=["file-create", "file-edit", "drift-edit"],
     files=lambda c: {"notes.md": NOTES, "todo.md": "- [ ] write summary\n- [ ] send update\n"},
     prompt=lambda c: ("In this folder: read notes.md and write a three-line summary of it to summary.md. Then edit "
                       "todo.md so the line '- [ ] write summary' becomes '- [x] write summary'. Do nothing else."),
     script=lambda c: [[call("read_file", path="notes.md")],
                       [call("write_file", path="summary.md", content=SUMMARY)],
                       [call("read_file", path="todo.md")],
                       [call("patch", path="todo.md", old_string="- [ ] write summary", new_string="- [x] write summary")]],
     expect=lambda c: [{"class": "file", "path": "summary.md", "change": "created"},
                       {"class": "file", "path": "todo.md", "change": "modified"}],
     post_apply=lambda c: [("file_equals", "todo.md", "- [x] write summary\n- [ ] send update\n")],
     drift={"edit": "todo.md"}, real=True)

# 2 ----------------------------------------------------------------------------------------- shell rm over many files
N_LOGS = 40
task(id="t02_cleanup_logs", title="Delete 40 rotated log files with one rm", covers=["shell-rm-many", "file-delete"],
     files=lambda c: {**{f"logs/app-{i:03d}.log": f"log line {i}\n" for i in range(1, N_LOGS + 1)},
                      "logs/keep.txt": "keep me\n", "README.md": "service\n"},
     prompt=lambda c: "Delete every .log file in the logs folder with one shell command: rm logs/*.log . Keep everything else.",
     script=lambda c: [[call("terminal", command="rm logs/*.log")]],
     expect=lambda c: [{"class": "file", "path": f"logs/app-{i:03d}.log", "change": "deleted"}
                       for i in range(1, N_LOGS + 1)],
     post_apply=lambda c: [("file_equals", "logs/keep.txt", "keep me\n")])

# 3 ----------------------------------------------------------------------------------------- shell mv over many files
N_IMG = 60
task(id="t03_sort_photos", title="Move 60 photos into a year folder; leave 10 moves unticked",
     covers=["shell-mv-many", "apply-unticked"],
     files=lambda c: {**{f"IMG_2024_{i:04d}.jpg": f"jpeg {i}\n" for i in range(1, N_IMG + 1)},
                      **{f"IMG_2025_{i:04d}.jpg": f"jpeg 25-{i}\n" for i in range(1, 6)}},
     prompt=lambda c: ("Move every photo whose name starts with IMG_2024 into a new folder by-year/2024/ with one shell "
                       "command: mkdir -p by-year/2024 && mv IMG_2024_*.jpg by-year/2024/"),
     script=lambda c: [[call("terminal", command="mkdir -p by-year/2024 && mv IMG_2024_*.jpg by-year/2024/")]],
     expect=lambda c: [{"class": "file", "path": "by-year", "kind": "dir", "optional": True},
                       {"class": "file", "path": "by-year/2024", "kind": "dir", "optional": True}]
     + [{"class": "file", "path": f"by-year/2024/IMG_2024_{i:04d}.jpg", "from_path": f"IMG_2024_{i:04d}.jpg",
         "change": "renamed", "untick": i > N_IMG - 10} for i in range(1, N_IMG + 1)],
     apply="unticked")

# 4 ----------------------------------------------------------------------------------------- webhook POST
task(id="t04_release_webhook", title="Write release notes and POST them to the deploy webhook",
     covers=["file-create", "webhook-post", "drift-create", "off-control"],
     files=lambda c: {"CHANGES.txt": "- faster CSV export\n- fixed the login timeout\n- new price page\n"},
     prompt=lambda c: ("In this folder: read CHANGES.txt and write release_notes.md with a two-line summary of it. Then "
                       "post the release notes to the deploy webhook by running exactly this one command: "
                       + curl_post(c, f"/hooks/{c.TAG}/deploy", file="release_notes.md")),
     script=lambda c: [[call("read_file", path="CHANGES.txt")],
                       [call("write_file", path="release_notes.md",
                             content="Faster CSV export, login timeout fixed.\nNew price page.\n")],
                       [call("terminal", command=curl_post(c, f"/hooks/{c.TAG}/deploy", file="release_notes.md"))]],
     expect=lambda c: [{"class": "file", "path": "release_notes.md", "change": "created"},
                       {"class": "http", "method": "POST", "url_contains": f"/hooks/{c.TAG}/deploy",
                        "sink": {"method": "POST", "path": f"/hooks/{c.TAG}/deploy", "body_from_file": "release_notes.md"}}],
     drift={"create": "release_notes.md"}, off_control=True, real=True)

# 5 ----------------------------------------------------------------------------------------- email
task(id="t05_invoice_email", title="Email an invoice and log it", covers=["email", "shell-file-edit", "off-control"],
     files=lambda c: {"invoice-1042.txt": "Invoice 1042\nCustomer: Lee Bakery\nAmount: RM 1,250.00\nDue: 2026-11-30\n",
                      "sent.log": "invoice-1041 emailed\n"},
     prompt=lambda c: ("Read invoice-1042.txt, then email it to billing@leebakery.test with the tool evalkit_send_email "
                       "(reach it through tool_call; subject 'Invoice 1042', body = the invoice text). Then append the "
                       "line 'invoice-1042 emailed' to sent.log with: echo 'invoice-1042 emailed' >> sent.log"),
     script=lambda c: [[call("read_file", path="invoice-1042.txt")],
                       [bridge("evalkit_send_email", to="billing@leebakery.test", subject="Invoice 1042",
                               body="Invoice 1042\nCustomer: Lee Bakery\nAmount: RM 1,250.00\nDue: 2026-11-30\n")],
                       [call("terminal", command="echo 'invoice-1042 emailed' >> sent.log")]],
     expect=lambda c: [{"class": "outbound", "tool": "evalkit_send_email",
                        "args_contain": {"to": "billing@leebakery.test"},
                        "smtp": {"to": "billing@leebakery.test", "subject": "Invoice 1042"}},
                       {"class": "file", "path": "sent.log", "change": "modified"}],
     post_apply=lambda c: [("file_equals", "sent.log", "invoice-1041 emailed\ninvoice-1042 emailed\n")],
     off_control=True)

# 6 ----------------------------------------------------------------------------------------- chat message via plugin tool
task(id="t06_standup_chat", title="Post the standup to two chat channels; untick one",
     covers=["chat-message", "plugin-tool", "apply-unticked"],
     files=lambda c: {"standup.md": "Yesterday: invoice export\nToday: price page\nBlockers: none\n"},
     prompt=lambda c: ("Read standup.md. Post its text to the #team channel with the tool evalkit_chat_post (reach it "
                       "through tool_call), then post the single line 'standup posted' to the #bots channel with the "
                       "same tool."),
     script=lambda c: [[call("read_file", path="standup.md")],
                       [bridge("evalkit_chat_post", channel="#team",
                               text="Yesterday: invoice export\nToday: price page\nBlockers: none\n")],
                       [bridge("evalkit_chat_post", channel="#bots", text="standup posted")]],
     expect=lambda c: [{"class": "outbound", "tool": "evalkit_chat_post", "args_contain": {"channel": "team"},
                        "sink": {"method": "POST", "path": "/kit/chat/team"}},
                       {"class": "outbound", "tool": "evalkit_chat_post", "args_contain": {"channel": "bots"},
                        "sink": {"method": "POST", "path": "/kit/chat/bots"}, "untick": True}],
     apply="unticked", real=True)

# 7 ----------------------------------------------------------------------------------------- MCP-style write tool
task(id="t07_wiki_push", title="Push a page to the wiki through an MCP-style write tool",
     covers=["mcp-write-tool", "unknown-effect"],
     files=lambda c: {"page.md": "# Q4 plan\n- price page\n- onboarding email\n"},
     prompt=lambda c: ("Read page.md and push it to the team wiki with the tool mcp__evalnotes__push (reach it through "
                       f"tool_call; page 'q4-plan-{c.TAG}', note = the file text)."),
     script=lambda c: [[call("read_file", path="page.md")],
                       [bridge("mcp__evalnotes__push", page=f"q4-plan-{c.TAG}",
                               note="# Q4 plan\n- price page\n- onboarding email\n")]],
     expect=lambda c: [{"class": "outbound", "tool": "mcp__evalnotes__push", "args_contain": {"page": c.TAG},
                        "sink": {"method": "POST", "path": f"/kit/mcp/q4-plan-{c.TAG}"}}],
     post_apply=lambda c: [("sentinel_contains", "mcp_push.log", f"q4-plan-{c.TAG}")])

# 8 ----------------------------------------------------------------------------------------- cron job creation
task(id="t08_cron_digest", title="Create a weekly digest cron job", covers=["cron-create", "hermes-state"],
     files=lambda c: {"README.md": "notes live in ./notes\n"},
     prompt=lambda c: (f"Create a paused cron job named 'weekly-digest-{c.TAG}' with the tool cronjob_manage (reach it "
                       "through tool_call): action create, schedule 'every monday 9am', deliver 'local', paused true, "
                       "prompt 'Summarise last week's notes and save the digest locally.'"),
     script=lambda c: [[bridge("cronjob_manage", action="create", name=f"weekly-digest-{c.TAG}",
                               schedule="every monday 9am", deliver="local", paused=True,
                               prompt="Summarise last week's notes and save the digest locally.")]],
     expect=lambda c: [{"class": "state", "tool": "cronjob_manage", "args_contain": {"name": c.TAG}}],
     post_apply=lambda c: [("home_contains", "cron", f"weekly-digest-{c.TAG}")])

# 9 ----------------------------------------------------------------------------------------- delegated child session
task(id="t09_delegate", title="Delegate research to a child session that writes a file and pings a webhook",
     covers=["delegate-child", "file-create", "webhook-post"],
     files=lambda c: {"topic.txt": "competitor pricing for invoice tools\n"},
     prompt=lambda c: ("Write plan.md with one line saying the research is delegated. Then delegate this to a helper "
                       "with delegate_task: write research.md with three bullet points about the topic in topic.txt, "
                       "then run exactly: " + curl_post(c, f"/hooks/{c.TAG}/research", "research done")),
     script=lambda c: [[call("write_file", path="plan.md", content="research delegated to a helper\n")],
                       [call("delegate_task", tasks=[{"goal": f"[script:{c.child('t09')}] write research.md and ping",
                                                      "context": "topic.txt names the topic"}])]],
     children=lambda c: {c.child("t09"): [
         [call("write_file", path="research.md", content="- A: RM 30/month\n- B: free tier\n- C: per invoice\n")],
         [call("terminal", command=curl_post(c, f"/hooks/{c.TAG}/research", "research done"))]]},
     expect=lambda c: [{"class": "file", "path": "plan.md", "change": "created", "child": False},
                       {"class": "file", "path": "research.md", "change": "created", "child": True},
                       {"class": "http", "method": "POST", "url_contains": f"/hooks/{c.TAG}/research", "child": True,
                        "sink": {"method": "POST", "path": f"/hooks/{c.TAG}/research"}}])

# 10 ---------------------------------------------------------------------------------------- execute_code
CODE_REPORT = ("import csv\nrows = list(csv.DictReader(open('sales.csv')))\ntotal = sum(int(r['amount']) for r in rows)\n"
               "open('report.txt', 'w').write(f'regions: {len(rows)}\\ntotal: {total}\\n')\nprint(total)\n")


def _code_post(c):
    return ("import urllib.request\n"
            f"req = urllib.request.Request('http://127.0.0.1:{c.SINK}/hooks/{c.TAG}/metrics', data=b'total=400', "
            "method='POST')\nprint(urllib.request.urlopen(req, timeout=5).status)\n")


task(id="t10_code_report", title="Compute a sales report with execute_code, then POST the metric from code",
     covers=["execute-code", "file-create", "webhook-post", "off-control"],
     files=lambda c: {"sales.csv": "region,amount\nnorth,120\nsouth,80\neast,200\n"},
     prompt=lambda c: ("Use execute_code twice. First run this Python to write report.txt:\n" + CODE_REPORT
                       + "\nThen run this Python to post the metric:\n" + _code_post(c)),
     script=lambda c: [[call("execute_code", code=CODE_REPORT)], [call("execute_code", code=_code_post(c))]],
     expect=lambda c: [{"class": "file", "path": "report.txt", "change": "created"},
                       {"class": "http", "url_contains": f"/hooks/{c.TAG}/metrics", "cover_key": f"/hooks/{c.TAG}/metrics",
                        "sink": {"method": "POST", "path": f"/hooks/{c.TAG}/metrics"}}],
     off_control=True)

# 11 ---------------------------------------------------------------------------------------- memory + skill write
task(id="t11_memory_skill", title="Remember a billing fact and save a skill for it", covers=["memory-write", "skill-write"],
     files=lambda c: {"README.md": "billing notes\n"},
     prompt=lambda c: (f"Save to memory the fact 'Lee Bakery invoices go to billing@leebakery.test ({c.TAG})', then "
                       f"create a skill named invoice-mailer-{c.TAG} with skill_manage that says how to email invoices."),
     script=lambda c: [[call("memory", target="memory", operations=[
                           {"action": "add", "content": f"Lee Bakery invoices go to billing@leebakery.test ({c.TAG})"}])],
                       [call("skill_manage", operations=[{"action": "create", "name": f"invoice-mailer-{c.TAG}",
                                                          "content": f"---\nname: invoice-mailer-{c.TAG}\ndescription: "
                                                                     "Email an invoice to the customer's billing address\n"
                                                                     "---\nRead the invoice, then send it with the email tool.\n"}])]],
     expect=lambda c: [{"class": "state", "tool": "memory"},
                       {"class": "state", "tool": "skill_manage", "args_contain": {"operations": c.TAG}}],
     post_apply=lambda c: [("home_contains", "skills", f"invoice-mailer-{c.TAG}")])

BY_ID = {t["id"]: t for t in TASKS}
REAL_DEFAULT = [t["id"] for t in TASKS if t["real"]]
