#!/usr/bin/env python3
"""
sandbox (sb) — version control in a single file.

version  1.0
author   jts.gg/sandbox

The repository is one SQLite database (.sb/sandbox.db); every operation
is a single atomic transaction. Objects are addressed by the SHA-256 of
their content and re-verified on every read. An append-only, hash-chained
journal records every operation; 'sb verify' re-checks the store, the
chain, and the branch tips end to end. No dependencies beyond the Python
standard library; no keys, no signatures.

Object model (zlib-compressed rows in the objects table)
  blob     raw file bytes
  tree     canonical JSON  [[mode, kind, hash, name], ...]  (sorted)
  commit   canonical JSON  {tree, parents, author, email, time, message}

Tables: meta, objects, refs, journal, statcache.
"""


import sys, os, io, json, time, zlib, hashlib, fnmatch, difflib, re
import argparse
import sqlite3, subprocess, tempfile, getpass, shutil
from pathlib import Path

VERSION = "1.0"
AUTHOR = "jts.gg/sandbox"
FORMAT_VERSION = 1
SB_DIR = ".sb"
DB_NAME = "sandbox.db"

# ---------------------------------------------------------------- output ----
# A deliberately small, calm palette: white (default / bold), gray (dim),
# and one amber accent. Red is reserved strictly for genuine failures.
def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else str(s)
def dim(s):    return _c("2", s)             # gray  — secondary / chrome
def bold(s):   return _c("1", s)             # white — names, verbs, emphasis
def amber(s):  return _c("38;5;215", s)      # amber — connectors, ids, checks
def red(s):    return _c("31", s)            # red   — errors and failures only
# on-theme aliases so every call site stays within the palette
def green(s):  return bold(s)                # success reads as bold white
def yellow(s): return amber(s)               # highlights / ids read as amber
def cyan(s):   return amber(s)               # paths / ids read as amber
RULE = "\u2500" * 34

def tree_print(lines, indent="  "):
    """Print lines under the previous message with light connector glyphs."""
    for i, line in enumerate(lines):
        conn = "\u2514\u2500\u2500\u2500 " if i == len(lines) - 1 else "\u251c\u2500\u2500\u2500 "
        print(indent + amber(conn) + line)

def leaf(line, indent="  "):
    print(indent + amber("\u2514\u2500\u2500\u2500 ") + line)

def die(msg, code=1):
    print(red("error: ") + msg, file=sys.stderr)
    sys.exit(code)

def short(h):
    return h[:10] if h else "-"

class CorruptObject(Exception):
    """An on-disk object failed its integrity check."""

class TamperedJournal(Exception):
    """The journal hash chain does not verify."""

# ------------------------------------------------------------ hashing -------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def canonical(obj) -> bytes:
    """Deterministic JSON encoding used for trees, commits and journal links."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()

def hash_obj(kind: str, data: bytes) -> str:
    return sha256_hex(f"{kind} {len(data)}\0".encode() + data)

# ------------------------------------------------------------- the store ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS objects (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS refs (
    name TEXT PRIMARY KEY,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    op     TEXT    NOT NULL,
    detail TEXT    NOT NULL,
    prev   TEXT    NOT NULL,
    link   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS statcache (
    path  TEXT PRIMARY KEY,
    size  INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    hash  TEXT NOT NULL
);
"""

def find_repo(start="."):
    p = Path(start).resolve()
    while True:
        if (p / SB_DIR / DB_NAME).is_file():
            return p
        if (p / SB_DIR).is_dir():          # legacy loose-object layout
            if (p / SB_DIR / "objects").is_dir():
                die(f"{p / SB_DIR} uses the old loose-file format.\n"
                    "       this version stores everything in one crash-safe "
                    "database.\n       re-init in a fresh folder and copy your "
                    "files in, or keep using the old sb for that repository.")
        if p.parent == p:
            return None
        p = p.parent

