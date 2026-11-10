"""Workspace clones, stat manifests, fingerprints and effect diffs (standard library only).

A rehearsal keeps three kinds of copies of the workspace, all APFS copy-on-write clones:
``base`` (frozen pre-state), ``work`` (where file tools and shell commands act, so later reads
see earlier writes) and one ``snaps/<n>`` per shell command (the pre-state of that command, so
its own diff is exact). Effects are found by comparing stat manifests and confirmed by sha256.
"""
from __future__ import annotations

import ctypes
import difflib
import hashlib
import os
import shutil
import stat
import sys

TEXT_LIMIT = 1_000_000  # bytes; larger files get no unified diff
CLONE_NOFOLLOW = 0x0001

_libc = None
if sys.platform == "darwin":
    try:
        _libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        _libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    except Exception:  # pragma: no cover - not macOS or no clonefile
        _libc = None


def clonefile(src: str, dst: str) -> str:
    """Clone a file or a whole directory tree. APFS clonefile(2) first, plain copy as fallback.

    Returns the method used: "clonefile" or "copy".
    """
    if _libc is not None:
        if _libc.clonefile(os.fsencode(src), os.fsencode(dst), CLONE_NOFOLLOW) == 0:
            return "clonefile"
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True)
    elif os.path.islink(src):
        os.symlink(os.readlink(src), dst)
    else:
        shutil.copy2(src, dst)
    return "copy"


def can_clonefile() -> bool:
    return _libc is not None


def stat_manifest(root: str) -> dict:
    """path -> (kind, ino, size, mtime_ns, ctime_ns, mode, link_target). Symlinks are not followed."""
    out = {}
    stack = [""]
    while stack:
        rel = stack.pop()
        try:
            it = os.scandir(os.path.join(root, rel) if rel else root)
        except FileNotFoundError:
            continue
        with it:
            for e in it:
                p = os.path.join(rel, e.name) if rel else e.name
                st = e.stat(follow_symlinks=False)
                m = st.st_mode
                if stat.S_ISLNK(m):
                    out[p] = ("link", st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, m, os.readlink(e.path))
                elif stat.S_ISDIR(m):
                    out[p] = ("dir", st.st_ino, 0, 0, 0, m, None)
                    stack.append(p)
                else:
                    out[p] = ("file", st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, m, None)
    return out


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of(root: str, rel: str, kind: str):
    p = os.path.join(root, rel)
    if kind == "link":
        return "link:" + hashlib.sha256(os.fsencode(os.readlink(p))).hexdigest()
    if kind == "dir":
        return None
    return sha256_file(p)


