"""The terminal colour contract, checked on rendered text (report.py's docstring states it).

check(test, text, red=[patterns]) walks every SGR code and tracks the state a terminal would be in:
  * only bold (1), dim (2), red (31), reverse (7) and their own closers (22, 39, 27): no reset, no other
    hue, no 256-colour or truecolour;
  * every IRREVERSIBLE badge is exactly BADGE, drawn in reverse video at normal intensity in the default
    colours, and the state after it (once the codes right after it are applied) is the state before it:
    the row's own attribute is opened again;
  * reverse video appears nowhere else; every line ends with every style closed;
  * each run of red characters on a line (spaces trimmed) fully matches one of ``red``; returns the runs.
"""
import re

SGR = re.compile(r"\x1b\[([0-9;]*)m")
ALLOWED = {"1", "2", "22", "31", "39", "7", "27"}
BADGE_TEXT = " ✂  IRREVERSIBLE "
BADGE = "\x1b[22;39;7m" + BADGE_TEXT + "\x1b[27m"
PLAIN_BADGE = "[IRREVERSIBLE]"


def _apply(state, params):
    bold, dim, red, rev = state
    for p in params:
        if p == "1":
            bold = True
        elif p == "2":
            dim = True
        elif p == "22":
            bold = dim = False
        elif p == "31":
            red = True
        elif p == "39":
            red = False
        elif p == "7":
            rev = True
        elif p == "27":
            rev = False
    return (bold, dim, red, rev)


NORMAL = (False, False, False, False)


def cells(text):
    """[(line, [(char, (bold, dim, red, reverse)), ...]), ...] and the list of badge checks."""
    state, out, cur, badges = NORMAL, [], [], []
    i, n = 0, len(text)
    while i < n:
        if text.startswith(BADGE, i):
            before = state
            for ch in BADGE_TEXT:
                cur.append((ch, (False, False, False, True)))
            i += len(BADGE)
            state = _apply(_apply(state, ["22", "39", "7"]), ["27"])
            m = SGR.match(text, i)
            while m:
                state = _apply(state, m.group(1).split(";"))
                i = m.end()
                m = SGR.match(text, i)
            badges.append((before, state))
            continue
        m = SGR.match(text, i)
        if m:
            state = _apply(state, m.group(1).split(";") if m.group(1) else ["0"])
            i = m.end()
            continue
        ch = text[i]
        if ch == "\n":
            out.append((cur, state))
            cur = []
        else:
            cur.append((ch, state))
        i += 1
    out.append((cur, state))
    return out, badges


def check(test, text, red=()):
    for m in SGR.finditer(text):
        params = m.group(1).split(";") if m.group(1) else ["0"]
        test.assertTrue(set(params) <= ALLOWED, f"code outside the contract: {m.group(0)!r}")
    test.assertNotIn(PLAIN_BADGE, text, "the plain badge in coloured output")
    test.assertEqual(text.count("✂"), text.count(BADGE), "a ✂ outside the exact badge")
    lines, badges = cells(text)
    for before, after in badges:
        test.assertEqual(after, before, f"the row's attribute is not restored after the badge: {before} -> {after}")
    runs = []
    for k, (cur, end) in enumerate(lines):
        test.assertEqual(end, NORMAL, f"line {k + 1} ends with a style still open: {''.join(c for c, _ in cur)!r}")
        plain = "".join(c for c, _ in cur)
        for j, (ch, st) in enumerate(cur):
            if st[3]:
                test.assertTrue(BADGE_TEXT in plain, f"reverse video outside the badge on line {k + 1}: {plain!r}")
        seg = ""
        for ch, st in cur + [(" ", NORMAL)]:
            if st[2]:
                seg += ch
            elif seg:
                if seg.strip():
                    runs.append(seg.strip())
                seg = ""
    for r in runs:
        test.assertTrue(any(re.fullmatch(p, r) for p in red), f"red where the contract has none: {r!r}")
    return runs


def styled_lines(text):
    """[(plain line, set of states of its non-space characters outside the badge)]."""
    lines, _ = cells(text)
    out = []
    for cur, _ in lines:
        plain = "".join(c for c, _ in cur)
        sts = {st for c, st in cur if not c.isspace() and not st[3]}
        out.append((plain, sts))
    return out