class Repo:
    def __init__(self, root: Path, create=False):
        self.root = root
        self.vdir = root / SB_DIR
        db_path = self.vdir / DB_NAME
        if create:
            self.vdir.mkdir(parents=True, exist_ok=False)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if create:
            with self.db:
                self.db.executescript(SCHEMA)
                repo_id = sha256_hex(os.urandom(32))[:32]
                self.db.executemany(
                    "INSERT INTO meta(key,value) VALUES(?,?)",
                    [("format", str(FORMAT_VERSION)),
                     ("repo_id", repo_id),
                     ("branch", "main"),
                     ("created", str(int(time.time())))])
                self.db.execute("INSERT INTO refs(name,hash) VALUES('main','')")
            self.journal("init", {"repo_id": repo_id})
            try:
                os.chmod(db_path, 0o600)   # private by default
            except OSError:
                pass
        fmt = self.meta("format")
        if fmt is None:
            die("this is not a sandbox database (missing metadata)")
        if int(fmt) > FORMAT_VERSION:
            die(f"repository format {fmt} is newer than this sb understands "
                f"({FORMAT_VERSION}) — upgrade sb")

    # ---- meta ----
    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))

    # ---- object store ----
    def put(self, kind: str, data: bytes) -> str:
        h = hash_obj(kind, data)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO objects(hash,kind,size,data) VALUES(?,?,?,?)",
                (h, kind, len(data), zlib.compress(data)))
        return h

    def has(self, h: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM objects WHERE hash=?", (h,)).fetchone() is not None

    def get(self, h: str):
        """Return (kind, data). KeyError if missing, CorruptObject if damaged.
        Content is re-hashed on EVERY read — corruption cannot pass silently."""
        row = self.db.execute(
            "SELECT kind, data FROM objects WHERE hash=?", (h,)).fetchone()
        if row is None:
            raise KeyError(h)
        kind, blob = row
        try:
            data = zlib.decompress(blob)
        except zlib.error:
            raise CorruptObject(f"object {short(h)} is unreadable (damaged in store)")
        if hash_obj(kind, data) != h:
            raise CorruptObject(f"object {short(h)} content does not match its hash")
        return kind, data

    def resolve(self, name_or_prefix: str):
        """Branch name, or unambiguous commit-hash prefix, -> full commit hash."""
        row = self.db.execute(
            "SELECT hash FROM refs WHERE name=?", (name_or_prefix,)).fetchone()
        if row is not None:
            return row[0] or None
        s = name_or_prefix.lower()
        if len(s) >= 4 and all(c in "0123456789abcdef" for c in s):
            rows = self.db.execute(
                "SELECT hash FROM objects WHERE kind='commit' AND hash LIKE ? "
                "LIMIT 3", (s + "%",)).fetchall()
            if len(rows) == 1:
                return rows[0][0]
            if len(rows) > 1:
                die(f"'{name_or_prefix}' is ambiguous — give more characters")
        return None

    # ---- refs / branch pointer ----
    def current_branch(self):
        return self.meta("branch")

    def branches(self):
        return [r[0] for r in self.db.execute(
            "SELECT name FROM refs ORDER BY name").fetchall()]

    def tip(self, branch):
        row = self.db.execute(
            "SELECT hash FROM refs WHERE name=?", (branch,)).fetchone()
        return (row[0] or None) if row else None

    def head_commit(self):
        return self.tip(self.current_branch())

    def update_ref(self, branch, commit_hash, op="ref"):
        """Move a branch tip. ALWAYS journaled — refs cannot move invisibly."""
        old = self.tip(branch)
        with self.db:
            self.db.execute(
                "INSERT INTO refs(name,hash) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET hash=excluded.hash",
                (branch, commit_hash or ""))
        self.journal(op, {"branch": branch, "old": old or "",
                          "new": commit_hash or ""})

    def remove_ref(self, branch):
        """Delete a branch tip. ALWAYS journaled — refs cannot vanish invisibly."""
        old = self.tip(branch)
        with self.db:
            self.db.execute("DELETE FROM refs WHERE name=?", (branch,))
        self.journal("branch-remove", {"branch": branch, "old": old or "",
                                       "new": ""})

    # ---- journal: append-only, hash-chained audit log ----
    def chain_head(self):
        row = self.db.execute(
            "SELECT link FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else self.meta("repo_id", "")

    def journal(self, op: str, detail: dict):
        prev = self.chain_head()
        ts = int(time.time())
        body = canonical({"ts": ts, "op": op, "detail": detail, "prev": prev})
        link = sha256_hex(body)
        with self.db:
            self.db.execute(
                "INSERT INTO journal(ts,op,detail,prev,link) VALUES(?,?,?,?,?)",
                (ts, op, canonical(detail).decode(), prev, link))
        return link

    def journal_entries(self):
        for seq, ts, op, detail, prev, link in self.db.execute(
                "SELECT seq,ts,op,detail,prev,link FROM journal ORDER BY seq"):
            yield {"seq": seq, "ts": ts, "op": op,
                   "detail": json.loads(detail), "prev": prev, "link": link}

    def verify_journal(self):
        """Recompute the hash chain. Returns (n_entries, head_link).
        Raises TamperedJournal at the first broken link."""
        prev = self.meta("repo_id", "")
        n, head = 0, prev
        for e in self.journal_entries():
            if e["prev"] != prev:
                raise TamperedJournal(
                    f"journal entry #{e['seq']} does not chain to the previous "
                    f"entry (edited or deleted history?)")
            body = canonical({"ts": e["ts"], "op": e["op"],
                              "detail": e["detail"], "prev": e["prev"]})
            if sha256_hex(body) != e["link"]:
                raise TamperedJournal(
                    f"journal entry #{e['seq']} content does not match its link "
                    f"(entry was modified)")
            prev = e["link"]
            head = e["link"]
            n += 1
        return n, head

    # ---- stat cache (why status is instant on large trees) ----
    def cached_hash(self, rel, size, mtime_ns):
        row = self.db.execute(
            "SELECT hash FROM statcache WHERE path=? AND size=? AND mtime=?",
            (rel, size, mtime_ns)).fetchone()
        return row[0] if row else None

    def remember(self, entries):
        if not entries:
            return
        with self.db:
            self.db.executemany(
                "INSERT INTO statcache(path,size,mtime,hash) VALUES(?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                "mtime=excluded.mtime, hash=excluded.hash", entries)

def need_repo() -> Repo:
    root = find_repo()
    if not root:
        die("not inside a sandbox repository (run 'sb init')")
    return Repo(root)

# ------------------------------------------------------------ identity ------
# sb records WHO made each save for humans reading history. There are no
# keys and no signatures — this is attribution, not authentication.
CONFIG_DIR = Path(os.environ.get("SB_HOME", Path.home() / ".config" / "sandbox"))
CONFIG_FILE = CONFIG_DIR / "profile.json"

def author():
    prof = {}
    if CONFIG_FILE.is_file():
        try:
            prof = json.loads(CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    name = os.environ.get("SB_NAME") or prof.get("name") or getpass.getuser()
    email = os.environ.get("SB_EMAIL") or prof.get("email") or f"{name}@local"
    return name, email

# ------------------------------------------------------------- ignores ------
DEFAULT_IGNORES = [SB_DIR, "*.sbox", "*.pyc", "__pycache__", ".DS_Store",
                   ".git", "node_modules"]

def load_ignores(root: Path):
    pats = list(DEFAULT_IGNORES)
    f = root / ".sbignore"
    if f.is_file():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line.rstrip("/"))
    return pats

def is_ignored(rel: str, pats) -> bool:
    parts = rel.split("/")
    for p in pats:
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*"):
            return True
        if any(fnmatch.fnmatch(part, p) for part in parts):
            return True
    return False

# --------------------------------------------------------- secret scanner ---
# What commit-time security can actually deliver: stopping credentials
# before they enter permanent history. Findings block the save by default.
SECRET_PATTERNS = [
    ("AWS access key",        re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key block",     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY( BLOCK)?-----")),
    ("GitHub token",          re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token",           re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key",        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Stripe live key",       re.compile(r"\b[rs]k_live_[0-9a-zA-Z]{20,}\b")),
    ("JWT",                   re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("generic secret assign", re.compile(r"(?i)\b(api[_-]?key|secret|passwd|password|auth[_-]?token)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]")),
]
MAX_SCAN_BYTES = 1_000_000

def scan_secrets(data: bytes):
    """Return [(line_no, label), ...] findings for one file's content."""
    if len(data) > MAX_SCAN_BYTES or b"\0" in data[:8000]:
        return []                                   # binary or huge: skip
    text = data.decode("utf-8", errors="replace")
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for label, pat in SECRET_PATTERNS:
            if pat.search(line):
                hits.append((i, label))
    return hits

# ----------------------------------------------------------- trees ----------
_BAD_NAME = re.compile(r"^\.?\.?$")   # "", ".", ".."

def safe_name(name: str) -> bool:
    """Tree entry names must be single path components. This is what stops a
    crafted tree object from writing outside the repository on checkout."""
    return bool(name) and "/" not in name and "\\" not in name \
        and "\0" not in name and not _BAD_NAME.match(name) and name != SB_DIR

RACY_WINDOW_NS = 2_000_000_000   # files touched < 2s ago bypass the stat cache

def snapshot_worktree(repo: Repo, write=True):
    """Walk the working tree -> {rel: (mode, blob_hash)}.
    write=True also stores the blobs. Uses the stat cache to avoid
    re-reading unchanged files; recently-touched files are always re-read."""
    pats = load_ignores(repo.root)
    files, cache_updates, symlinks = {}, [], 0
    now_ns = time.time_ns()
    for dirpath, dirnames, filenames in os.walk(repo.root):
        rel_dir = os.path.relpath(dirpath, repo.root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        dirnames[:] = sorted(d for d in dirnames
                             if not is_ignored((rel_dir + "/" + d).lstrip("/"), pats))
        for fn in sorted(filenames):
            rel = (rel_dir + "/" + fn).lstrip("/")
            if is_ignored(rel, pats):
                continue
            p = Path(dirpath) / fn
            if p.is_symlink():
                symlinks += 1
                continue
            st = p.stat()
            mode = "100755" if os.access(p, os.X_OK) else "100644"
            h = None
            if now_ns - st.st_mtime_ns > RACY_WINDOW_NS:
                h = repo.cached_hash(rel, st.st_size, st.st_mtime_ns)
            if h is not None and (not write or repo.has(h)):
                files[rel] = (mode, h)
                continue
            data = p.read_bytes()
            h = repo.put("blob", data) if write else hash_obj("blob", data)
            files[rel] = (mode, h)
            cache_updates.append((rel, st.st_size, st.st_mtime_ns, h))
    repo.remember(cache_updates)
    if symlinks and not write:      # the write pass repeats the walk; stay quiet
        print(dim(f"note: {symlinks} symlink(s) skipped (not tracked in v1)"))
    return files

def build_tree(repo: Repo, files: dict) -> str:
    """files: {rel: (mode, blob_hash)} -> root tree hash (nested trees)."""
    def build(prefix):
        entries, subdirs = {}, set()
        plen = len(prefix)
        for rel, (mode, h) in files.items():
            if prefix and not rel.startswith(prefix):
                continue
            rest = rel[plen:]
            if "/" in rest:
                subdirs.add(rest.split("/", 1)[0])
            else:
                entries[rest] = [mode, "blob", h]
        for sub in subdirs:
            entries[sub] = ["040000", "tree", build(prefix + sub + "/")]
        rows = [[m, k, h, name] for name, (m, k, h) in sorted(entries.items())]
        return repo.put("tree", canonical(rows))
    return build("")

def read_tree(repo: Repo, tree_hash: str, prefix="") -> dict:
    """Flatten a tree object to {rel: (mode, blob_hash)}. Validates names."""
    out = {}
    kind, data = repo.get(tree_hash)
    if kind != "tree":
        raise CorruptObject(f"{short(tree_hash)} is a {kind}, not a tree")
    for mode, k, h, name in json.loads(data or b"[]"):
        if not safe_name(name):
            raise CorruptObject(
                f"tree {short(tree_hash)} contains unsafe entry name {name!r}")
        if k == "tree":
            out.update(read_tree(repo, h, prefix + name + "/"))
        else:
            out[prefix + name] = (mode, h)
    return out

# ----------------------------------------------------------- commits --------
def make_commit(repo: Repo, tree_hash, parents, message) -> str:
    name, email = author()
    c = {"tree": tree_hash, "parents": list(parents), "author": name,
         "email": email, "time": int(time.time()), "message": message}
    return repo.put("commit", canonical(c))

def parse_commit(repo: Repo, h: str) -> dict:
    kind, data = repo.get(h)
    if kind != "commit":
        die(f"{short(h)} is a {kind}, not a save")
    c = json.loads(data)
    c["hash"] = h
    return c

def walk_history(repo: Repo, start: str):
    seen, stack = set(), [start]
    while stack:
        h = stack.pop()
        if h in seen:
            continue
        seen.add(h)
        c = parse_commit(repo, h)
        yield c
        stack.extend(c["parents"])

def head_tree_files(repo):
    head = repo.head_commit()
    if not head:
        return {}, None
    c = parse_commit(repo, head)
    return read_tree(repo, c["tree"]), c

def worktree_vs_tree(work, tree):
    """-> (added, modified, deleted) sorted rel-path lists."""
    added    = sorted(p for p in work if p not in tree)
    deleted  = sorted(p for p in tree if p not in work)
    modified = sorted(p for p in work if p in tree and work[p] != tree[p])
    return added, modified, deleted

# ----------------------------------------------------- checkout / cleanup ---
def checkout_tree(repo: Repo, target: dict, current: dict):
    """Make the worktree equal `target`. Writes are atomic (tmp + replace)."""
    for rel in current:
        if rel not in target:
            p = repo.root / rel
            if p.exists() or p.is_symlink():
                p.unlink()
    for rel, (mode, h) in target.items():
        p = repo.root / rel
        cur = current.get(rel)
        if cur == (mode, h) and p.exists():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".sbtmp")
        tmp.write_bytes(repo.get(h)[1])
        os.chmod(tmp, 0o755 if mode == "100755" else 0o644)
        os.replace(tmp, p)
    # prune directories emptied by deletions (never touches .sb itself)
    sbdir = repo.vdir.resolve()
    for dirpath, dirnames, filenames in os.walk(repo.root, topdown=False):
        d = Path(dirpath).resolve()
        if d == repo.root.resolve() or d == sbdir or sbdir in d.parents:
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass

def ensure_clean(repo):
    work = snapshot_worktree(repo, write=False)
    tree, _ = head_tree_files(repo)
    a, m, d = worktree_vs_tree(work, tree)
    if a or m or d:
        die("you have unsaved changes — run 'sb save' first (nothing is ever\n"
            "       silently discarded), or 'sb restore <file>' to drop them")

# ------------------------------------------------------- three-way merge ----
def _diff_regions(base, side):
    """Changed regions of `side` relative to `base`:
    [(base_start, base_end, replacement_lines), ...] in base coordinates."""
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, base, side, autojunk=False).get_opcodes():
        if tag != "equal":
            out.append((i1, i2, side[j1:j2]))
    return out

def _apply_regions(base, s, e, regions):
    out, pos = [], s
    for i1, i2, new in regions:
        out.extend(base[pos:i1])
        out.extend(new)
        pos = i2
    out.extend(base[pos:e])
    return out

def merge3(base, ours, theirs):
    """Line-level three-way merge. -> (merged_lines, n_conflicts).
    Non-overlapping changes merge cleanly; overlapping changes become
    conflict-marked blocks. Deliberately conservative: touching hunks and
    same-point insertions are treated as conflicts (a false conflict is
    safe; a false merge is not)."""
    ca, cb = _diff_regions(base, ours), _diff_regions(base, theirs)
    out, conflicts = [], 0
    ia = ib = pos = 0
    while ia < len(ca) or ib < len(cb):
        ra = ca[ia] if ia < len(ca) else None
        rb = cb[ib] if ib < len(cb) else None
        if rb is None or (ra is not None and ra[1] < rb[0]):
            out.extend(base[pos:ra[0]]); out.extend(ra[2])
            pos = ra[1]; ia += 1
        elif ra is None or rb[1] < ra[0]:
            out.extend(base[pos:rb[0]]); out.extend(rb[2])
            pos = rb[1]; ib += 1
        else:                                   # overlapping change group
            s = min(ra[0], rb[0]); e = max(ra[1], rb[1])
            ga, gb = [ra], [rb]
            ia += 1; ib += 1
            grew = True
            while grew:
                grew = False
                while ia < len(ca) and ca[ia][0] <= e:
                    e = max(e, ca[ia][1]); ga.append(ca[ia]); ia += 1; grew = True
                while ib < len(cb) and cb[ib][0] <= e:
                    e = max(e, cb[ib][1]); gb.append(cb[ib]); ib += 1; grew = True
            out.extend(base[pos:s])
            a_txt = _apply_regions(base, s, e, ga)
            b_txt = _apply_regions(base, s, e, gb)
            if a_txt == b_txt:
                out.extend(a_txt)
            else:
                conflicts += 1
                out.append("<<<<<<< ours")
                out.extend(a_txt)
                out.append("=======")
                out.extend(b_txt)
                out.append(">>>>>>> theirs")
            pos = e
    out.extend(base[pos:])
    return out, conflicts

def _blob_lines(repo, h):
    """Blob -> text lines, or None if binary."""
    if h is None:
        return []
    data = repo.get(h)[1]
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace").splitlines()

# ---------------------------------------------------------- test gates ------
# Versioned test scripts live in sb-tests/<stage>/ so they travel with
# branches and are themselves history. Stages:
#   pre-save    gates every save          (sb save)
#   pre-merge   gates merges, incl. FFs   (sb merge)
#   pre-deploy  gates deployments         (sb deploy)
# Scripts run sorted (name them 10-lint.sh, 20-unit.py, ...) inside a
# PRISTINE temp checkout of the exact candidate tree — never your dirty
# worktree — with SB_STAGE / SB_BRANCH / SB_COMMIT / SB_REPO exported.
# Non-zero exit or timeout blocks the operation; --no-verify overrides.
TESTS_DIR = "sb-tests"
STAGES = ("pre-save", "pre-merge", "pre-deploy")
TEST_TIMEOUT = int(os.environ.get("SB_TEST_TIMEOUT", "120"))

def _runner_for(path: Path):
    if path.suffix == ".py":
        return [sys.executable, str(path)]
    if os.access(path, os.X_OK):
        return [str(path)]
    return ["sh", str(path)]

def _materialize(repo: Repo, files: dict, dest: Path, from_worktree: bool):
    for rel, (mode, h) in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = (repo.root / rel).read_bytes() if from_worktree else repo.get(h)[1]
        p.write_bytes(data)
        os.chmod(p, 0o755 if mode == "100755" else 0o644)

def discover_tests(root: Path, stage: str):
    d = root / TESTS_DIR / stage
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and not p.name.startswith("."))

def run_stage(repo: Repo, stage: str, files: dict, *,
              from_worktree=False, commit="(worktree)", quiet_if_empty=True):
    """Run one gate against the candidate tree. True = gate passes."""
    with tempfile.TemporaryDirectory(prefix="sb-test-") as tmp:
        tmpdir = Path(tmp)
        _materialize(repo, files, tmpdir, from_worktree)
        # discover from the CANDIDATE tree so merges run the merged
        # result's own tests, not the current worktree's
        scripts = discover_tests(tmpdir, stage)
        if not scripts:
            if not quiet_if_empty:
                print(dim(f"no {stage} tests (add one: sb test new {stage} <name>)"))
            return True
        env = dict(os.environ, SB_STAGE=stage, SB_BRANCH=repo.current_branch(),
                   SB_COMMIT=str(commit), SB_REPO=str(repo.root))
        print(bold(stage) + dim(f" · {len(scripts)} test(s) · clean checkout · "
                                f"{TEST_TIMEOUT}s timeout"))
        failed = 0
        for s in scripts:
            t0 = time.time()
            try:
                r = subprocess.run(_runner_for(s), cwd=tmpdir, env=env,
                                   capture_output=True, text=True,
                                   timeout=TEST_TIMEOUT)
                ok, note = r.returncode == 0, f"exit {r.returncode}"
                output = (r.stdout + r.stderr).strip()
            except subprocess.TimeoutExpired as e:
                ok, note = False, f"TIMEOUT after {TEST_TIMEOUT}s"
                output = ((e.stdout or b"").decode(errors="replace")
                          + (e.stderr or b"").decode(errors="replace")).strip()
            dt = time.time() - t0
            mark = green("pass") if ok else red("FAIL")
            print("  " + amber("\u251c\u2500\u2500\u2500 ") + f"{mark}  {s.name}  "
                  + dim(f"({dt:.2f}s)") + ("" if ok else "  " + red(note)))
            if not ok:
                failed += 1
                for line in output.splitlines()[-15:]:
                    print("  " + amber("\u2502") + "    " + dim(line))
        if failed:
            leaf(red(f"{failed}/{len(scripts)} test(s) failed"))
            return False
        leaf(green(f"all {len(scripts)} test(s) passed"))
        return True

TEST_TEMPLATE_SH = """#!/bin/sh
# {name} — {stage} gate for sandbox (sb)
# Runs inside a clean checkout of the candidate tree (cwd = checkout root).
# Env: SB_STAGE, SB_BRANCH, SB_COMMIT, SB_REPO.  Exit 0 = pass, non-zero = block.
set -eu

echo "[{name}] checking $SB_BRANCH @ $SB_COMMIT"
# --- your checks here, e.g.: ---
# python3 -m py_compile $(find . -name '*.py' -not -path './sb-tests/*')
# ./run_unit_tests.sh
exit 0
"""

TEST_TEMPLATE_PY = """#!/usr/bin/env python3
# {name} — {stage} gate for sandbox (sb).
# Runs inside a clean checkout of the candidate tree (cwd = checkout root).
# Env: SB_STAGE, SB_BRANCH, SB_COMMIT, SB_REPO. Exit 0 = pass.
import os, sys

print("[{name}] checking", os.environ["SB_BRANCH"], "@", os.environ["SB_COMMIT"])
# --- your checks here ---
sys.exit(0)
"""

# ------------------------------------------------------------ commands ------
def cmd_init(args):
    root = Path(".").resolve()
    if (root / SB_DIR).exists():
        die("repository already exists here")
    repo = Repo(root, create=True)
    name, email = author()
    print(f"initialized sandbox on branch {bold('main')}")
    tree_print([
        dim("store   ") + str(repo.vdir / DB_NAME),
        dim("author  ") + f"{name} <{email}>  " + dim("(change: sb who <name> <email>)"),
    ])

def cmd_status(args):
    repo = need_repo()
    work = snapshot_worktree(repo, write=False)
    tree, _ = head_tree_files(repo)
    added, modified, deleted = worktree_vs_tree(work, tree)
    head = repo.head_commit()
    print(f"on branch {bold(repo.current_branch())}"
          + (f" {dim('·')} head {amber(short(head))}" if head
             else dim("  (no saves yet)")))
    if not (added or modified or deleted):
        leaf("working tree clean " + dim("— nothing to save"))
        return
    rows  = [dim("new       ") + p for p in added]
    rows += [dim("modified  ") + p for p in modified]
    rows += [dim("deleted   ") + dim(p) for p in deleted]
    tree_print(rows)
    print(dim(f"run 'sb save \"message\"' to snapshot "
              f"{len(added)+len(modified)+len(deleted)} change(s)"))

def cmd_save(args):
    repo = need_repo()
    if not args.message:
        die('a message is required:  sb save "what changed"')
    work = snapshot_worktree(repo, write=False)
    tree_files, head_c = head_tree_files(repo)
    added, modified, deleted = worktree_vs_tree(work, tree_files)
    if not (added or modified or deleted) and head_c:
        print(green("nothing changed — no save created"))
        return
    # secret scan: only files being introduced or changed by THIS save
    if not args.allow_secrets:
        findings = []
        for rel in added + modified:
            for line_no, label in scan_secrets((repo.root / rel).read_bytes()):
                findings.append((rel, line_no, label))
        if findings:
            print(red("save blocked — possible secrets detected"))
            tree_print([red(f"{rel}:{line_no}  {label}")
                        for rel, line_no, label in findings])
            print(dim("history is permanent; remove the secret, add the file\n"
                      "to .sbignore, or override deliberately with --allow-secrets"))
            sys.exit(2)
    if not args.no_verify:
        if not run_stage(repo, "pre-save", work, from_worktree=True):
            die("pre-save tests failed — save blocked (--no-verify to override)")
    work = snapshot_worktree(repo, write=True)          # now persist blobs
    tree_hash = build_tree(repo, work)
    parents = [head_c["hash"]] if head_c else []
    h = make_commit(repo, tree_hash, parents, args.message)
    repo.update_ref(repo.current_branch(), h, op="save")
    n = len(added) + len(modified) + len(deleted)
    print(f"{bold('saved')} {amber(short(h))} "
          f"{dim('on')} {bold(repo.current_branch())} {dim('·')} {dim(str(n) + ' file(s)')}")
    leaf(f'"{args.message}"')

def cmd_log(args):
    repo = need_repo()
    head = repo.head_commit()
    if not head:
        print(dim("no saves yet"))
        return
    count = 0
    for c in walk_history(repo, head):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["time"]))
        merge = dim("  (merge)") if len(c["parents"]) > 1 else ""
        print(f"{amber(short(c['hash']))}  {dim(when)}  {c['author']} "
              f"{dim('<' + c['email'] + '>')}{merge}")
        tree_print(c["message"].splitlines() or ['""'])
        count += 1
        if args.limit and count >= args.limit:
            break