def fingerprint(path: str) -> dict:
    """Before/after-state fingerprint of one path (never follows a final symlink)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"exists": False}
    if stat.S_ISLNK(st.st_mode):
        return {"exists": True, "kind": "link", "link": os.readlink(path),
                "sha256": "link:" + hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()}
    if stat.S_ISDIR(st.st_mode):
        return {"exists": True, "kind": "dir", "mode": oct(stat.S_IMODE(st.st_mode))}
    return {"exists": True, "kind": "file", "sha256": sha256_file(path), "size": st.st_size,
            "mode": oct(stat.S_IMODE(st.st_mode)), "mtime_ns": st.st_mtime_ns}


def same_state(a: dict, b: dict) -> bool:
    """True when two fingerprints describe the same content (mtime is ignored)."""
    if not a.get("exists") and not b.get("exists"):
        return True
    if a.get("exists") != b.get("exists"):
        return False
    if a.get("kind") != b.get("kind"):
        return False
    if a.get("kind") == "dir":
        return True
    return a.get("sha256") == b.get("sha256")


def read_text_lines(path: str):
    try:
        if not os.path.isfile(path) or os.path.islink(path) or os.path.getsize(path) > TEXT_LIMIT:
            return None
        b = open(path, "rb").read()
        if b"\0" in b:
            return None
        return b.decode("utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None


def unified_paths(before_path, after_path, label_before, label_after):
    """Unified diff of two files (None path = absent). None when either side is not small UTF-8 text."""
    a = read_text_lines(before_path) if before_path and os.path.lexists(before_path) else []
    b = read_text_lines(after_path) if after_path and os.path.lexists(after_path) else []
    if a is None or b is None:
        return None
    return "".join(difflib.unified_diff(a, b, fromfile="a/" + label_before, tofile="b/" + label_after))


def diff_stats(diff):
    if not diff:
        return 0, 0
    plus = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    minus = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return plus, minus


def diff_effects(base: str, work: str, pre: dict, post: dict) -> list:
    """Changes in ``work`` since manifest ``pre``. ``base`` is a clone of ``work`` taken with ``pre``."""
    changed = []
    created = [p for p in post if p not in pre]
    deleted = [p for p in pre if p not in post]
    for p in post:
        if p in pre:
            a, b = pre[p], post[p]
            if a[0] != b[0]:
                deleted.append(p)
                created.append(p)
                continue
            if a[0] == "dir":
                continue
            if a[1] == b[1] and a[2] == b[2] and p.startswith(".git/objects/") and a[5] == b[5]:
                continue  # content-addressed and same inode+size: git only freshened the mtime
            if a[1:5] != b[1:5] or a[6] != b[6]:
                bs, as_ = sha256_of(base, p, a[0]), sha256_of(work, p, b[0])
                if bs != as_:
                    changed.append({"path": p, "change": "modified", "kind": b[0],
                                    "before_sha256": bs, "after_sha256": as_})
                elif a[5] != b[5]:
                    changed.append({"path": p, "change": "mode", "kind": b[0], "before_sha256": bs,
                                    "after_sha256": as_, "before_mode": oct(stat.S_IMODE(a[5])),
                                    "after_mode": oct(stat.S_IMODE(b[5]))})
            elif a[5] != b[5]:
                changed.append({"path": p, "change": "mode", "kind": b[0],
                                "before_mode": oct(stat.S_IMODE(a[5])), "after_mode": oct(stat.S_IMODE(b[5])),
                                "before_sha256": None, "after_sha256": None})
    # renames: same inode first (mv inside the clone keeps it), then same content hash
    del_by_ino = {pre[p][1]: p for p in deleted if pre[p][0] != "dir"}
    renamed, used_del, used_cre = [], set(), set()
    for p in created:
        if post[p][0] == "dir":
            continue
        src = del_by_ino.get(post[p][1])
        if src and src not in used_del and pre[src][0] == post[p][0]:
            renamed.append((src, p))
            used_del.add(src)
            used_cre.add(p)
    del_hash = {}
    for p in deleted:
        if p not in used_del and pre[p][0] != "dir":
            del_hash.setdefault(sha256_of(base, p, pre[p][0]), []).append(p)
    cre_hash = {}
    for p in created:
        if p not in used_cre and post[p][0] != "dir":
            h = sha256_of(work, p, post[p][0])
            cre_hash[p] = h
            if del_hash.get(h):
                src = del_hash[h].pop(0)
                renamed.append((src, p))
                used_del.add(src)
                used_cre.add(p)
    for src, dst in renamed:
        changed.append({"path": dst, "from": src, "change": "renamed", "kind": post[dst][0],
                        "before_sha256": sha256_of(base, src, pre[src][0]),
                        "after_sha256": sha256_of(work, dst, post[dst][0])})
    for p in created:
        if p in used_cre:
            continue
        k = post[p][0]
        if k == "dir" and any(c.startswith(p + "/") for c in created):
            continue  # implied by its contents
        changed.append({"path": p, "change": "created", "kind": k, "before_sha256": None,
                        "after_sha256": cre_hash.get(p) or sha256_of(work, p, k)})
    deleted_set = set(deleted)
    for p in deleted:
        if p in used_del:
            continue
        k = pre[p][0]
        if k == "dir" and any(d.startswith(p + "/") for d in deleted_set):
            continue  # implied by its contents; every deleted file is listed on its own
        changed.append({"path": p, "change": "deleted", "kind": k, "before_sha256": sha256_of(base, p, k),
                        "after_sha256": None})
    for c in changed:
        c["vcs_internal"] = c["path"].split("/", 1)[0] == ".git"
    changed.sort(key=lambda c: (c["vcs_internal"], c["path"]))
    return changed


def tree_hash(root: str, skip=()) -> str:
    """One hash over every entry under ``root`` (path, kind, mode, content or link target).

    Used by the tests as the filesystem instrument: equal hashes = nothing changed.
    """
    h = hashlib.sha256()
    if not os.path.lexists(root):
        return "absent"
    for rel in sorted(stat_manifest(root)):
        if any(rel == s or rel.startswith(s + "/") for s in skip):
            continue
        p = os.path.join(root, rel)
        st = os.lstat(p)
        h.update(os.fsencode(rel) + b"\0" + oct(st.st_mode).encode() + b"\0")
        if stat.S_ISLNK(st.st_mode):
            h.update(os.fsencode(os.readlink(p)))
        elif stat.S_ISREG(st.st_mode):
            h.update(sha256_file(p).encode())
        h.update(b"\n")
    return h.hexdigest()