def cmd_diff(args):
    repo = need_repo()
    tree, _ = head_tree_files(repo)
    work = snapshot_worktree(repo, write=False)
    added, modified, deleted = worktree_vs_tree(work, tree)
    targets = added + modified + deleted
    if args.path:
        want = args.path.rstrip("/")
        targets = [t for t in targets if t == want or t.startswith(want + "/")]
    if not targets:
        print(dim("no differences"))
        return
    for rel in sorted(targets):
        old = (repo.get(tree[rel][1])[1].decode("utf-8", "replace").splitlines()
               if rel in tree else [])
        new = ((repo.root / rel).read_text(errors="replace").splitlines()
               if rel in work else [])
        for line in difflib.unified_diff(old, new, fromfile=f"saved/{rel}",
                                         tofile=f"work/{rel}", lineterm=""):
            if line.startswith("+") and not line.startswith("+++"):
                print(bold(line))
            elif line.startswith("-") and not line.startswith("---"):
                print(dim(line))
            elif line.startswith("@@"):
                print(amber(line))
            else:
                print(line)

def cmd_restore(args):
    repo = need_repo()
    tree, _ = head_tree_files(repo)
    rel = args.path.rstrip("/")
    matches = [rel] if rel in tree else \
              [p for p in tree if p.startswith(rel + "/")]
    if not matches:
        die(f"'{rel}' is not in the last save")
    for m in matches:
        mode, h = tree[m]
        p = repo.root / m
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(repo.get(h)[1])
        os.chmod(p, 0o755 if mode == "100755" else 0o644)
    what = cyan(rel) if len(matches) == 1 else f"{len(matches)} file(s) under {rel}/"
    print(f"restored {what} from last save")

def cmd_undo(args):
    """Non-destructive undo: a NEW save whose content equals the previous
    save. History is never rewritten; run undo again to redo."""
    repo = need_repo()
    head = repo.head_commit()
    if not head:
        die("nothing to undo")
    ensure_clean(repo)
    c = parse_commit(repo, head)
    if not c["parents"]:
        die("cannot undo the very first save")
    parent = parse_commit(repo, c["parents"][0])
    checkout_tree(repo, read_tree(repo, parent["tree"]),
                  read_tree(repo, c["tree"]))
    msg = c["message"].splitlines()[0]
    h = make_commit(repo, parent["tree"], [head], f"undo: {msg}")
    repo.update_ref(repo.current_branch(), h, op="undo")
    print(f"{bold('undone')} {dim('— created')} {amber(short(h))}")
    leaf(f'reverts "{msg}"  '
         + dim("(history preserved; sb undo again to redo)"))

def cmd_branch(args):
    repo = need_repo()
    if not args.name:
        cur = repo.current_branch()
        branches = repo.branches()
        print(f"{len(branches)} branch(es)")
        tree_print([(amber("* ") if b == cur else "  ")
                    + (bold(b) if b == cur else b)
                    + "  " + amber(short(repo.tip(b)))
                    for b in branches])
        return
    if args.remove:
        if args.name not in repo.branches():
            die(f"no branch named '{args.name}'")
        if args.name == repo.current_branch():
            die(f"'{args.name}' is the current branch — switch away first")
        if len(repo.branches()) == 1:
            die("cannot remove the last branch")
        tip = repo.tip(args.name)
        repo.remove_ref(args.name)
        print(f"{dim('removed branch')} {bold(args.name)} "
              f"{dim('(was at')} {amber(short(tip))}{dim(')')}")
        leaf(dim("its saves stay in history — nothing was deleted from the store"))
        return
    if not safe_name(args.name) or args.name.startswith("-"):
        die(f"'{args.name}' is not a valid branch name")
    if args.name in repo.branches():
        die(f"branch '{args.name}' already exists")
    head = repo.head_commit()
    repo.update_ref(args.name, head, op="branch")
    print(f"{dim('created branch')} {bold(args.name)} {dim('at')} {amber(short(head))}")
    leaf(dim(f"switch to it: sb switch {args.name}"))

def cmd_switch(args):
    repo = need_repo()
    if args.target not in repo.branches():
        die(f"no branch named '{args.target}' "
            f"(sandbox has no detached mode; create it first: sb branch {args.target})")
    if args.target == repo.current_branch():
        print(f"already on {bold(args.target)}")
        return
    ensure_clean(repo)
    cur_tree, _ = head_tree_files(repo)
    target_commit = repo.tip(args.target)
    target_tree = (read_tree(repo, parse_commit(repo, target_commit)["tree"])
                   if target_commit else {})
    checkout_tree(repo, target_tree, cur_tree)
    repo.set_meta("branch", args.target)
    repo.journal("switch", {"to": args.target, "tip": target_commit or ""})
    print(f"{dim('switched to')} {bold(args.target)} {amber(short(target_commit))}")

def find_merge_base(repo, a, b):
    anc = {c["hash"] for c in walk_history(repo, a)}
    for c in walk_history(repo, b):
        if c["hash"] in anc:
            return c["hash"]
    return None

def cmd_merge(args):
    repo = need_repo()
    theirs_tip = repo.resolve(args.branch)
    if theirs_tip is None:
        die(f"unknown branch '{args.branch}'")
    ours_tip = repo.head_commit()
    if not ours_tip:
        die("current branch has no saves")
    if theirs_tip == ours_tip:
        print(green("already up to date")); return
    ensure_clean(repo)
    base = find_merge_base(repo, ours_tip, theirs_tip)
    ours_tree = read_tree(repo, parse_commit(repo, ours_tip)["tree"])
    if base == ours_tip:                                   # fast-forward
        theirs_tree = read_tree(repo, parse_commit(repo, theirs_tip)["tree"])
        if not args.no_verify:
            if not run_stage(repo, "pre-merge", theirs_tree, commit=theirs_tip):
                die("pre-merge tests failed — merge blocked (--no-verify to override)")
        checkout_tree(repo, theirs_tree, ours_tree)
        repo.update_ref(repo.current_branch(), theirs_tip, op="merge")
        print(f"{bold('fast-forwarded')} {dim('to')} {amber(short(theirs_tip))}")
        return
    if base == theirs_tip:
        print(green("already contains that branch")); return
    base_tree = read_tree(repo, parse_commit(repo, base)["tree"]) if base else {}
    theirs_tree = read_tree(repo, parse_commit(repo, theirs_tip)["tree"])
    merged, conflicts, auto_merged = {}, [], []
    for rel in sorted(set(base_tree) | set(ours_tree) | set(theirs_tree)):
        b = base_tree.get(rel); o = ours_tree.get(rel); t = theirs_tree.get(rel)
        if o == t:                        merged[rel] = o
        elif t == b:                      merged[rel] = o     # only we changed
        elif o == b:                      merged[rel] = t     # only they changed
        elif o is None or t is None:      conflicts.append((rel, "changed vs deleted"))
        else:
            # both sides changed the same file: try a line-level 3-way merge
            bl = _blob_lines(repo, b[1] if b else None)
            ol = _blob_lines(repo, o[1])
            tl = _blob_lines(repo, t[1])
            if bl is None or ol is None or tl is None:
                conflicts.append((rel, "binary file changed on both branches"))
                continue
            lines, n = merge3(bl, ol, tl)
            if n:
                conflicts.append((rel, f"{n} overlapping change(s)"))
                continue
            mode = o[0] if o[0] != b[0] or t[0] == b[0] else t[0]
            merged[rel] = (mode, repo.put("blob",
                           ("\n".join(lines) + "\n").encode()))
            auto_merged.append(rel)
    if conflicts:
        print(red("merge stopped — these files conflict"))
        tree_print([red(rel) + dim(f"  ({why})") for rel, why in conflicts])
        print(dim("sb never half-merges your worktree: reconcile the files on\n"
                  "one branch (copy the other side's version or combine them\n"
                  "by hand), save, then merge again"))
        sys.exit(2)
    merged = {k: v for k, v in merged.items() if v is not None}
    if not args.no_verify:
        if not run_stage(repo, "pre-merge", merged,
                         commit=f"merge({short(ours_tip)},{short(theirs_tip)})"):
            die("pre-merge tests failed on the merged tree — merge blocked\n"
                "       (fix on a branch and re-merge, or --no-verify to override)")
    checkout_tree(repo, merged, ours_tree)
    tree_hash = build_tree(repo, merged)
    h = make_commit(repo, tree_hash, [ours_tip, theirs_tip],
                    f"merge {args.branch} into {repo.current_branch()}")
    repo.update_ref(repo.current_branch(), h, op="merge")
    print(f"{bold('merged')} {args.branch} {dim('→')} {bold(repo.current_branch())} "
          f"{dim('as')} {amber(short(h))}")
    if auto_merged:
        leaf(dim(f"{len(auto_merged)} file(s) auto-merged line by line"))

TEST_GUIDE = f"""\
{bold('setting up test scripts')}
{amber(RULE)}

Tests are plain executable scripts inside {bold('sb-tests/<stage>/')} in your
repo. Any language works — sb only cares about the {bold('exit code')}:
exit {bold('0')} means pass, anything else means fail.

{amber('stages')}
  {amber('\u251c\u2500\u2500\u2500')} pre-save    {dim('runs before every save — the gate for bad snapshots')}
  {amber('\u251c\u2500\u2500\u2500')} pre-merge   {dim('runs before a merge is committed')}
  {amber('\u2514\u2500\u2500\u2500')} pre-deploy  {dim('runs before a deploy is recorded')}

{amber('quick start')}
  {amber('\u251c\u2500\u2500\u2500')} sb test new pre-save smoke     {dim('scaffold sb-tests/pre-save/smoke.sh')}
  {amber('\u251c\u2500\u2500\u2500')} $EDITOR sb-tests/pre-save/smoke.sh
  {amber('\u251c\u2500\u2500\u2500')} sb test                        {dim('run every stage now')}
  {amber('\u2514\u2500\u2500\u2500')} sb save "msg"                  {dim('gates now run automatically')}

{amber('how they run')}
  {amber('\u251c\u2500\u2500\u2500')} each script runs in a {bold('pristine temp checkout')} of HEAD —
  {amber('\u2502')}    never your working folder, so tests cannot dirty your files
  {amber('\u251c\u2500\u2500\u2500')} env vars provided: {dim('SB_STAGE · SB_BRANCH · SB_COMMIT · SB_REPO')}
  {amber('\u251c\u2500\u2500\u2500')} timeout {dim('per script:')} {TEST_TIMEOUT}s {dim('(override with SB_TEST_TIMEOUT)')}
  {amber('\u2514\u2500\u2500\u2500')} skip once with --no-verify {dim('(the skip is journaled)')}

{amber('example')}  {dim('sb-tests/pre-save/smoke.sh')}
  #!/bin/sh
  python3 -m py_compile app.py || exit 1
  ./app.py --self-check        || exit 1
  exit 0
"""

def cmd_test(args):
    repo = need_repo()
    sub = args.args
    if sub and sub[0] in ("guide", "help"):
        print(TEST_GUIDE)
        return
    if sub and sub[0] == "new":
        if len(sub) != 3 or sub[1] not in STAGES:
            die(f"usage: sb test new <{'|'.join(STAGES)}> <name>")
        stage, name = sub[1], sub[2]
        if "." not in name:
            name += ".sh"
        path = repo.root / TESTS_DIR / stage / name
        if path.exists():
            die(f"{path.relative_to(repo.root)} already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        tpl = TEST_TEMPLATE_PY if name.endswith(".py") else TEST_TEMPLATE_SH
        path.write_text(tpl.format(name=name, stage=stage))
        os.chmod(path, 0o755)
        print(f"created {cyan(str(path.relative_to(repo.root)))}")
        leaf(dim(f"edit it — it runs automatically at {stage}"))
        return
    if sub and sub[0] == "list":
        found = False
        for stage in STAGES:
            scripts = discover_tests(repo.root, stage)
            if scripts:
                found = True
                print(bold(stage))
                tree_print([s.name for s in scripts])
        if not found:
            print(dim("no tests yet — scaffold one:  sb test new pre-merge <name>"))
        return
    stages = [sub[0]] if sub else list(STAGES)
    for st in stages:
        if st not in STAGES:
            die(f"unknown stage '{st}' (choose from: {', '.join(STAGES)})")
    work = snapshot_worktree(repo, write=False)
    ok = True
    for st in stages:
        ok &= run_stage(repo, st, work, from_worktree=True,
                        quiet_if_empty=not sub)
    sys.exit(0 if ok else 2)

def cmd_deploy(args):
    if args.label == "list":               # word form of --list, like 'sb test list'
        args.list, args.label = True, None
    repo = need_repo()
    if args.list:
        recs = [e for e in repo.journal_entries() if e["op"] == "deploy"]
        if not recs:
            print(dim("no deployments recorded")); return
        try:
            repo.verify_journal()
            chain = "journal chain ok " + amber("\u2713")
        except TamperedJournal as e:
            chain = red("JOURNAL TAMPERED: " + str(e))
        print(f"{len(recs)} deployment(s)")
        tree_print([f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(e['ts']))}  "
                    f"{amber(short(e['detail']['commit']))}  "
                    f"{bold(e['detail']['label'])}  on {e['detail']['branch']}  "
                    + dim(f"by {e['detail']['author']}") for e in recs])
        print(dim("record integrity: ") + chain)
        return
    head = repo.head_commit()
    if not head:
        die("nothing to deploy — no saves yet")
    ensure_clean(repo)
    print(bold("gate 1/2") + dim(" · full store verification"))
    if not _verify(repo, quiet=True):
        die("verification failed — refusing to deploy from a damaged store\n"
            "       (run 'sb verify' for the full report)")
    leaf("store intact " + amber("\u2713"))
    c = parse_commit(repo, head)
    tree = read_tree(repo, c["tree"])
    print(bold("gate 2/2") + dim(" · pre-deploy tests on the HEAD tree"))
    if not run_stage(repo, "pre-deploy", tree, commit=head, quiet_if_empty=False):
        if not args.no_verify:
            die("pre-deploy tests failed — deployment blocked (--no-verify to override)")
        print(yellow("tests failed but --no-verify given — proceeding"))
    name, email = author()
    link = repo.journal("deploy", {
        "commit": head, "branch": repo.current_branch(),
        "label": args.label or "deploy", "author": f"{name} <{email}>"})
    print(f"{bold('deployed')} {amber(short(head))} {dim('as')} "
          f"{bold(args.label or 'deploy')}")
    leaf(dim("journaled · anchor ") + amber(link[:16])
         + dim(f"  (list: sb deploy --list · get files: "
               f"sb export {args.label or 'deploy'})"))

def _verify(repo, quiet=False, anchor=None):
    """Full store verification. Returns True if everything checks out.
    Problems are tagged with a category so the summary never guesses."""
    problems = []          # list of (category, message)
    def flag(cat, msg): problems.append((cat, msg))
    objects = 0
    seen_trees, seen_blobs, seen_commits = set(), set(), set()

    def check_tree(th):
        nonlocal objects
        if th in seen_trees:
            return
        seen_trees.add(th)
        objects += 1
        try:
            entries = json.loads(repo.get(th)[1] or b"[]")   # get() re-hashes
        except CorruptObject as e:
            flag("object", str(e)); return
        for mode, kind, h, name in entries:
            if not safe_name(name):
                flag("object", f"UNSAFE NAME {name!r} in tree {short(th)}")
                continue
            if kind == "tree":
                check_tree(h)
            elif h not in seen_blobs:
                seen_blobs.add(h)
                objects += 1
                try:
                    repo.get(h)
                except KeyError:
                    flag("object", f"missing blob {short(h)} ({name})")
                except CorruptObject as e:
                    flag("object", f"{e} ({name})")

    # 1. every object reachable from every branch, re-hashed
    for b in repo.branches():
        tip = repo.tip(b)
        if not tip:
            continue
        try:
            for c in walk_history(repo, tip):
                if c["hash"] in seen_commits:
                    continue
                seen_commits.add(c["hash"])
                objects += 1
                check_tree(c["tree"])
        except (CorruptObject, KeyError) as e:
            flag("object", f"branch {b}: {e}")

    # 2. the journal hash chain, end to end
    chain_ok, head_link, n_entries = True, None, 0
    try:
        n_entries, head_link = repo.verify_journal()
    except TamperedJournal as e:
        chain_ok = False
        flag("journal", str(e))

    # 3. branch tips must match what the journal last recorded — a ref
    #    edited behind sb's back (e.g. direct SQL) is caught here
    expected = {}
    for e in repo.journal_entries():
        if e["op"] in ("save", "merge", "undo", "branch", "ref"):
            d = e["detail"]
            expected[d["branch"]] = d["new"]
        elif e["op"] == "branch-remove":
            expected.pop(e["detail"]["branch"], None)
    for b in repo.branches():
        cur = repo.tip(b) or ""
        if b in expected and expected[b] != cur:
            flag("refs", f"branch '{b}' points at {short(cur)} but the "
                         f"journal last recorded {short(expected[b])} "
                         f"(moved outside sb?)")
    for b in expected:
        if b not in repo.branches():
            flag("refs", f"branch '{b}' exists in the journal but was "
                         f"removed from refs outside sb")

    # 4. optional external anchor: is this (prefix of a) link in the chain?
    #    Anchors are 16-hex prefixes (64 bits) — short enough to jot down and
    #    paste back, far too long for a forged entry to collide with.
    anchor_ok = False
    if anchor:
        a = anchor.strip().lower().rstrip(".\u2026")   # forgive a pasted ellipsis
        if not re.fullmatch(r"[0-9a-f]{8,64}", a):
            die("an anchor is 8-64 hex characters (sb prints 16) — "
                f"got {anchor!r}")
        links = {e["link"] for e in repo.journal_entries()}
        links.add(repo.meta("repo_id"))
        if any(l.startswith(a) for l in links):
            anchor_ok = True
        else:
            flag("journal", f"anchor {a[:16]} is NOT in the journal "
                            f"chain (history was replaced wholesale?)")

    if not quiet:
        cats = {c for c, _ in problems}
        print(f"checked {bold(str(objects))} {dim('objects across')} "
              f"{bold(str(len(seen_commits)))} {dim('save(s)')}")
        rows = ["content hashes  " + (red("CORRUPTION FOUND")
                if "object" in cats else "all valid " + amber("\u2713")),
                "journal chain   " + (f"{n_entries} entries linked " + amber("\u2713")
                if chain_ok else red("BROKEN")),
                "branch tips     " + (red("MISMATCH vs journal")
                if "refs" in cats else "match the journal " + amber("\u2713"))]
        if anchor_ok:
            rows.append("anchor check    "
                        + amber(anchor.strip().lower()[:16]) + " found "
                        + amber("\u2713"))
        if head_link:
            rows.append("anchor          " + amber(head_link[:16])
                        + dim("  (save it · check later: sb verify --anchor)"))
        tree_print(rows)
        for _, p in problems:
            print(red("  ! " + p))
    return not problems

def cmd_verify(args):
    repo = need_repo()
    if _verify(repo, anchor=getattr(args, "anchor", None)):
        print("history is intact " + amber("\u2713")
              + dim(" — store, journal and refs all agree"))
    else:
        sys.exit(2)

def cmd_journal(args):
    repo = need_repo()
    try:
        n, head = repo.verify_journal()
        status = f"chain verified {amber(chr(0x2713))} {dim(f'({n} entries)')}"
    except TamperedJournal as e:
        status = red(f"CHAIN BROKEN: {e}")
    entries = list(repo.journal_entries())
    if args.limit:
        entries = entries[-args.limit:]
    for e in entries:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
        d = e["detail"]
        if e["op"] in ("save", "merge", "undo", "branch", "ref"):
            what = f"{d.get('branch','')}: {short(d.get('old',''))} → {short(d.get('new',''))}"
        elif e["op"] == "switch":
            what = f"to {d.get('to','')}"
        elif e["op"] == "deploy":
            what = f"{d.get('label','')} @ {short(d.get('commit',''))}"
        else:
            what = ""
        print(f"{dim('#%-4d' % e['seq'])} {when}  {bold('%-7s' % e['op'])} "
              f"{what}  {amber(e['link'][:16])}")
    leaf(status)

def cmd_info(args):
    repo = need_repo()
    counts = dict(repo.db.execute(
        "SELECT kind, COUNT(*) FROM objects GROUP BY kind").fetchall())
    raw = repo.db.execute("SELECT COALESCE(SUM(size),0) FROM objects").fetchone()[0]
    db_size = (repo.vdir / DB_NAME).stat().st_size
    n_journal = repo.db.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    name, email = author()
    print(f"{dim('repository')} {bold(str(repo.root))}")
    tree_print([
        f"version  sb {VERSION}  " + dim(f"· {AUTHOR}"),
        f"store    {repo.vdir / DB_NAME}  "
        + dim(f"({db_size:,} bytes on disk · {raw:,} bytes of content)"),
        f"branch   {bold(repo.current_branch())}  "
        + dim(f"of {len(repo.branches())}"),
        f"objects  {counts.get('commit',0)} save(s) · "
        f"{counts.get('tree',0)} tree(s) · {counts.get('blob',0)} blob(s)",
        f"journal  {n_journal} entries · anchor "
        + amber(repo.chain_head()[:16]),
        f"you      {name} <{email}>  "
        + dim("(attribution only — no keys, no signatures)"),
    ])

def cmd_who(args):
    if args.name:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        prof = {"name": args.name}
        if args.email:
            prof["email"] = args.email
        CONFIG_FILE.write_text(json.dumps(prof, indent=2))
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
    name, email = author()
    print(f"saves are recorded as {bold(name)} <{email}>")
    leaf(dim(f"config {CONFIG_FILE}  ·  env SB_NAME / SB_EMAIL override"))

def cmd_ignore(args):
    repo = need_repo()
    f = repo.root / ".sbignore"
    existing = f.read_text().splitlines() if f.is_file() else []
    if args.pattern in existing:
        print(dim(f"'{args.pattern}' already ignored")); return
    with open(f, "a") as fh:
        fh.write(args.pattern + "\n")
    print(f"ignoring {cyan(args.pattern)}")
    leaf(dim(".sbignore updated"))

# ------------------------------------------------------- portable archive ---
# 'sb pack' seals the entire repository (the single sandbox.db) plus a small
# manifest into one encrypted .sbox file; 'sb unpack' reverses it. Encryption
# is provided by vox (embedded below), so both commands work fully offline.
SBOX_MAGIC = b"SBOX"
SBOX_VERSION = 1

# vox v1.7.3 (jts.gg/vox) — embedded verbatim so pack/unpack work fully
# offline. It is loaded into an in-memory module only when those two
# commands run; nothing else in sb touches it.
VOX_SOURCE = r"""
#  Vox Encryption Module      v1.7.3
#  Documentation          jts.gg/vox
#  License         r2.jts.gg/license
#
#  this module implements a misuse-resistant AEAD using:
#    - HMAC-SHA512 (PRF)
#    - PBKDF2-HMAC-SHA512 (key stretching)
#    - HKDF-Expand (RFC 5869) (key separation)
#
#  security properties:
#    - AEAD confidentiality and authenticity
#    - nonce misuse resistance (SIV)
#    - key separation
#    - RNG failure resistance
#
#  misuse bounds and limits:
#  - repeated encryption of identical plaintext with identical
#    associated data reveals equality only
#  - authenticity is always preserved
#  - recommended maximum data encrypted per key: 2^40 bytes (1TB) - hard limit: 2^46 bytes (64TB)

import os
import hashlib
import hmac
import base64

SALT_LEN        = 64        # synthetic nonce length (SIV)
TAG_LEN         = 64        # AEAD authentication tag length
KDF_ITERS       = 300_000   # PBKDF2 work factor
KDF_KEY_LEN     = 64        # master key length
KEM_LEN         = 64        # legacy asymmetric prefix length

# internal context cache
# ensures PBKDF2 is executed once per key lifecycle

_CTX_CACHE = {}

# key setup context

class VoxContext:
    # holds stretched and separated keys

    def __init__(self, passkey: bytes):
        master = _kdf(passkey)

        self.enc_key = _hkdf_expand(master, b"vox enc", 64)
        self.mac_key = _hkdf_expand(master, b"vox mac", 64)

# internal helper

def _get_context(passkey: bytes) -> VoxContext:
    ctx = _CTX_CACHE.get(passkey)
    if ctx is None:
        ctx = VoxContext(passkey)
        _CTX_CACHE[passkey] = ctx
    return ctx

# public API

def encrypt(
    plaintext: bytes,
    passkey: str,
    *,
    associated_data: bytes = b"",
    asym: bool = False
) -> bytes:
    # encrypts plaintext using AEAD
    # associated_data is authenticated but not encrypted

    pt = plaintext

    if asym:
        enc, shared = _kem_encapsulate(passkey)
        ctx = _get_context(shared)
        ct = _aead_encrypt(ctx, pt, associated_data)
        return enc + ct

    ctx = _get_context(passkey.encode())
    return _aead_encrypt(ctx, pt, associated_data)


def decrypt(
    ciphertext: bytes,
    passkey: str,
    *,
    associated_data: bytes = b"",
    asym: bool = False
) -> bytes:
    # verifies authenticity before decryption

    if asym:
        enc = ciphertext[:KEM_LEN]
        ct  = ciphertext[KEM_LEN:]
        shared = _kem_decapsulate(enc, passkey)
        ctx = _get_context(shared)
        pt = _aead_decrypt(ctx, ct, associated_data)
        return pt

    ctx = _get_context(passkey.encode())
    pt = _aead_decrypt(ctx, ciphertext, associated_data)
    return pt

# AEAD core

def _aead_encrypt(
    ctx: VoxContext,
    plaintext: bytes,
    associated_data: bytes
) -> bytes:
    # SIV-style AEAD construction

    salt = hmac.new(
        ctx.mac_key,
        associated_data + plaintext,
        hashlib.sha512
    ).digest()[:SALT_LEN]

    stream = _derive_keystream(ctx.enc_key, salt, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream))

    tag = hmac.new(
        ctx.mac_key,
        salt + associated_data + ciphertext,
        hashlib.sha512
    ).digest()

    return salt + ciphertext + tag


def _aead_decrypt(
    ctx: VoxContext,
    data: bytes,
    associated_data: bytes
) -> bytes:
    # verifies authentication prior to decryption

    salt = data[:SALT_LEN]
    tag  = data[-TAG_LEN:]
    ct   = data[SALT_LEN:-TAG_LEN]

    expected = hmac.new(
        ctx.mac_key,
        salt + associated_data + ct,
        hashlib.sha512
    ).digest()

    if not hmac.compare_digest(tag, expected):
        raise ValueError("authentication failed")

    stream = _derive_keystream(ctx.enc_key, salt, len(ct))
    return bytes(a ^ b for a, b in zip(ct, stream))

# key derivation

def _kdf(passkey: bytes) -> bytes:
    # PBKDF2-HMAC-SHA512 is used solely for key stretching

    return hashlib.pbkdf2_hmac(
        "sha512",
        passkey,
        b"vox-static-salt-SS7419",
        KDF_ITERS,
        dklen=KDF_KEY_LEN
    )


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    # HKDF-Expand as defined in RFC 5869

    out = b""
    t = b""
    counter = 1

    while len(out) < length:
        t = hmac.new(
            prk,
            t + info + bytes([counter]),
            hashlib.sha512
        ).digest()
        out += t
        counter += 1

    return out[:length]

# keystream generation

def _derive_keystream(
    key: bytes,
    nonce: bytes,
    length: int
) -> bytes:
    # PRF keystream generator

    out = bytearray()
    counter = 0

    while len(out) < length:
        block = hmac.new(
            key,
            nonce + counter.to_bytes(5, "big"),
            hashlib.sha512
        ).digest()
        out.extend(block)
        counter += 1

    return bytes(out[:length])

# legacy asymmetric (non-standard)
# does not provide forward secrecy and is excluded from security claims

def keypair():
    sk = os.urandom(64)
    pk = hashlib.sha512(sk).digest()
    return (
        base64.b64encode(pk).decode(),
        base64.b64encode(sk).decode()
    )


def _kem_encapsulate(pk_b64: str):
    pk = base64.b64decode(pk_b64)
    r = os.urandom(64)
    mask = hashlib.sha512(pk).digest()
    enc = bytes(a ^ b for a, b in zip(r, mask))
    shared = hashlib.sha512(r + pk).digest()
    return enc, shared


def _kem_decapsulate(enc: bytes, sk_b64: str):
    sk = base64.b64decode(sk_b64)
    pk = hashlib.sha512(sk).digest()
    mask = hashlib.sha512(pk).digest()
    r = bytes(a ^ b for a, b in zip(enc, mask))
    return hashlib.sha512(r + pk).digest()
"""

def load_vox():
    """Load the embedded vox module in memory (no disk, no network)."""
    import types
    mod = types.ModuleType("vox")
    try:
        exec(compile(VOX_SOURCE, "vox.py", "exec"), mod.__dict__)
    except Exception as e:
        die(f"the embedded encryption module failed to load: {e}")
    if not (hasattr(mod, "encrypt") and hasattr(mod, "decrypt")):
        die("the embedded encryption module is missing encrypt/decrypt")
    return mod

def _frame(manifest: dict, db: bytes) -> bytes:
    head = canonical(manifest)
    return len(head).to_bytes(4, "big") + head + db

def _unframe(payload: bytes):
    n = int.from_bytes(payload[:4], "big")
    manifest = json.loads(payload[4:4 + n])
    return manifest, payload[4 + n:]

def _snapshot_db(repo: Repo) -> bytes:
    """A consistent single-file copy of the store, WAL folded in."""
    src = repo.vdir / DB_NAME
    tmp = Path(tempfile.mkdtemp(prefix="sb-pack-")) / "snap.db"
    con = sqlite3.connect(str(src))
    try:
        con.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        con.close()
    data = tmp.read_bytes()
    tmp.unlink(); tmp.parent.rmdir()
    return data

def _tar_tree(repo, tree) -> bytes:
    """Serialize a saved tree's files into an in-memory tar."""
    import tarfile, io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for rel in sorted(tree):
            mode, bh = tree[rel]
            _, data = repo.get(bh)
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if mode == "100755" else 0o644
            info.mtime = int(time.time())
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()

def _untar_files(data: bytes, dest: Path) -> int:
    """Safely extract an archive's files into dest. Returns file count."""
    import tarfile, io
    n = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as t:
        for m in t.getmembers():
            if not m.isreg():
                continue
            parts = Path(m.name).parts
            if (m.name.startswith("/") or any(p in ("..", SB_DIR) for p in parts)
                    or not all(safe_name(p) for p in parts)):
                die(f"archive contains an unsafe path: {m.name!r} — refusing")
            out = dest / m.name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(t.extractfile(m).read())
            try:
                os.chmod(out, m.mode & 0o777)
            except OSError:
                pass
            n += 1
    return n

def cmd_pack(args):
    repo = need_repo()
    # canonical: sb pack [out] --key <PASS-KEY>; classic 'sb pack KEY [out]' works too
    extra = list(args.params)
    if args.key:
        key = args.key
        if len(extra) > 1:
            die("too many arguments — with --key, give at most one output name")
        out_name = extra[0] if extra else None
    else:
        if not extra:
            die("a pass-key is required:  sb pack --key <PASS-KEY> [out.sbox]")
        if len(extra) > 2:
            die("too many arguments — usage: sb pack --key <PASS-KEY> [out.sbox]")
        key = extra[0]
        out_name = extra[1] if len(extra) > 1 else None
    work = snapshot_worktree(repo, write=False)
    tree, _ = head_tree_files(repo)
    a, m, d = worktree_vs_tree(work, tree)
    if a or m or d:
        print(yellow("note: ") + dim(f"{len(a)+len(m)+len(d)} unsaved change(s) "
              "will NOT be included — pack seals saved history. Save first "
              "to capture them."))
    vox = load_vox()
    name, email = author()
    if args.files_only:
        if not tree:
            die("nothing saved yet — files-only pack needs at least one save")
        payload_kind = "files"
        body = _tar_tree(repo, tree)
    else:
        payload_kind = "repo"
        body = _snapshot_db(repo)
    manifest = {
        "format": "sbox",
        "sbox_version": SBOX_VERSION,
        "sb_version": VERSION,
        "payload": payload_kind,
        "created": int(time.time()),
        "created_by": {"name": name, "email": email},
        "repo_id": repo.meta("repo_id"),
        "repo_name": repo.root.name,
        "branch": repo.current_branch(),
        "chain_head": repo.chain_head(),
        "files": len(tree),
        "db_sha256": sha256_hex(body),
        "db_size": len(body),
    }
    out = Path(out_name) if out_name else Path(f"{repo.root.name}.sbox")
    if out.suffix != ".sbox":
        out = out.with_name(out.name + ".sbox")
    if out.exists():
        die(f"{out} already exists — choose another name or remove it")
    header = SBOX_MAGIC + bytes([SBOX_VERSION])
    blob = vox.encrypt(_frame(manifest, body), key, associated_data=header)
    tmp = out.with_name(out.name + ".sbtmp")
    tmp.write_bytes(header + blob)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, out)
    size = out.stat().st_size
    print(f"{bold('packed')} {amber(out.name)} {dim('·')} {dim(f'{size:,} bytes')}")
    what = (f"files only · {len(tree)} file(s), no history"
            if args.files_only else "full history + files")
    tree_print([
        f"branch   {manifest['branch']} {dim('· anchor')} {amber(manifest['chain_head'][:16])}",
        f"holds    {what}",
        f"sealed   {name} <{email}>  "
        + dim(time.strftime('%Y-%m-%d %H:%M', time.localtime(manifest['created']))),
        dim("encrypted with vox · unpack: sb unpack "
            + out.name + " --key <PASS-KEY>"),
    ])

def cmd_unpack(args):
    # canonical: sb unpack <file> [dest] --key <PASS-KEY>;
    # classic 'sb unpack <file> KEY [dest]' works too
    extra = list(args.params)
    if not extra:
        die("usage:  sb unpack <file.sbox> --key <PASS-KEY> [dest]")
    path_name = extra.pop(0)
    if args.key:
        key = args.key
        if len(extra) > 1:
            die("too many arguments — with --key, give at most one destination")
        dest_name = extra[0] if extra else None
    else:
        if not extra:
            die("usage:  sb unpack <file.sbox> --key <PASS-KEY> [dest]")
        if len(extra) > 2:
            die("too many arguments — usage: sb unpack <file.sbox> "
                "--key <PASS-KEY> [dest]")
        key = extra[0]
        dest_name = extra[1] if len(extra) > 1 else None
    src = Path(path_name)
    if not src.is_file():
        die(f"no such file: {src}")
    raw = src.read_bytes()
    if raw[:4] != SBOX_MAGIC:
        die(f"{src} is not a sandbox archive (bad magic)")
    ver = raw[4]
    if ver > SBOX_VERSION:
        die(f"archive format {ver} is newer than this sb understands "
            f"({SBOX_VERSION}) — upgrade sb")
    header, blob = raw[:5], raw[5:]
    vox = load_vox()
    try:
        payload = vox.decrypt(blob, key, associated_data=header)
    except Exception:
        die("could not open the archive — wrong pass-key or the file was "
            "altered\n       (vox verifies authenticity before decrypting)")
    manifest, body = _unframe(payload)
    if sha256_hex(body) != manifest.get("db_sha256"):
        die("archive integrity check failed — the contents did not match "
            "their recorded hash")
    kind = manifest.get("payload", "repo")
    dest = Path(dest_name) if dest_name else Path(manifest.get("repo_name", "sandbox"))
    if (dest / SB_DIR).exists():
        die(f"{dest / SB_DIR} already exists — unpack into a fresh folder")
    who = manifest.get("created_by", {})
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(manifest.get("created", 0)))
    files_only = args.files_only or kind == "files"

    if kind == "files":
        # archive holds files, no history — extraction is inherently files-only
        dest.mkdir(parents=True, exist_ok=True)
        n = _untar_files(body, dest)
        held = "files only — this archive carries no history"
    elif files_only:
        # full-history archive, but the user wants just the native files:
        # restore the store in a temp area, check out from there, keep no .sb
        stage = Path(tempfile.mkdtemp(prefix="sb-unpack-"))
        try:
            (stage / SB_DIR).mkdir()
            (stage / SB_DIR / DB_NAME).write_bytes(body)
            srepo = Repo(stage.resolve())
            tree, _ = head_tree_files(srepo)
            dest.mkdir(parents=True, exist_ok=True)
            n = _untar_files(_tar_tree(srepo, tree), dest)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        held = "files only — history was in the archive but not written"
    else:
        (dest / SB_DIR).mkdir(parents=True, exist_ok=True)
        db_path = dest / SB_DIR / DB_NAME
        db_path.write_bytes(body)
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass
        repo = Repo(dest.resolve())
        tree, _ = head_tree_files(repo)
        checkout_tree(repo, tree, {})
        n = len(tree)
        held = None

    print(f"{bold('unpacked')} {amber(str(dest))} {dim('·')} {dim(str(n) + ' file(s)')}")
    rows = [f"sealed by  {who.get('name','?')} <{who.get('email','?')}>  {dim('· ' + when)}",
            f"branch     {manifest.get('branch','main')} "
            + dim("· anchor ") + amber(manifest.get("chain_head","")[:16])]
    if held:
        rows.append(dim(held))
    else:
        rows.append(dim(f"verify it: cd {dest} && sb verify"))
    tree_print(rows)

def _resolve_version(repo, what):
    """Resolve a deploy label, branch name, or save-hash prefix to a commit.
    Returns (commit_hash, human description of how it matched)."""
    recs = [e for e in repo.journal_entries()
            if e["op"] == "deploy" and e["detail"].get("label") == what]
    if recs:
        d = recs[-1]["detail"]
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(recs[-1]["ts"]))
        return d["commit"], f"deploy '{what}' ({when})"
    if what in repo.branches():
        t = repo.tip(what)
        if not t:
            die(f"branch '{what}' has no saves yet")
        return t, f"branch '{what}'"
    if re.fullmatch(r"[0-9a-f]{4,64}", what or ""):
        rows = repo.db.execute(
            "SELECT hash FROM objects WHERE kind='commit' AND hash LIKE ?",
            (what + "%",)).fetchall()
        if len(rows) == 1:
            return rows[0][0], "save " + short(rows[0][0])
        if len(rows) > 1:
            die(f"'{what}' matches {len(rows)} saves — give more characters")
    die(f"nothing named '{what}' — not a deploy label, branch, or save hash\n"
        f"       (see labels: sb deploy --list · see saves: sb log)")

def cmd_export(args):
    repo = need_repo()
    extra = list(args.params)
    if not extra:
        die("usage:  sb export <label|branch|hash> [dest] [--key PASS-KEY]")
    if len(extra) > 2:
        die("too many arguments — usage: sb export <version> [dest] "
            "[--key PASS-KEY]")
    what, dest_name = extra[0], (extra[1] if len(extra) > 1 else None)
    commit_hash, how = _resolve_version(repo, what)
    c = parse_commit(repo, commit_hash)
    tree = read_tree(repo, c["tree"])
    if not tree:
        die(f"{how} contains no files")
    name, email = author()

    key = args.key or (args.sbox or None)
    sbox_mode = args.key is not None or args.sbox is not None
    if sbox_mode:
        if not key:
            die("a pass-key is required:  sb export <version> --key <PASS-KEY>")
        vox = load_vox()
        body = _tar_tree(repo, tree)
        manifest = {
            "format": "sbox", "sbox_version": SBOX_VERSION,
            "sb_version": VERSION, "payload": "files",
            "created": int(time.time()),
            "created_by": {"name": name, "email": email},
            "repo_id": repo.meta("repo_id"),
            "repo_name": repo.root.name,
            "branch": repo.current_branch(),
            "label": what, "commit": commit_hash,
            "chain_head": repo.chain_head(),
            "files": len(tree),
            "db_sha256": sha256_hex(body), "db_size": len(body),
        }
        out = Path(dest_name) if dest_name else Path(f"{repo.root.name}-{what}.sbox")
        if out.suffix != ".sbox":
            out = out.with_name(out.name + ".sbox")
        if out.exists():
            die(f"{out} already exists — choose another name or remove it")
        header = SBOX_MAGIC + bytes([SBOX_VERSION])
        blob = vox.encrypt(_frame(manifest, body), key,
                           associated_data=header)
        tmp = out.with_name(out.name + ".sbtmp")
        tmp.write_bytes(header + blob)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, out)
        print(f"{bold('exported')} {amber(out.name)} {dim('·')} "
              f"{dim(f'{out.stat().st_size:,} bytes')}")
        tree_print([
            f"version  {how} {dim('·')} {amber(short(commit_hash))}",
            f"holds    files only · {len(tree)} file(s), no history",
            dim(f"deploy it: sb unpack {out.name} --key <PASS-KEY> "
                f"/path/to/production --files-only"),
        ])
        return

    dest = Path(dest_name) if dest_name else Path(f"{repo.root.name}-{what}")
    if dest.exists() and any(dest.iterdir()):
        die(f"{dest} exists and is not empty — export into a fresh folder")
    dest.mkdir(parents=True, exist_ok=True)
    n = _untar_files(_tar_tree(repo, tree), dest)
    print(f"{bold('exported')} {amber(str(dest))} {dim('·')} {dim(str(n) + ' file(s)')}")
    tree_print([
        f"version  {how} {dim('·')} {amber(short(commit_hash))}",
        dim("plain files, no .sb — the repository stays where it is"),
    ])

def _parse_share_args(cmd, tokens):
    """Order-independent parsing for pack/unpack/export: flags may appear
    anywhere; everything else stays a positional, in the order given."""
    params, key, sbox, files_only = [], None, None, False
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-k", "--key"):
            i += 1
            if i >= len(tokens):
                die(f"{t} needs a value:  sb {cmd} ... {t} <PASS-KEY>")
            key = tokens[i]
        elif t.startswith("--key="):
            key = t[len("--key="):]
        elif cmd == "export" and t == "--sbox":            # legacy alias
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 1
                sbox = tokens[i]
            else:
                sbox = ""
        elif cmd == "export" and t.startswith("--sbox="):
            sbox = t[len("--sbox="):]
        elif t == "--files-only":
            files_only = True
        elif t.startswith("-") and len(t) > 1:
            die(f"unknown option '{t}' for sb {cmd}")
        else:
            params.append(t)
        i += 1
    return argparse.Namespace(cmd=cmd, params=params, key=key, sbox=sbox,
                              files_only=files_only)

# ---------------------------------------------------------------- CLI -------
def _row(cmd, desc, last=False):
    conn = amber("\u2514\u2500\u2500\u2500" if last else "\u251c\u2500\u2500\u2500")
    return f"  {conn} {cmd.ljust(22)}{dim(desc)}"

def _sub(text, cont=True):
    bar = amber("\u2502") if cont else " "
    return f"  {bar}      {dim(text)}"

HELP = f"""
  {bold('sandbox (sb)')}   {dim('version ' + VERSION)}
  {dim('optimal version control · ' + AUTHOR)}

{amber('work')}
{_row('init', 'start tracking this folder')}
{_row('status', 'what changed since the last save')}
{_row('save "msg"', 'snapshot everything')}
{_row('log [-n N]', 'history of saves')}
{_row('diff [path]', 'line-by-line changes')}
{_row('undo', 'revert the last save, keeping history')}
{_row('restore <path>', 'bring a file or folder back', last=True)}

{amber('branches')}
{_row('branch [name] [-r]', 'list, create, or remove branches')}
{_row('switch <branch>', 'move between branches')}
{_row('merge <branch>', 'bring <branch> into the current one', last=True)}

{amber('quality')}
{_row('test guide', 'how to set up test scripts')}
{_row('test [stage]', 'run test gates in a clean checkout')}
{_row('test new <s> <n>', 'scaffold a test script')}
{_row('test list', 'show discovered tests')}
{_row('deploy [label]', 'verify + test + record a release')}
{_row('verify [--anchor H]', 're-check objects and the journal', last=True)}

{amber('share')}
{_row('pack [out] --key K', 'seal the repo into an encrypted .sbox')}
{_sub('--files-only — seal saved files without history')}
{_row('unpack <file> --key K', 'restore a .sbox into a folder')}
{_sub('--files-only — write just the files, no .sb dir')}
{_row('export <ver> [dest]', 'files of a deploy, branch, or save', last=True)}
{_sub('--key K — as an encrypted .sbox instead', cont=False)}

{amber('repository')}
{_row('journal [-n N]', 'log of every operation')}
{_row('info', 'stats and chain head')}
{_row('who [name] [email]', 'how saves are attributed')}
{_row('ignore <pattern>', 'add a .sbignore pattern', last=True)}
"""

def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP); return
    if argv[0] in ("-V", "--version", "version"):
        print(f"sb {VERSION} · {AUTHOR}"); return
    p = argparse.ArgumentParser(prog="sb", add_help=False)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("init")
    sub.add_parser("status")
    sp = sub.add_parser("save"); sp.add_argument("message", nargs="?")
    sp.add_argument("--allow-secrets", action="store_true")
    sp.add_argument("--no-verify", action="store_true")
    lp = sub.add_parser("log"); lp.add_argument("-n", "--limit", type=int, default=0)
    dp = sub.add_parser("diff"); dp.add_argument("path", nargs="?")
    sub.add_parser("undo")
    rp = sub.add_parser("restore"); rp.add_argument("path")
    bp = sub.add_parser("branch"); bp.add_argument("name", nargs="?")
    bp.add_argument("-r", "--remove", action="store_true")
    wp = sub.add_parser("switch"); wp.add_argument("target")
    mp = sub.add_parser("merge"); mp.add_argument("branch")
    mp.add_argument("--no-verify", action="store_true")
    tp = sub.add_parser("test"); tp.add_argument("args", nargs="*")
    dpl = sub.add_parser("deploy"); dpl.add_argument("label", nargs="?")
    dpl.add_argument("--list", action="store_true")
    dpl.add_argument("--no-verify", action="store_true")
    vp = sub.add_parser("verify"); vp.add_argument("--anchor")
    jp = sub.add_parser("journal"); jp.add_argument("-n", "--limit", type=int, default=0)
    sub.add_parser("info")
    who = sub.add_parser("who"); who.add_argument("name", nargs="?")
    who.add_argument("email", nargs="?")
    gp = sub.add_parser("ignore"); gp.add_argument("pattern")
    pk = sub.add_parser("pack"); pk.add_argument("params", nargs="*")
    pk.add_argument("-k", "--key", metavar="PASS-KEY")
    pk.add_argument("--files-only", action="store_true")
    up = sub.add_parser("unpack"); up.add_argument("params", nargs="*")
    up.add_argument("-k", "--key", metavar="PASS-KEY")
    up.add_argument("--files-only", action="store_true")
    ex = sub.add_parser("export"); ex.add_argument("params", nargs="*")
    ex.add_argument("-k", "--key", metavar="PASS-KEY")
    ex.add_argument("--sbox", nargs="?", const="", metavar="PASS-KEY")
    if argv and argv[0] in ("pack", "unpack", "export"):
        args = _parse_share_args(argv[0], argv[1:])
    else:
        args = p.parse_args(argv)
    if args.cmd is None:
        print(HELP); return
    try:
        globals()[f"cmd_{args.cmd}"](args)
    except CorruptObject as e:
        die(str(e) + " — run 'sb verify' for a full report")
    except sqlite3.Error as e:
        die(f"store error: {e} — the database may be locked or damaged; "
            f"run 'sb verify'")

if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
