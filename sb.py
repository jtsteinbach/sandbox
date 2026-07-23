#!/usr/bin/env python3
"""
sandbox (sb) — version control in a single file.

version   1.4
author    jts.gg/sandbox
"""

import sys, os, io, json, time, zlib, hashlib, fnmatch, difflib, re
import argparse, contextlib
import sqlite3, subprocess, tempfile, getpass, shutil
from pathlib import Path

VERSION = "1.4"
AUTHOR = "jts.gg/sandbox"
FORMAT_VERSION = 1
SB_DIR = ".sb"
DB_NAME = "sandbox.db"

# every journal op that moves a branch tip — verify cross-checks refs
# against these, and _journal_tips_at replays them
REF_OPS = ("save", "merge", "undo", "restore", "branch", "ref",
           "autosave")

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
    ctime INTEGER NOT NULL DEFAULT 0,
    ino   INTEGER NOT NULL DEFAULT 0,
    hash  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locks (
    path   TEXT PRIMARY KEY,
    owner  TEXT NOT NULL,
    email  TEXT NOT NULL,
    since  INTEGER NOT NULL,
    base   TEXT NOT NULL DEFAULT '',
    held   TEXT NOT NULL DEFAULT '',
    mode   TEXT NOT NULL DEFAULT '100644',
    uid    INTEGER NOT NULL DEFAULT -1
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

_UNSET = object()   # sentinel: "caller passed no expected value" for CAS

class Repo:
    def __init__(self, root: Path, create=False):
        self.root = root
        self.vdir = root / SB_DIR
        db_path = self.vdir / DB_NAME
        if create:
            self.vdir.mkdir(parents=True, exist_ok=False)
        # autocommit connection; all writes go through transaction()
        self.db = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
        self._tx = 0                     # transaction nesting depth
        self.db.execute("PRAGMA journal_mode=WAL")
        # FULL by default: the newest committed transaction survives OS crash
        # and power loss, at a small write cost. 'sb durability normal'
        # trades that for speed (still crash-safe, may lose the last commit on
        # power loss). Applied after open once meta is readable.
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        if create:
            with self.transaction():   # schema, meta, refs, init entry
                for stmt in SCHEMA.split(";"):
                    if stmt.strip():
                        self.db.execute(stmt)
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
        if self.meta("durability") == "normal":
            self.db.execute("PRAGMA synchronous=NORMAL")
        if not create:
            self._migrate_statcache()
            self._migrate_locks()

    @contextlib.contextmanager
    def transaction(self):
        """One atomic unit of work. Nested calls join the outermost
        transaction, so a whole command commits or rolls back together.
        Any exception, including sys.exit, triggers the rollback."""
        if self._tx == 0:
            self.db.execute("BEGIN IMMEDIATE")
        self._tx += 1
        try:
            yield
        except BaseException:
            self._tx -= 1
            if self._tx == 0:
                self.db.execute("ROLLBACK")
            raise
        else:
            self._tx -= 1
            if self._tx == 0:
                self.db.execute("COMMIT")

    def _migrate_statcache(self):
        """Pre-1.1 stores keyed the stat cache on (size, mtime) only. Add
        the ctime and inode columns and drop the stale rows; the only cost
        is one full re-read on the next command."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(statcache)")}
        if "ctime" in cols and "ino" in cols:
            return
        with self.transaction():
            if "ctime" not in cols:
                self.db.execute("ALTER TABLE statcache ADD COLUMN "
                                "ctime INTEGER NOT NULL DEFAULT 0")
            if "ino" not in cols:
                self.db.execute("ALTER TABLE statcache ADD COLUMN "
                                "ino INTEGER NOT NULL DEFAULT 0")
            self.db.execute("DELETE FROM statcache")

    def _migrate_locks(self):
        """Create the locks table on stores made before shared-lock support,
        and add the columns the 1.4 content-preserving lock model needs:
        `held` (the exact content the holder is protecting), its file mode,
        and the OS account the holder writes as. A pre-1.4 lock has an empty
        `held`; the first enforcement pass adopts whatever is on disk rather
        than guessing, so nothing is reverted on the strength of a blank."""
        with self.transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS locks ("
                "path TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                "email TEXT NOT NULL, since INTEGER NOT NULL, "
                "base TEXT NOT NULL DEFAULT '')")
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(locks)")}
            if "held" not in cols:
                self.db.execute("ALTER TABLE locks ADD COLUMN "
                                "held TEXT NOT NULL DEFAULT ''")
            if "mode" not in cols:
                self.db.execute("ALTER TABLE locks ADD COLUMN "
                                "mode TEXT NOT NULL DEFAULT '100644'")
            if "uid" not in cols:
                self.db.execute("ALTER TABLE locks ADD COLUMN "
                                "uid INTEGER NOT NULL DEFAULT -1")

    # ---- meta ----
    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self.transaction():
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))

    # ---- object store ----
    def put(self, kind: str, data: bytes) -> str:
        h = hash_obj(kind, data)
        with self.transaction():
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

    def update_ref(self, branch, commit_hash, op="ref", expect=_UNSET,
                   extra=None):
        """Move a branch tip. The ref update and its journal entry commit in
        one transaction. If `expect` is given, the move is a compare-and-swap:
        the tip must still equal `expect` (the value the caller read before
        doing its work), or the whole transaction aborts with a clear error.
        This turns the 'two saves race, one silently orphans the other's
        commit' scenario into a loud, safe failure. `extra` merges audit
        fields (e.g. gate bypasses) into the journal entry."""
        with self.transaction():
            old = self.tip(branch)
            if expect is not _UNSET and (old or None) != (expect or None):
                die(f"branch '{branch}' changed under this operation "
                    f"(expected {short(expect)}, found {short(old)}) — "
                    f"nothing was changed; run the command again")
            self.db.execute(
                "INSERT INTO refs(name,hash) VALUES(?,?) "
                "ON CONFLICT(name) DO UPDATE SET hash=excluded.hash",
                (branch, commit_hash or ""))
            detail = {"branch": branch, "old": old or "", "new": commit_hash or ""}
            if extra:
                detail.update(extra)
            self.journal(op, detail)

    def remove_ref(self, branch):
        """Delete a branch tip. The deletion and its journal entry commit
        in one transaction."""
        old = self.tip(branch)
        with self.transaction():
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
        with self.transaction():
            self.db.execute(
                "INSERT INTO journal(ts,op,detail,prev,link) VALUES(?,?,?,?,?)",
                (ts, op, canonical(detail).decode(), prev, link))
        return link

    def journal_entries(self):
        for seq, ts, op, detail, prev, link in self.db.execute(
                "SELECT seq,ts,op,detail,prev,link FROM journal ORDER BY seq"):
            try:
                d = json.loads(detail)
            except (ValueError, TypeError):
                raise TamperedJournal(
                    f"journal entry #{seq} detail is not valid JSON "
                    f"(edited outside sb?)")
            yield {"seq": seq, "ts": ts, "op": op,
                   "detail": d, "prev": prev, "link": link}

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
    # Keyed on size + mtime + ctime + inode. mtime can be restored from
    # userspace (touch -d, archive extraction), so a same-size edit could
    # slip past a (size, mtime) key; ctime cannot be set backward, and the
    # inode changes when an editor replaces the file. A miss only means
    # the file is re-read.
    def cached_hash(self, rel, size, mtime_ns, ctime_ns, ino):
        row = self.db.execute(
            "SELECT hash FROM statcache WHERE path=? AND size=? AND mtime=? "
            "AND ctime=? AND ino=?",
            (rel, size, mtime_ns, ctime_ns, ino)).fetchone()
        return row[0] if row else None

    def remember(self, entries):
        if not entries:
            return
        with self.transaction():
            self.db.executemany(
                "INSERT INTO statcache(path,size,mtime,ctime,ino,hash) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                "mtime=excluded.mtime, ctime=excluded.ctime, "
                "ino=excluded.ino, hash=excluded.hash", entries)

    # ---- shared-edit locks ----
    def locks(self):
        """All current locks:
        {path: {owner, email, since, base, held, mode, uid}}.
        `held` is the content hash the holder is protecting, LOCK_DELETED if
        the holder removed the file, or "" for a pre-1.4 lock that predates
        content tracking."""
        out = {}
        for path, owner, email, since, base, held, mode, uid in self.db.execute(
                "SELECT path,owner,email,since,base,held,mode,uid FROM locks"):
            out[path] = {"owner": owner, "email": email, "since": since,
                         "base": base, "held": held or "",
                         "mode": mode or "100644",
                         "uid": -1 if uid is None else int(uid)}
        return out

    def set_lock(self, path, owner, email, base, held="", mode="100644",
                 uid=-1):
        with self.transaction():
            self.db.execute(
                "INSERT INTO locks(path,owner,email,since,base,held,mode,uid) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO NOTHING",
                (path, owner, email, int(time.time()), base or "",
                 held or "", mode or "100644",
                 -1 if uid is None else int(uid)))

    def update_lock_held(self, path, held, mode, touch=True):
        """Move a lock's protected content forward — the holder edited the
        file again. `touch` also restarts the expiry clock, so a lock times
        out after an hour of NOT being worked on rather than an hour after
        the first keystroke."""
        with self.transaction():
            if touch:
                self.db.execute(
                    "UPDATE locks SET held=?, mode=?, since=? WHERE path=?",
                    (held or "", mode or "100644", int(time.time()), path))
            else:
                self.db.execute(
                    "UPDATE locks SET held=?, mode=? WHERE path=?",
                    (held or "", mode or "100644", path))

    def clear_locks(self, paths):
        if not paths:
            return
        with self.transaction():
            self.db.executemany("DELETE FROM locks WHERE path=?",
                                [(p,) for p in paths])

def need_repo() -> Repo:
    root = find_repo()
    if not root:
        die("not inside a sandbox repository (run 'sb init')")
    repo = Repo(root)
    # in shared mode, every command teaches the store which OS account this
    # sb identity belongs to, so edits found on disk later can be attributed
    # to their real author. Best-effort: never block a read-only command.
    if shared_mode(repo):
        try:
            register_identity(repo)
        except sqlite3.Error:
            pass
    return repo

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
# before they enter permanent history. Since 1.3, recognized credentials in
# clean-UTF-8 text are REDACTED in the committed blob (the working file is
# never rewritten) and the save proceeds; files that cannot be rewritten
# safely still block. --allow-secrets saves the bytes verbatim (journaled).
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

REDACTED = "<REDACTED>"

# The line scanner flags a private key by its BEGIN header, but the key
# material is the base64 body BELOW that line — redacting only the header
# would leave the actual secret in history. This whole-block pattern (BEGIN
# through the matching END, or end-of-file for a truncated block) makes the
# redaction cover the key itself.
_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    r".*?"
    r"(?:-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    r"|\Z)", re.S)

def redact_secrets(data: bytes):
    """Replace every recognized credential in `data` with <REDACTED>.
    Returns (new_data, findings, safe):
      findings  [(line_no, label), ...] — what was found
      safe      True  -> new_data is a faithful rewrite (or nothing found)
                False -> secrets were found but the file is not clean UTF-8,
                         so it cannot be rewritten without corrupting it;
                         the caller must block instead of redacting.
    Binary and oversized files are skipped entirely (same rule the scanner
    has always used). The working file on disk is never touched — this
    rewrites only the blob that will be committed."""
    if len(data) > MAX_SCAN_BYTES or b"\0" in data[:8000]:
        return data, [], True                       # binary or huge: skip
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # can't rewrite without mangling bytes — report via the lossy
        # scanner and let the caller block the save
        return data, scan_secrets(data), False
    findings = []
    def _block_sub(m):
        findings.append((text.count("\n", 0, m.start()) + 1,
                         "private key block"))
        return REDACTED
    text = _KEY_BLOCK.sub(_block_sub, text)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for label, pat in SECRET_PATTERNS:
            if label == "private key block":
                continue                             # handled above, whole-block
            line, n = pat.subn(REDACTED, line)
            if n:
                findings.append((i + 1, label))
        lines[i] = line
    if not findings:
        return data, [], True
    return "\n".join(lines).encode("utf-8"), findings, True

def _redact_for_commit(repo, disk, paths, allow_secrets=False):
    """The redaction pass every commit path shares. `disk` is a snapshot
    whose blobs are already stored; `paths` are the ones about to be
    committed. Returns (overrides, redacted, hard_blocked):
      overrides    {rel: (mode, redacted_blob_hash)} to layer over `disk`
      redacted     [(rel, findings)] for the report
      hard_blocked [(rel, line, label)] files that hold a credential but
                   cannot be rewritten faithfully — the caller must stop."""
    over, redacted, hard = {}, [], []
    if allow_secrets:
        return over, redacted, hard
    for rel in paths:
        if rel not in disk:
            continue                        # a deletion — nothing to scan
        _, data = repo.get(disk[rel][1])
        new_data, findings, safe = redact_secrets(data)
        if not findings:
            continue
        if not safe:
            hard.extend((rel, ln, lb) for ln, lb in findings)
            continue
        over[rel] = (disk[rel][0], repo.put("blob", new_data))
        redacted.append((rel, findings))
    return over, redacted, hard

def _report_hard_blocked(hard_blocked, what="save"):
    print(red(f"{what} blocked — secrets in files that cannot be "
              "safely redacted (not clean UTF-8)"))
    tree_print([red(f"{rel}:{ln}  {lb}") for rel, ln, lb in hard_blocked])
    print(dim("history is permanent; remove the secret, add the "
              "file\nto .sbignore, or override deliberately with "
              "--allow-secrets"))
    sys.exit(2)

# ----------------------------------------------------------- trees ----------
_BAD_NAME = re.compile(r"^\.?\.?$")   # "", ".", ".."

def safe_name(name: str) -> bool:
    """Tree entry names must be single path components. This is what stops a
    crafted tree object from writing outside the repository on checkout."""
    return bool(name) and "/" not in name and "\\" not in name \
        and "\0" not in name and not _BAD_NAME.match(name) and name != SB_DIR

RACY_WINDOW_NS = 2_000_000_000   # files whose mtime OR ctime is < 2s old
                                 # bypass the stat cache and are re-read

# The stat cache trusts (size, mtime, ctime, inode) to skip re-reading a file.
# On Windows, Python reports st_ctime as CREATION time, not metadata-change
# time, so a same-size in-place edit with a restored mtime would not be caught.
# There the cache is unsafe as an authority, so we disable it and hash every
# file. 'sb status --deep' and every release/pack path hash unconditionally on
# all platforms.
_STATCACHE_TRUSTWORTHY = not sys.platform.startswith("win")

def snapshot_worktree(repo: Repo, write=True, deep=False):
    """Walk the working tree -> {rel: (mode, blob_hash)}.
    write=True also stores the blobs. Unless deep=True (or the platform's
    ctime is untrustworthy), the stat cache lets unchanged files be skipped;
    recently-touched files are always re-read."""
    use_cache = _STATCACHE_TRUSTWORTHY and not deep
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
            if use_cache:
                age_ns = min(now_ns - st.st_mtime_ns, now_ns - st.st_ctime_ns)
                if age_ns > RACY_WINDOW_NS:
                    h = repo.cached_hash(rel, st.st_size, st.st_mtime_ns,
                                         st.st_ctime_ns, st.st_ino)
            if h is not None and (not write or repo.has(h)):
                files[rel] = (mode, h)
                continue
            data = p.read_bytes()
            h = repo.put("blob", data) if write else hash_obj("blob", data)
            files[rel] = (mode, h)
            cache_updates.append((rel, st.st_size, st.st_mtime_ns,
                                  st.st_ctime_ns, st.st_ino, h))
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
    try:
        entries = json.loads(data or b"[]")
        entries = [(m, k, h, n) for m, k, h, n in entries]
    except (ValueError, TypeError):
        raise CorruptObject(
            f"tree {short(tree_hash)} does not decode to tree entries")
    for mode, k, h, name in entries:
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
        raise CorruptObject(f"{short(h)} is a {kind}, not a save")
    try:
        c = json.loads(data)
    except ValueError:
        raise CorruptObject(f"save {short(h)} does not decode to a commit")
    if (not isinstance(c, dict) or "tree" not in c
            or not isinstance(c.get("parents"), list)):
        raise CorruptObject(f"save {short(h)} is missing commit fields")
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

_EMPTY_BLOB = hash_obj("blob", b"")

def detect_renames(new_files, old_files, added, deleted):
    """Exact-content rename detection: pair each deleted path with an added
    path whose blob content is byte-identical (same hash). Deterministic —
    both sides are matched in sorted order. Empty files never pair (every
    empty file would match every other). Returns (renames, added', deleted')
    with renames as [(old_path, new_path)] and paired paths removed."""
    by_hash = {}
    for p in sorted(deleted):
        h = old_files[p][1]
        if h != _EMPTY_BLOB:
            by_hash.setdefault(h, []).append(p)
    renames, still_added = [], []
    for p in sorted(added):
        olds = by_hash.get(new_files[p][1])
        if olds:
            renames.append((olds.pop(0), p))
        else:
            still_added.append(p)
    gone = {o for o, _ in renames}
    return renames, still_added, [p for p in sorted(deleted) if p not in gone]

# ----------------------------------------------------- checkout / cleanup ---
def _safe_parent_fd(root_fd: int, rel: str):
    """Open the parent directory of `rel` relative to root_fd, refusing to
    follow any symlinked component. Returns (parent_fd, leaf_name). The
    caller closes parent_fd. Raises CheckoutConflict if a component that
    must be a directory is a file, a symlink, or a Windows reparse point."""
    parts = rel.split("/")
    leaf = parts[-1]
    fd = os.dup(root_fd)
    try:
        for comp in parts[:-1]:
            try:
                nfd = os.open(comp, os.O_RDONLY | _O_NOFOLLOW | _O_DIRECTORY,
                              dir_fd=fd)
            except FileNotFoundError:
                nfd = _mkdir_at(fd, comp)
            except (NotADirectoryError, OSError) as e:
                # a symlink component raises ELOOP with O_NOFOLLOW; a plain
                # file raises ENOTDIR — both mean the path is unsafe
                raise CheckoutConflict(
                    f"cannot check out {rel!r}: {comp!r} is not a real "
                    f"directory (symlink, reparse point, or file in the way)")
            os.close(fd)
            fd = nfd
        return fd, leaf
    except BaseException:
        os.close(fd)
        raise

def _mkdir_at(dir_fd: int, name: str) -> int:
    os.mkdir(name, 0o755, dir_fd=dir_fd)
    return os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_DIRECTORY, dir_fd=dir_fd)

# O_NOFOLLOW / O_DIRECTORY exist on POSIX; fall back to 0 elsewhere and lean
# on the lstat pre-check below (Windows junctions still need the pre-check).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

class CheckoutConflict(Exception):
    """A path in the working tree conflicts with the target (a symlinked or
    file-shaped parent, or a directory where a file must go)."""

def _lstat_at(dir_fd, name):
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except (FileNotFoundError, NotADirectoryError):
        return None

def _remove_at(dir_fd, name):
    """Delete name under dir_fd whether it is a file, symlink, or directory
    tree — never following a symlink out of the repository."""
    st = _lstat_at(dir_fd, name)
    if st is None:
        return
    import stat as _stat
    if _stat.S_ISDIR(st.st_mode):
        sub = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_DIRECTORY,
                      dir_fd=dir_fd)
        try:
            for child in os.listdir(sub):
                _remove_at(sub, child)
        finally:
            os.close(sub)
        os.rmdir(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)

def _plan_checkout(target: dict, current: dict):
    """Two-phase plan. Deletions first (so a file→directory transition frees
    the name before the directory is created), then creations sorted parent-
    first (so a directory→file transition removes children before the parent
    name is reused). Returns (deletions, creations)."""
    deletions = [rel for rel in current if rel not in target]
    # a path that changes shape (file↔dir) shows up as: some old paths under
    # it disappear and a new path appears; sorting handles ordering, and the
    # per-name remove/create below tolerates whatever is physically there.
    creations = sorted(
        (rel for rel, mh in target.items()
         if current.get(rel) != mh or True),  # recheck on disk at apply time
        key=lambda r: r.count("/"))
    return deletions, creations

def _checkout_preserving(repo, target, current, preserve):
    """checkout_tree, but never touch paths in `preserve` on disk (they hold
    someone's in-progress edits — a lock holder's version, or the files an
    --ignore merge deliberately left alone)."""
    preserve = set(preserve or ())
    t = {k: v for k, v in target.items() if k not in preserve}
    c = {k: v for k, v in current.items() if k not in preserve}
    checkout_tree(repo, t, c, preserve=preserve)

def checkout_tree(repo: Repo, target: dict, current: dict, preserve=None):
    """Make the worktree equal `target`, symlink-safely and in an order that
    survives file↔directory transitions. Every write goes through a parent
    directory opened with no-follow semantics, so a pre-existing symlinked
    parent can never redirect a write outside the repository. Individual file
    writes are atomic (exclusive temp + rename + fsync); the whole-tree
    transition is not a single atomic unit (see the durability notes in the
    README), but a crash leaves only ordinary unsaved changes, never a write
    outside the repo and never a torn object in the store. Paths in `preserve`
    are never written or deleted (used by locks and --ignore merges)."""
    preserve = preserve or set()
    root_fd = os.open(str(repo.root), os.O_RDONLY | _O_DIRECTORY)
    try:
        deletions, creations = _plan_checkout(target, current)
        # Phase 0: pre-flight every creation's parent chain for conflicts, so
        # we fail before mutating anything rather than halfway through.
        for rel in creations:
            mode, h = target[rel]
            if current.get(rel) == (mode, h):
                pfd, leaf = _safe_parent_fd(root_fd, rel)
                try:
                    st = _lstat_at(pfd, leaf)
                    import stat as _stat
                    if st is not None and _stat.S_ISREG(st.st_mode):
                        continue          # already correct, leave it
                finally:
                    os.close(pfd)
        # Phase 1: deletions.
        for rel in deletions:
            if rel in preserve:
                continue
            pfd, leaf = _safe_parent_fd(root_fd, rel)
            try:
                _remove_at(pfd, leaf)
            finally:
                os.close(pfd)
        # Phase 2: creations, parent-first.
        for rel in creations:
            if rel in preserve:
                continue
            mode, h = target[rel]
            pfd, leaf = _safe_parent_fd(root_fd, rel)
            try:
                st = _lstat_at(pfd, leaf)
                if st is not None:
                    import stat as _stat
                    if (_stat.S_ISREG(st.st_mode)
                            and current.get(rel) == (mode, h)):
                        continue          # unchanged regular file, skip
                    _remove_at(pfd, leaf)   # wrong shape/content: clear it
                data = repo.get(h)[1]
                _write_file_at(pfd, leaf, data,
                               0o755 if mode == "100755" else 0o644)
            finally:
                os.close(pfd)
        _prune_empty_dirs(repo)
    finally:
        os.close(root_fd)

def _write_file_at(parent_fd: int, name: str, data: bytes, perm: int):
    """Exclusively create a randomized temp file in the verified parent, write
    and fsync it, then atomically rename it onto `name` and fsync the parent
    directory. No predictable .sbtmp name, no symlink to follow."""
    import stat as _stat
    tmpname = f".sb-{os.urandom(6).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
    fd = os.open(tmpname, flags, perm, dir_fd=parent_fd)
    try:
        # os.write may write fewer bytes than asked (POSIX allows partial
        # writes); loop until every byte has landed
        view = memoryview(data)
        off = 0
        while off < len(view):
            off += os.write(fd, view[off:])
        os.fchmod(fd, perm)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmpname, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        try:
            os.unlink(tmpname, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    try:
        os.fsync(parent_fd)
    except OSError:
        pass

def _prune_empty_dirs(repo: Repo):
    """Remove directories emptied by deletions. Never descends through or
    removes .sb, and never follows symlinks."""
    sbdir = repo.vdir.resolve()
    root = repo.root.resolve()
    for dirpath, dirnames, filenames in os.walk(repo.root, topdown=False,
                                                followlinks=False):
        d = Path(dirpath).resolve()
        if d == root or d == sbdir or sbdir in d.parents:
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass

# --------------------------------------------------------- shared locking ---
# Everyone works in ONE repository — one folder, one database — without
# clone/push/pull. Coordination is by per-file locks, and since 1.4 a lock
# is about CONTENT, not permission:
#
#   * Editing a file auto-locks it to you (detected the next time any sb
#     command scans the tree — sb has no daemon, so acquisition, enforcement
#     and expiry are evaluated lazily but deterministically).
#   * While you hold a lock, YOUR bytes are the file. Anyone may open and
#     edit it, but on the next sb command their version is put back to
#     yours, on disk. Their rejected bytes are kept in the object store and
#     named in the journal ('sb salvage <hash>' writes them out again), so
#     the revert costs nothing permanently — it just refuses to let a second
#     writer's copy become the version of record.
#   * Only you can save that file. Everyone else's save skips it entirely,
#     so a locked file can never be committed by anyone but its holder.
#   * Your own later edits move the lock forward: each command that sees you
#     changed the file again records the new content as the protected one and
#     restarts the expiry clock.
#   * A lock ends when you 'sb save' it, when you put the file back the way
#     it was saved (nothing left to protect), or after an hour of inactivity.
#     On expiry your edits are auto-saved (never lost), then auto-reverted —
#     in history and on disk — so the shared tree returns to its pre-edit
#     state. Bring them back with 'sb restore <auto-save hash>'.
#
# Shared operation is ALWAYS ON since 1.3 — there is no single-user mode
# and no way to turn locking off. A repo used by one person simply never
# sees a foreign lock; the semantics collapse to the old solo behavior.
#
# Attribution: a file on disk carries no note of who edited it, so a lock
# claimed at scan time must not simply go to whoever ran the scan (Alice
# edits, Bob runs 'sb status', Bob must NOT become the lock owner). The one
# signal a shared drive does provide is the file's owner uid: each teammate
# writes as their own OS account. So sb keeps a uid -> identity registry
# (updated whenever anyone runs a command) and locks each found edit to
# the account that owns the file. Where the signal doesn't exist —
# deletions, Windows, uid-squashing network mounts, or one login shared by
# several sb identities — it falls back to the invoking user.
LOCK_TTL = int(os.environ.get("SB_LOCK_TTL", "3600"))   # one hour, seconds
LOCK_DELETED = "deleted"    # held-content marker: the holder removed the file

def shared_mode(repo):
    # 1.3: shared operation is structural, not a setting. Kept as a function
    # so call sites read naturally; it can never return False. The legacy
    # meta key 'shared' is ignored (old journals still display its entries).
    return True

def _my_uid():
    try:
        return os.getuid()
    except AttributeError:          # Windows — no usable file-owner signal
        return None

def register_identity(repo):
    """Remember which OS account maps to which sb identity so edits found
    on disk can be attributed to the person who actually wrote them."""
    uid = _my_uid()
    if uid is None:
        return
    name, email = author()
    key, val = f"uid:{uid}", canonical([name, email]).decode()
    if repo.meta(key) != val:
        with repo.transaction():        # mapping + journal: one transaction
            repo.set_meta(key, val)
            repo.journal("identity", {"uid": uid, "name": name,
                                      "email": email})

def _uid_identity(repo, uid):
    """Best-known (name, email) for an OS account: the registry if that
    person has ever run sb here, else their system account name."""
    v = repo.meta(f"uid:{uid}")
    if v:
        try:
            name, email = json.loads(v)
            return name, email
        except (ValueError, TypeError):
            pass
    try:
        import pwd
        name = pwd.getpwuid(uid).pw_name
    except Exception:
        name = f"uid{uid}"
    return name, f"{name}@uid{uid}"

def _restore_paths_on_disk(repo, targets):
    """Write {rel: (mode, hash) or None} onto the worktree. None removes the
    path. Every write goes through the same no-follow, descriptor-relative,
    atomic machinery checkout uses, so a symlink planted at the path or in a
    parent cannot redirect the write outside the repository."""
    root_fd = os.open(str(repo.root), os.O_RDONLY | _O_DIRECTORY)
    try:
        for rel, mh in sorted(targets.items()):
            pfd, leaf_name = _safe_parent_fd(root_fd, rel)
            try:
                _remove_at(pfd, leaf_name)
                if mh is not None:
                    mode, h = mh
                    _write_file_at(pfd, leaf_name, repo.get(h)[1],
                                   0o755 if mode == "100755" else 0o644)
            finally:
                os.close(pfd)
    finally:
        os.close(root_fd)
    _prune_empty_dirs(repo)

def _only_redaction_differs(repo, tree_files, work, p):
    """True when the on-disk file differs from the last save ONLY because
    the save holds the redacted form of its secrets. Such a file is not
    abandoned work — committing it verbatim on lock expiry would leak the
    secret into history, and reverting it would destroy the user's live
    credential on disk. Expiry leaves these files alone, and a lock on one
    is released rather than enforced forever."""
    if p not in work or p not in tree_files:
        return False
    try:
        data = (repo.root / p).read_bytes()
    except OSError:
        return False
    data2, findings, safe = redact_secrets(data)
    return (bool(findings) and safe
            and hash_obj("blob", data2) == tree_files[p][1])

def foreign_locks(repo):
    """Paths locked by someone other than me. These hold another person's
    in-progress work: they are never overwritten by a checkout, never count
    as my unsaved changes, and are never included in my saves."""
    _, email = author()
    return {p for p, l in repo.locks().items() if l["email"] != email}

def _disk_state(repo, rel):
    """(mode, hash, data, st) for a path, or (None, None, b'', st/None).
    Anything that is not a plain file reads as absent."""
    p = repo.root / rel
    try:
        st = os.lstat(p)
    except OSError:
        return None, None, b"", None
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        return None, None, b"", st
    try:
        data = p.read_bytes()
    except OSError:
        return None, None, b"", st
    mode = "100755" if os.access(p, os.X_OK) else "100644"
    return mode, hash_obj("blob", data), data, st

def _lock_actor(repo, lock, st):
    """Which sb identity most likely made an on-disk change to a locked file.

    The file's owner uid is the only hard signal a shared folder gives, so
    it wins when it is usable: a file now owned by a different account was
    written by that account. When the uid cannot separate the two people —
    the same login used by several sb identities, a uid-squashing mount, a
    deletion with nothing left to stat, or Windows — the best remaining
    signal is who is running sb right now."""
    _, me_email = author()
    uid = getattr(st, "st_uid", None) if st is not None else None
    if uid is not None and lock.get("uid", -1) >= 0:
        if uid != lock["uid"]:
            return _uid_identity(repo, uid)[1]        # another account
        registered = _uid_identity(repo, uid)[1]
        if registered == lock["email"] or me_email == lock["email"]:
            return lock["email"]        # the account really is the holder's
    return me_email

def enforce_locks(repo, quiet=False):
    """Keep every locked file equal to its holder's version.

    For each lock the on-disk content is compared with the content the
    holder is protecting:
      * unchanged            -> nothing to do
      * changed by the holder -> the protected content moves forward, and
                                 the expiry clock restarts
      * changed by anyone else -> the file is put back on disk to the
                                 holder's version; the rejected bytes are
                                 stored and journaled first, so they can be
                                 recovered with 'sb salvage <hash>'
    A lock whose protected content matches the last save is released: there
    is nothing in progress left to protect. Returns True if anything on
    disk was changed."""
    locks = repo.locks()
    if not locks:
        return False
    tree_files, _ = head_tree_files(repo)
    restore, rejected, release, advanced = {}, [], [], []
    for rel in sorted(locks):
        l = locks[rel]
        mode, cur_h, data, st = _disk_state(repo, rel)
        cur = (mode, cur_h) if cur_h is not None else None
        if not l["held"]:
            # a pre-1.4 lock, recorded before content was tracked: adopt
            # what is there now instead of guessing at a version
            repo.update_lock_held(rel, cur_h or LOCK_DELETED,
                                  mode or "100644", touch=False)
            held = cur
        else:
            held = None if l["held"] == LOCK_DELETED else (l["mode"], l["held"])
            if cur != held:
                if _lock_actor(repo, l, st) == l["email"]:
                    new_h = repo.put("blob", data) if cur else LOCK_DELETED
                    repo.update_lock_held(rel, new_h, mode or "100644")
                    held = cur
                    advanced.append(rel)
                else:
                    if cur:
                        rejected.append((rel, repo.put("blob", data),
                                         l["owner"]))
                    restore[rel] = held
        committed = tree_files.get(rel)
        if held == committed or (held is not None
                                 and _only_redaction_differs(
                                     repo, tree_files, {rel: held}, rel)):
            release.append(rel)         # nothing in progress any more
    if restore:
        _restore_paths_on_disk(repo, restore)
        with contextlib.suppress(sqlite3.Error):
            repo.journal("lock-revert", {
                "paths": sorted(restore),
                "kept": {rel: h for rel, h, _ in rejected},
                "by": author()[1]})
        # always reported, never quiet: someone's bytes just left the disk
        print(yellow("reverted ") + dim(f"{len(restore)} file(s) to their "
              "lock holder's version — a locked file is theirs until they "
              "save"))
        rows = []
        keep_h = dict((rel, h) for rel, h, _ in rejected)
        for rel in sorted(restore):
            note = f"held by {locks[rel]['owner']}"
            if rel in keep_h:
                note += f" · your version kept as {short(keep_h[rel])}"
            rows.append(f"{cyan(rel)}  " + dim(note))
        tree_print(rows)
        leaf(dim("recover your version any time: sb salvage <hash> [<path>]"))
    if release:
        repo.clear_locks(release)
    if advanced and not quiet:
        tree_print([f"lock on {cyan(p)} " + dim("follows your latest edit")
                    for p in sorted(advanced)])
    return bool(restore)

def process_lock_expiry(repo):
    """Handle expired locks. For each owner's abandoned edits:
      1. auto-save them as a commit in the owner's name — nothing is lost;
      2. auto-revert those paths, in history AND on disk, to their pre-edit
         state, so the shared tree goes back to the way the owner found it.
    The edits stay one command away: 'sb restore <auto-save hash>' (the hash
    is printed and is in 'sb log'/'sb journal'). Runs at the start of every
    state-touching command so an abandoned lock never blocks the team.
    Returns True if anything was committed.

    The auto-save is a PRESERVATION commit, so it saves the bytes verbatim:
    redacting here and then reverting the disk would destroy the only copy
    of a live credential. If the snapshot contains recognizable secrets sb
    says so loudly and flags it in the journal; files whose only difference
    from the last save is a redacted secret are skipped entirely."""
    register_identity(repo)
    now = int(time.time())
    expired = [(p, l) for p, l in repo.locks().items()
               if now - l["since"] >= LOCK_TTL]
    if not expired:
        return False
    # write=True: the auto-save commits these exact stored blobs, never a
    # later re-read of the worktree
    work = snapshot_worktree(repo, write=True)
    tree_files, head_c = head_tree_files(repo)
    committed = False
    # group expired locks by owner so each person's abandoned edits land as
    # one save + one revert attributed to them
    by_owner = {}
    for p, l in expired:
        by_owner.setdefault((l["owner"], l["email"]), []).append(p)
    for (owner, email), paths in by_owner.items():
        changed = [p for p in paths
                   if work.get(p) != tree_files.get(p)
                   and not _only_redaction_differs(repo, tree_files, work, p)]
        if not changed:
            continue
        prior = {p: tree_files.get(p) for p in changed}   # pre-edit state
        secretish = sorted(p for p in changed if p in work
                           and scan_secrets(repo.get(work[p][1])[1]))
        extra = {"secrets_present": secretish} if secretish else None
        h1 = _commit_subset(repo, work, tree_files, head_c, changed,
                            f"auto-save: {owner}'s expired lock(s)",
                            owner=owner, email=email, op="autosave",
                            extra=extra)
        tree_files, head_c = head_tree_files(repo)  # refresh after commit
        # the revert commit: the same paths, back to their pre-edit content
        # (a path that didn't exist before is deleted again)
        revert_work = {p: mh for p, mh in prior.items() if mh is not None}
        _commit_subset(repo, revert_work, tree_files, head_c, changed,
                       f"auto-revert: {owner}'s expired edits "
                       f"(bring them back: sb restore {short(h1)})",
                       owner=owner, email=email, op="autosave")
        tree_files, head_c = head_tree_files(repo)
        # and put the pre-edit content back on disk, symlink-safely
        _restore_paths_on_disk(repo, prior)
        committed = True
        print(dim(f"auto-saved {owner}'s expired edits as ")
              + amber(short(h1))
              + dim(", then reverted them (redo: sb restore "
                    f"{short(h1)}): " + ", ".join(changed[:4])
                    + (" …" if len(changed) > 4 else "")))
        if secretish:
            print(yellow("warning: ") + dim("that auto-save contains "
                  "recognizable secrets (kept verbatim so the revert loses "
                  "nothing): " + ", ".join(secretish[:4])))
    repo.clear_locks([p for p, _ in expired])
    return committed

def acquire_locks_for_edits(repo, quiet=False):
    """Lock every modified file that isn't already locked — each to the
    person who actually edited it, recording the exact content that person
    is protecting. This is the 'editing auto-locks' rule, applied lazily by
    whichever sb invocation scans the tree next.

    The snapshot is taken with write=True on purpose: a lock has to be able
    to put its holder's version back, so those bytes must be in the store
    before anyone else can overwrite the file.

    Attribution goes by the file's owner uid, not the invoking user: if Bob
    runs 'sb status' and finds Alice's unsaved edit, the lock is created in
    Alice's name (from the uid registry, or her system account name if she
    has never run sb here). Deletions leave nothing to stat, and Windows has
    no owner uid — those fall back to the invoker.

    A file already locked by someone else differs from the last commit
    because their protected version sits on disk. That is THEIR work, not
    mine — I neither lock it nor am blocked by it."""
    register_identity(repo)
    name, email = author()
    me_uid = _my_uid()
    work = snapshot_worktree(repo, write=True)
    tree_files, _ = head_tree_files(repo)
    a, m, d = worktree_vs_tree(work, tree_files)
    edited = set(a) | set(m) | set(d)
    locks = repo.locks()
    # unlocked edits, grouped by who owns the file on disk
    mine_new, theirs = [], {}
    for p in edited:
        if p in locks:
            continue
        owner_uid = None
        if me_uid is not None and p in work:      # deletions: nothing to stat
            try:
                owner_uid = os.lstat(repo.root / p).st_uid
            except OSError:
                pass
        if owner_uid is None or owner_uid == me_uid:
            mine_new.append(p)
        else:
            theirs.setdefault(owner_uid, []).append(p)
    if mine_new or theirs:
        base = repo.head_commit() or ""
        if mine_new:
            for p in mine_new:
                mode, h = work.get(p, ("100644", None))
                repo.set_lock(p, name, email, base, held=h or LOCK_DELETED,
                              mode=mode,
                              uid=-1 if me_uid is None else me_uid)
            if not quiet:
                tree_print([f"locked {cyan(p)} " + dim("(your version wins "
                            "until you save)") for p in sorted(mine_new)])
        for uid, paths in sorted(theirs.items()):
            o_name, o_email = _uid_identity(repo, uid)
            for p in sorted(paths):
                mode, h = work.get(p, ("100644", None))
                repo.set_lock(p, o_name, o_email, base,
                              held=h or LOCK_DELETED, mode=mode, uid=uid)
            if not quiet:
                tree_print([f"locked {cyan(p)} to {bold(o_name)} "
                            + dim("(their on-disk edit)")
                            for p in sorted(paths)])

def sync_locks(repo, quiet=False):
    """The whole lock lifecycle, in the only order that keeps content
    honest: put every locked file back to its holder's version, retire
    abandoned locks, then claim whatever is newly edited. Every command
    that reads or writes repository state runs this first."""
    enforce_locks(repo, quiet=quiet)
    process_lock_expiry(repo)
    acquire_locks_for_edits(repo, quiet=quiet)

def _ago(ts):
    s = max(0, int(time.time()) - ts)
    if s < 60:  return f"{s}s ago"
    if s < 3600: return f"{s//60}m ago"
    return f"{s//3600}h{(s%3600)//60}m ago"

def _commit_subset(repo, work, tree_files, head_c, subset, message,
                   *, owner=None, email=None, op="save", extra=None,
                   verify_drift=False, disk_ref=None):
    """Commit a NEW tree that equals the last committed tree with only
    `subset` paths replaced by their `work` version. This is how 'save my
    changes' leaves everyone else's in-progress edits untouched in the
    commit. Returns the new commit hash.

    The tree is built from the blob hashes ALREADY IN `work` — the snapshot
    (or candidate) the caller scanned and gated — never from a fresh re-read
    of the worktree. What passed the gates is byte-for-byte what lands in
    history. Every referenced blob must already be in the store (snapshot
    with write=True, or repo.put by the caller); a missing one is corruption
    and raises rather than being papered over with a disk read.

    verify_drift=True re-checks, inside the commit transaction, that the
    files on disk still match `disk_ref` (default: `work`) and refuses if
    the worktree moved while the caller was scanning and testing — the same
    guard the whole-tree save path applies. Pass disk_ref=<original disk
    snapshot> when `work` holds rewritten (e.g. redacted) blobs that are
    intentionally different from the disk."""
    merged = dict(tree_files)
    for rel in subset:
        if rel in work:
            merged[rel] = work[rel]
        else:
            merged.pop(rel, None)          # a deletion in the subset
    with repo.transaction():
        for rel in subset:
            if rel in work:
                h = work[rel][1]
                if not repo.has(h):
                    raise CorruptObject(
                        f"blob {short(h)} for '{rel}' is not in the store — "
                        f"it was never written before commit")
        if verify_drift:
            ref = disk_ref if disk_ref is not None else work
            recheck = snapshot_worktree(repo, write=False)
            drifted = sorted(p for p in subset
                             if recheck.get(p) != ref.get(p))
            if drifted:
                die("the working tree changed while this save was being "
                    "scanned and tested\n       ("
                    + ", ".join(drifted[:5])
                    + (" …" if len(drifted) > 5 else "")
                    + ")\n       nothing was saved — run 'sb save' again")
        tree_hash = build_tree(repo, merged)
        parents = [head_c["hash"]] if head_c else []
        if owner:
            c = {"tree": tree_hash, "parents": parents, "author": owner,
                 "email": email, "time": int(time.time()), "message": message}
            h = repo.put("commit", canonical(c))
        else:
            h = make_commit(repo, tree_hash, parents, message)
        repo.update_ref(repo.current_branch(), h, op=op,
                        expect=head_c["hash"] if head_c else None,
                        extra=extra)
    return h

def ensure_clean(repo, extra_exempt=None):
    """Refuse to run when the working tree holds changes an operation would
    throw away. Files locked by other people are exempt: their in-progress
    edits are supposed to be sitting there, every checkout preserves them,
    and no operation of mine can commit or destroy them."""
    exempt = foreign_locks(repo) | set(extra_exempt or ())
    work = snapshot_worktree(repo, write=False)
    tree, _ = head_tree_files(repo)
    a, m, d = worktree_vs_tree(work, tree)
    dirty = sorted((set(a) | set(m) | set(d)) - exempt)
    if dirty:
        die("you have unsaved changes — run 'sb save' first (nothing is ever\n"
            "       silently discarded), or 'sb undo -p <path>' to drop them\n"
            "       (" + ", ".join(dirty[:5])
            + (" …" if len(dirty) > 5 else "") + ")")

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

def _mergeable_lines(repo, h):
    """Blob -> list of lines suitable for a byte-preserving line merge, or
    None if the file must not be auto-merged. A file is mergeable only when
    it is valid UTF-8, contains no NUL, uses LF endings exclusively (no CR),
    and ends in a newline — the exact shape that '\\n'.join(...) + '\\n'
    reconstructs without altering a single byte. CRLF files, files with no
    trailing newline, mixed endings, invalid UTF-8, and binaries all return
    None and are reported as conflicts rather than silently rewritten."""
    if h is None:
        return []
    data = repo.get(h)[1]
    if data == b"":
        return []
    if b"\0" in data or b"\r" in data:
        return None
    if not data.endswith(b"\n"):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()                     # the trailing newline, not a line
    # round-trip guard: prove reconstruction is byte-identical before we
    # ever rely on it, so no encoding edge case can slip through
    if ("\n".join(lines) + "\n").encode("utf-8") != data:
        return None
    return lines

# ---------------------------------------------------------- test gates ------
# Versioned test scripts live in sb-tests/<stage>/ so they travel with
# branches and are themselves history. Stages:
#   pre-save    gates every save          (sb save)
#   pre-merge   gates merges, incl. FFs   (sb merge)
#   pre-publish  gates deployments         (sb deploy)
# Scripts run sorted (name them 10-lint.sh, 20-unit.py, ...) inside a
# PRISTINE temp checkout of the exact candidate tree — never your dirty
# worktree — with SB_STAGE / SB_BRANCH / SB_COMMIT / SB_REPO exported.
# Non-zero exit or timeout blocks the operation; --no-verify overrides.
TESTS_DIR = "sb-tests"
STAGES = ("pre-save", "pre-merge", "pre-publish")
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
    sync_locks(repo, quiet=True)
    work = snapshot_worktree(repo, write=False,
                             deep=getattr(args, "deep", False))
    tree, _ = head_tree_files(repo)
    added, modified, deleted = worktree_vs_tree(work, tree)
    head = repo.head_commit()
    print(f"on branch {bold(repo.current_branch())}"
          + (f" {dim('·')} head {amber(short(head))}" if head
             else dim("  (no saves yet)")))
    name, email = author()
    locks = repo.locks()
    if locks:
        rows = []
        for p in sorted(locks):
            l = locks[p]
            mine = l["email"] == email
            who = bold("you") if mine else l["owner"]
            rows.append(f"{cyan(p)}  " + dim(f"locked by {who} "
                        + _ago(l["since"])
                        + ("" if mine else " · their version wins")))
        print(dim("locks:"))
        tree_print(rows)
    if not (added or modified or deleted):
        leaf("working tree clean " + dim("— nothing to save"))
        return
    renames, added_r, deleted_r = detect_renames(work, tree, added, deleted)
    theirs = foreign_locks(repo)
    def mark(p):
        return p + (dim("  (theirs)") if p in theirs else "")
    rows  = [dim("renamed   ") + dim(o) + dim(" → ") + mark(p) for o, p in renames]
    rows += [dim("new       ") + mark(p) for p in added_r]
    rows += [dim("modified  ") + mark(p) for p in modified]
    rows += [dim("deleted   ") + dim(mark(p)) for p in deleted_r]
    tree_print(rows)
    n_mine = len([p for p in (added + modified + deleted) if p not in theirs])
    print(dim(f"run 'sb save \"<message>\"' to snapshot {n_mine} change(s) "
              f"of yours"))

def cmd_save(args):
    repo = need_repo()
    if not args.message:
        die('a message is required:  sb save "<message>"')
    sync_locks(repo, quiet=True)
    if not getattr(args, "global_force", False):
        _save_shared(repo, args)           # the normal path: your files only
        return
    # --global-force: snapshot the ENTIRE worktree, everyone's edits included.
    # Read the worktree ONCE, storing every candidate blob as we go. From this
    # point on, the redaction pass, the secret scan, the test gate, and the
    # commit all operate on this exact stored tree — never on fresh re-reads
    # of the worktree — so what passes the gates is byte-for-byte what gets
    # committed. If the live worktree changes underneath us during testing,
    # we notice and report it rather than committing something that was never
    # scanned or tested. Everything runs inside one transaction so an abort
    # also rolls the candidate blobs (redacted or not) back out of the store.
    with repo.transaction():
        disk = snapshot_worktree(repo, write=True)   # what's really on disk
        tree_files, head_c = head_tree_files(repo)
        added, modified, deleted = worktree_vs_tree(disk, tree_files)
        if not (added or modified or deleted) and head_c:
            print(green("nothing changed — no save created"))
            return
        # redaction pass: recognized credentials in new/changed files are
        # replaced with <REDACTED> in the blob that will be committed. The
        # file on disk is never rewritten. Files that contain secrets but
        # can't be rewritten faithfully (not clean UTF-8) still block.
        over, redacted, hard_blocked = _redact_for_commit(
            repo, disk, added + modified, args.allow_secrets)
        if hard_blocked:
            _report_hard_blocked(hard_blocked)
        work = dict(disk)                  # the candidate tree to commit
        work.update(over)
        # redaction can make a "change" identical to what's already saved;
        # re-check so we don't create an empty save
        a2, m2, d2 = worktree_vs_tree(work, tree_files)
        if not (a2 or m2 or d2) and head_c:
            print(green("nothing new to save — the only changes were "
                        "redacted secrets already saved as <REDACTED>"))
            return
        # gate: materialize the stored candidate tree (from_worktree=False) so
        # the tests run exactly the bytes that will be committed
        if not args.no_verify:
            if not run_stage(repo, "pre-save", work, from_worktree=False):
                die("pre-save tests failed — save blocked "
                    "(--no-verify to override)")
        # guard: if the worktree drifted from the snapshot while we scanned
        # and tested, refuse rather than commit an untested state (compared
        # against `disk`, since redaction makes `work` differ by design)
        recheck = snapshot_worktree(repo, write=False)
        if recheck != disk:
            drifted = sorted(set(disk) ^ set(recheck)) or \
                sorted(p for p in disk if disk.get(p) != recheck.get(p))
            die("the working tree changed while this save was being scanned "
                "and tested\n       (" + ", ".join(drifted[:5])
                + (" …" if len(drifted) > 5 else "")
                + ")\n       nothing was saved — run 'sb save' again")
        tree_hash = build_tree(repo, work)
        parents = [head_c["hash"]] if head_c else []
        h = make_commit(repo, tree_hash, parents, args.message)
        bypass = {"global_force": True}
        if args.no_verify:    bypass["skipped_tests"] = True
        if args.allow_secrets: bypass["skipped_secret_scan"] = True
        if redacted:
            bypass["secrets_redacted"] = sorted(rel for rel, _ in redacted)
        repo.update_ref(repo.current_branch(), h, op="save",
                        expect=head_c["hash"] if head_c else None,
                        extra=bypass)
        # --global-force sweeps in everyone's edits, so all locks are resolved
        repo.clear_locks(list(repo.locks().keys()))
    n = len(added) + len(modified) + len(deleted)
    print(f"{bold('saved')} {amber(short(h))} "
          f"{dim('on')} {bold(repo.current_branch())} {dim('·')} {dim(str(n) + ' file(s)')}")
    leaf(f'"{args.message}"  ' + dim("· everyone's edits · all locks released"))
    _report_redactions(redacted)

def _report_redactions(redacted):
    if not redacted:
        return
    print(yellow("secrets redacted in the save (working files untouched):"))
    tree_print([yellow(rel) + dim("  " + " · ".join(
        f"line {ln}: {lb}" for ln, lb in fs[:3])
        + (" …" if len(fs) > 3 else "")) for rel, fs in redacted])
    leaf(dim("history holds <REDACTED>; move live credentials to env vars or "
             "an ignored file (sb ignore <pattern>) so this stops recurring"))

def _save_shared(repo, args):
    """The normal save: commit only the current user's own edits (the files
    they hold locks on / have changed), leaving everyone else's in-progress
    work untouched in the commit. Releases the user's locks.

    A file locked by somebody else is never included, whatever is on disk —
    their lock means their version is the file, and only they can commit it.

    Consistency contract (same as the --global-force whole-tree path): the
    worktree is read ONCE into stored blobs; redaction, the secret check,
    the test gate, and the commit all operate on those exact blobs; and the
    commit re-checks the disk against the original snapshot so a worktree
    that drifted during testing aborts the save instead of smuggling
    unscanned, untested bytes into history."""
    name, email = author()
    with repo.transaction():
        # one transaction end to end: an abort at any point also rolls the
        # candidate blobs (redacted or not) back out of the object store
        disk = snapshot_worktree(repo, write=True)   # what's really on disk
        tree_files, head_c = head_tree_files(repo)
        locks = repo.locks()
        mine = sorted(p for p, l in locks.items() if l["email"] == email)
        # also include files I changed that somehow aren't locked (defensive)
        a, m, d = worktree_vs_tree(disk, tree_files)
        mine = sorted(set(mine) | {p for p in (set(a) | set(m) | set(d))
                                   if locks.get(p, {}).get("email", email) == email})
        changed = [p for p in mine if disk.get(p) != tree_files.get(p)]
        if not changed:
            print(green("nothing of yours to save"))
            held = sorted(p for p, l in locks.items() if l["email"] != email)
            if held:
                leaf(dim(f"{len(held)} file(s) belong to other people's "
                         "locks: " + ", ".join(held[:4])
                         + (" …" if len(held) > 4 else "")))
            return
        # redaction pass: recognized credentials are replaced with <REDACTED>
        # in the blob that will be committed; the file on disk is never
        # rewritten. Files with secrets that can't be rewritten faithfully
        # (not clean UTF-8) still block the save.
        over, redacted, hard_blocked = _redact_for_commit(
            repo, disk, changed, args.allow_secrets)
        if hard_blocked:
            _report_hard_blocked(hard_blocked)
        work = dict(disk)                  # the candidate content to commit
        work.update(over)
        # redaction can make a "change" identical to what's already saved
        # (the file's only difference was a secret already stored as
        # <REDACTED>); re-filter so we don't create an empty save
        changed = [p for p in changed if work.get(p) != tree_files.get(p)]
        if not changed:
            print(green("nothing new to save — the only changes were "
                        "redacted secrets already saved as <REDACTED>"))
            repo.clear_locks(mine)
            return
        if not args.no_verify:
            candidate = dict(tree_files)
            for rel in changed:
                if rel in work:
                    candidate[rel] = work[rel]
                else:
                    candidate.pop(rel, None)
            if not run_stage(repo, "pre-save", candidate, from_worktree=False):
                die("pre-save tests failed — save blocked "
                    "(--no-verify to override)")
        bypass = {}
        if args.no_verify:     bypass["skipped_tests"] = True
        if args.allow_secrets: bypass["skipped_secret_scan"] = True
        if redacted:
            bypass["secrets_redacted"] = sorted(rel for rel, _ in redacted)
        # verify_drift + disk_ref=disk: the commit is refused if any of these
        # files changed on disk after the snapshot (the gate/scan window).
        # disk_ref is the ORIGINAL snapshot because `work` may hold redacted
        # blobs that legitimately differ from the disk.
        h = _commit_subset(repo, work, tree_files, head_c, changed,
                           args.message, owner=name, email=email, op="save",
                           extra=bypass or None,
                           verify_drift=True, disk_ref=disk)
        repo.clear_locks(changed)
    print(f"{bold('saved')} {amber(short(h))} "
          f"{dim('on')} {bold(repo.current_branch())} {dim('·')} "
          f"{dim(str(len(changed)) + ' of your file(s)')}")
    leaf(f'"{args.message}"  ' + dim("· locks released"))
    _report_redactions(redacted)
    others = sorted(p for p, l in repo.locks().items() if l["email"] != email)
    if others:
        leaf(dim(f"{len(others)} file(s) still locked by others — not included"))

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
        rows = c["message"].splitlines() or ['""']
        # what this save changed, relative to its first parent — so the log
        # shows every action's effect, not just its message
        try:
            ptree = (read_tree(repo, parse_commit(repo, c["parents"][0])["tree"])
                     if c["parents"] else {})
            ctree = read_tree(repo, c["tree"])
            a, m, d = worktree_vs_tree(ctree, ptree)
            rn, a, d = detect_renames(ctree, ptree, a, d)
            bits = []
            if a: bits.append(f"+{len(a)} new")
            if m: bits.append(f"~{len(m)} modified")
            if d: bits.append(f"-{len(d)} deleted")
            bits += [f"{o} → {n}" for o, n in rn[:3]]
            if len(rn) > 3:
                bits.append(f"…{len(rn) - 3} more renamed")
            if bits:
                rows.append(dim(" · ".join(bits)))
        except (CorruptObject, KeyError):
            pass                        # damaged history: message still shows
        tree_print(rows)
        count += 1
        if args.limit and count >= args.limit:
            break

def cmd_diff(args):
    repo = need_repo()
    tree, _ = head_tree_files(repo)
    work = snapshot_worktree(repo, write=False)
    added, modified, deleted = worktree_vs_tree(work, tree)
    renames, added, deleted = detect_renames(work, tree, added, deleted)
    targets = added + modified + deleted
    if args.path:
        want = args.path.rstrip("/")
        targets = [t for t in targets if t == want or t.startswith(want + "/")]
        renames = [(o, n) for o, n in renames
                   if o == want or n == want
                   or o.startswith(want + "/") or n.startswith(want + "/")]
    if not targets and not renames:
        print(dim("no differences"))
        return
    theirs = foreign_locks(repo)
    for o, n in renames:
        print(amber(f"@@ {o} → {n}") + dim("  renamed (content identical)"))
    for rel in sorted(targets):
        old_b = repo.get(tree[rel][1])[1] if rel in tree else b""
        new_b = (repo.root / rel).read_bytes() if rel in work else b""
        if rel in theirs:
            print(amber(f"@@ {rel}") + dim("  (another user's locked edit)"))
        if b"\0" in old_b[:8000] or b"\0" in new_b[:8000]:
            print(amber(f"@@ {rel}") + dim(
                f"  binary file differs ({len(old_b):,} → {len(new_b):,} bytes)"))
            continue
        old = old_b.decode("utf-8", "replace").splitlines()
        new = new_b.decode("utf-8", "replace").splitlines()
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

def cmd_undo(args):
    """Non-destructive undo. Plain: a NEW save whose content equals the
    previous save (history is never rewritten; run undo again to redo).
    With -p <path>: bring just that file or folder back from the last
    save, overwriting the working copy — no new save is created."""
    repo = need_repo()
    sync_locks(repo, quiet=True)
    keep = foreign_locks(repo)
    if args.path:                       # targeted: un-mangle one path
        tree, _ = head_tree_files(repo)
        rel = args.path.rstrip("/")
        matches = [rel] if rel in tree else \
                  [p for p in tree if p.startswith(rel + "/")]
        if not matches:
            die(f"'{rel}' is not in the last save")
        blocked = sorted(set(matches) & keep)
        if blocked:
            die("someone else holds a lock on "
                + ", ".join(blocked[:4]) + (" …" if len(blocked) > 4 else "")
                + "\n       their version is the file until they save it "
                  "(see 'sb locks')")
        # write through the same no-follow, descriptor-relative path that
        # checkout uses — a symlinked parent or a symlink squatting on the
        # target name must never redirect the restore outside the repo
        root_fd = os.open(str(repo.root), os.O_RDONLY | _O_DIRECTORY)
        try:
            for m in matches:
                mode, h = tree[m]
                try:
                    pfd, fn = _safe_parent_fd(root_fd, m)
                except CheckoutConflict as e:
                    die(str(e))
                try:
                    _remove_at(pfd, fn)      # clear symlink/dir in the way
                    _write_file_at(pfd, fn, repo.get(h)[1],
                                   0o755 if mode == "100755" else 0o644)
                finally:
                    os.close(pfd)
        finally:
            os.close(root_fd)
        repo.clear_locks(matches)       # the edit is gone: so is its lock
        what = cyan(rel) if len(matches) == 1 else \
            f"{len(matches)} file(s) under {rel}/"
        print(f"brought back {what} from the last save")
        return
    head = repo.head_commit()
    if not head:
        die("nothing to undo")
    ensure_clean(repo)
    c = parse_commit(repo, head)
    if not c["parents"]:
        die("cannot undo the very first save")
    parent = parse_commit(repo, c["parents"][0])
    _checkout_preserving(repo, read_tree(repo, parent["tree"]),
                         read_tree(repo, c["tree"]), keep)
    msg = c["message"].splitlines()[0]
    with repo.transaction():            # commit point: one transaction
        h = make_commit(repo, parent["tree"], [head], f"undo: {msg}")
        repo.update_ref(repo.current_branch(), h, op="undo", expect=head)
    print(f"{bold('undone')} {dim('— created')} {amber(short(h))}")
    leaf(f'reverts "{msg}"  '
         + dim("(history preserved; sb undo again to redo)"))

def _journal_tips_at(repo, seq):
    """Replay the journal up to entry `seq` and return the branch tips
    as recorded at that point: {branch: commit_hash}."""
    tips = {}
    for e in repo.journal_entries():
        if e["seq"] > seq:
            break
        op, d = e["op"], e["detail"]
        if op in REF_OPS:
            tips[d["branch"]] = d["new"]
        elif op == "branch-remove":
            tips.pop(d["branch"], None)
    return tips

def _resolve_restore(repo, what):
    """Resolve a restore target — journal anchor, save-hash prefix, release
    label, or branch — to (commit_hash, how). Anchors resolve to the
    CURRENT branch's tip as of that journal moment."""
    hits = []
    recs = [e for e in repo.journal_entries()
            if e["op"] in ("publish", "deploy")
            and e["detail"].get("label") == what]
    if recs:
        hits.append((recs[-1]["detail"]["commit"], f"release '{what}'"))
    if what in repo.branches():
        t = repo.tip(what)
        if t:
            hits.append((t, f"branch '{what}'"))
    w = (what or "").strip().lower().rstrip(".\u2026")
    if re.fullmatch(r"[0-9a-f]{4,64}", w):
        rows = repo.db.execute(
            "SELECT hash FROM objects WHERE kind='commit' AND hash LIKE ?",
            (w + "%",)).fetchall()
        if len(rows) > 1:
            die(f"'{what}' matches {len(rows)} saves — give more characters")
        if len(rows) == 1:
            hits.append((rows[0][0], "save " + short(rows[0][0])))
        if len(w) >= 8:                    # anchors are 8-64 hex (sb prints 16)
            marks = [e for e in repo.journal_entries()
                     if e["link"].startswith(w)]
            if len(marks) > 1:
                die(f"'{what}' matches {len(marks)} journal entries — "
                    f"give more characters")
            if marks:
                e = marks[0]
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
                branch = repo.current_branch()
                tip = _journal_tips_at(repo, e["seq"]).get(branch)
                if not tip:
                    existed = sorted(b for b, t in
                                     _journal_tips_at(repo, e["seq"]).items() if t)
                    die(f"at anchor {w[:16]} ({when}) branch '{branch}' had no "
                        f"saves yet" + (f"\n       branches with saves then: "
                                        f"{', '.join(existed)}" if existed else ""))
                hits.append((tip, f"anchor {w[:16]} ({when}, {branch})"))
    seen, uniq = set(), []                 # one name can match twice; same
    for h, how in hits:                    # commit found two ways is fine
        if h not in seen:
            seen.add(h); uniq.append((h, how))
    if not uniq:
        hint = ""
        if "/" in (what or "") or (repo.root / (what or "")).exists():
            hint = (f"\n       to bring a file back from the last save: "
                    f"sb undo -p {what}")
        die(f"nothing named '{what}' — not an anchor, save hash, release "
            f"label, or branch\n       (anchors: sb journal · saves: sb log · "
            f"labels: sb publish -l)" + hint)
    if len(uniq) > 1:
        die(f"'{what}' is ambiguous — it matches "
            + " and ".join(how for _, how in uniq)
            + "\n       give more characters or use the full form")
    return uniq[0]

def cmd_restore(args):
    """Create a NEW save whose content equals the chosen past state.
    Like undo, but to any point: nothing is rewound or deleted, and
    running undo right after returns to the pre-restore state."""
    repo = need_repo()
    sync_locks(repo, quiet=True)
    head = repo.head_commit()
    if not head:
        die("no saves yet — nothing to restore")
    ensure_clean(repo)
    commit_hash, how = _resolve_restore(repo, args.target)
    c = parse_commit(repo, commit_hash)
    if commit_hash == head or c["tree"] == parse_commit(repo, head)["tree"]:
        print(green("already at that state — nothing to do"))
        return
    target_tree = read_tree(repo, c["tree"])     # every blob re-hash-verified
    cur_tree, _ = head_tree_files(repo)
    _checkout_preserving(repo, target_tree, cur_tree, foreign_locks(repo))
    with repo.transaction():            # commit point: one transaction
        h = make_commit(repo, c["tree"], [head], f"restore: to {how}")
        repo.update_ref(repo.current_branch(), h, op="restore", expect=head)
    print(f"{bold('restored')} {dim('to')} {how} {dim('— created')} "
          f"{amber(short(h))}")
    leaf(dim("history preserved — nothing deleted; sb undo returns you"))

def _create_branch(repo, branch, head, allow_secrets=False):
    """Create `branch` and give it an immediate first save of the working
    folder, messaged "Initial branch creation".

    A branch is therefore born with content: it can be switched to, tested,
    and merged straight away, and there is no "save something first" step
    and no branch that exists only as a name. Files another person holds a
    lock on are taken from the last save instead of from disk — their
    in-progress work is theirs to commit — and recognized credentials are
    redacted exactly as a normal save redacts them. If the current branch
    has never been saved, it is seeded with this same commit so both
    branches start from one common first save (which is what makes an
    immediate merge meaningful)."""
    name, email = author()
    keep = foreign_locks(repo)
    seeded = None
    with repo.transaction():
        disk = snapshot_worktree(repo, write=True)
        base_tree = (read_tree(repo, parse_commit(repo, head)["tree"])
                     if head else {})
        work = dict(disk)
        for p in sorted(keep):
            if p in base_tree:
                work[p] = base_tree[p]      # their lock: use the saved version
            else:
                work.pop(p, None)           # their unsaved new file: not mine
        over, redacted, hard = _redact_for_commit(repo, work, sorted(work),
                                                  allow_secrets)
        if hard:
            _report_hard_blocked(hard, what="branch")
        work.update(over)
        tree_hash = build_tree(repo, work)
        c = {"tree": tree_hash, "parents": [head] if head else [],
             "author": name, "email": email, "time": int(time.time()),
             "message": "Initial branch creation"}
        h = repo.put("commit", canonical(c))
        extra = {"initial": True}
        if redacted:
            extra["secrets_redacted"] = sorted(rel for rel, _ in redacted)
        repo.update_ref(branch, h, op="branch", expect=None, extra=extra)
        cur = repo.current_branch()
        if cur != branch and not repo.tip(cur):
            # nothing has ever been saved here: give the branch we are
            # standing on the same first save, so the two share a base
            repo.update_ref(cur, h, op="branch", expect=None,
                            extra={"initial": True, "seeded_from": branch})
            seeded = cur
    return h, len(work), redacted, seeded

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
    sync_locks(repo, quiet=True)
    head = repo.head_commit()
    h, n_files, redacted, seeded = _create_branch(
        repo, args.name, head, allow_secrets=getattr(args, "allow_secrets", False))
    print(f"{dim('created branch')} {bold(args.name)} {dim('at')} "
          f"{amber(short(h))}")
    rows = [f'initial save {amber(short(h))}  ' + dim('"Initial branch '
            f'creation" · {n_files} file(s) from this folder')]
    if seeded:
        rows.append(dim(f"'{seeded}' had no saves yet — it now starts from "
                        "this same first save"))
    rows.append(dim(f"switch to it: sb switch {args.name}  ·  it is "
                    "mergeable immediately"))
    tree_print(rows)
    _report_redactions(redacted)

def cmd_switch(args):
    repo = need_repo()
    if args.target not in repo.branches():
        die(f"no branch named '{args.target}' "
            f"(sandbox has no detached mode; create it first: sb branch {args.target})")
    if args.target == repo.current_branch():
        print(f"already on {bold(args.target)}")
        return
    sync_locks(repo, quiet=True)
    target_commit = repo.tip(args.target)
    target_tree = (read_tree(repo, parse_commit(repo, target_commit)["tree"])
                   if target_commit else {})
    keep = foreign_locks(repo)
    work = snapshot_worktree(repo, write=False)
    # If the folder is already exactly what the target branch holds, there is
    # nothing to lose and nothing to write — this is what lets 'sb branch x'
    # (which saves the folder onto x) be followed straight away by
    # 'sb switch x' without an intervening save.
    if ({k: v for k, v in work.items() if k not in keep}
            != {k: v for k, v in target_tree.items() if k not in keep}):
        ensure_clean(repo)
    _checkout_preserving(repo, target_tree, work, keep)
    with repo.transaction():            # pointer + journal: one transaction
        repo.set_meta("branch", args.target)
        repo.journal("switch", {"to": args.target, "tip": target_commit or ""})
    print(f"{dim('switched to')} {bold(args.target)} {amber(short(target_commit))}")
    if keep:
        leaf(dim(f"{len(keep)} file(s) locked by others were left as they are"))

def _parents(repo, h):
    return parse_commit(repo, h)["parents"]

def find_merge_base(repo, a, b):
    """Best common ancestor in the commit DAG. Returns a common ancestor
    that has no descendant which is also a common ancestor (a lowest common
    ancestor). A first-shared-commit DFS is wrong once merges exist, because
    it can return an ancestor of the real base and feed the three-way merge a
    stale base. When several independent LCAs exist (criss-cross history) the
    result is deterministic (lowest commit time, then hash); such histories
    are rare in sb's single-writer model, and a merge from a slightly older
    base is conservative — it can only produce more conflicts, never a
    silently wrong auto-merge."""
    anc_a = {c["hash"] for c in walk_history(repo, a)}
    common = [c["hash"] for c in walk_history(repo, b) if c["hash"] in anc_a]
    if not common:
        return None
    common_set = set(common)
    # discard any common ancestor that is itself an ancestor of another
    # common ancestor — those are not "lowest"
    dominated = set()
    for h in common:
        for p in _parents(repo, h):
            # walk up from h's parents; anything common we reach is dominated
            stack = [p]
            seen = set()
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                if x in common_set:
                    dominated.add(x)
                stack.extend(_parents(repo, x))
    lcas = [h for h in common if h not in dominated]
    if not lcas:
        lcas = common
    def key(h):
        c = parse_commit(repo, h)
        return (c["time"], h)
    return sorted(lcas, key=key)[-1]

def cmd_merge(args):
    repo = need_repo()
    sync_locks(repo, quiet=True)
    theirs_tip = repo.resolve(args.branch)
    if theirs_tip is None:
        die(f"unknown branch '{args.branch}'")
    ours_tip = repo.head_commit()
    if not ours_tip:
        die("current branch has no saves")
    if theirs_tip == ours_tip:
        print(green("already up to date")); return
    # shared mode: a merge that would change a file someone else is actively
    # editing (holds a lock on) is refused, so it can't clobber their work.
    # --ignore skips those files: the merge proceeds for everything else and
    # each locked file is kept at your current version, its lock untouched.
    name, email = author()
    ignore_locked = getattr(args, "ignore", False)
    skip = set()
    theirs_tree_pre = read_tree(repo, parse_commit(repo, theirs_tip)["tree"])
    ours_tree_pre = read_tree(repo, parse_commit(repo, ours_tip)["tree"])
    touched = {rel for rel in set(theirs_tree_pre) | set(ours_tree_pre)
               if theirs_tree_pre.get(rel) != ours_tree_pre.get(rel)}
    blocked = [(p, l) for p, l in repo.locks().items()
               if p in touched and l["email"] != email]
    if blocked and not ignore_locked:
        print(red("merge blocked — it would change files locked by others:"))
        tree_print([f"{red(p)}  " + dim(f"locked by {l['owner']} "
                    + _ago(l["since"])) for p, l in blocked])
        print(yellow("warning: ") + dim("these files hold others' "
              "in-progress edits."))
        die("re-run with --ignore to merge everything else and leave these\n"
            f"       files (and their locks) as they are:  "
            f"sb merge {args.branch} --ignore")
    if ignore_locked:
        skip = {p for p, _ in blocked}
    # everyone else's locked files are exempt from the clean check (they are
    # preserved on disk, never merged over); the rest of the tree must be clean
    ensure_clean(repo, extra_exempt=skip)
    preserve = skip | foreign_locks(repo)
    base = find_merge_base(repo, ours_tip, theirs_tip)
    ours_tree = read_tree(repo, parse_commit(repo, ours_tip)["tree"])
    theirs_tree = read_tree(repo, parse_commit(repo, theirs_tip)["tree"])
    # a clean fast-forward takes theirs wholesale, which would change skipped
    # files — so only fast-forward when no skipped file actually differs
    ff_ok = base == ours_tip and not any(
        ours_tree.get(p) != theirs_tree.get(p) for p in skip)
    if ff_ok:
        if not args.no_verify:
            if not run_stage(repo, "pre-merge", theirs_tree, commit=theirs_tip):
                die("pre-merge tests failed — merge blocked (--no-verify to override)")
        _checkout_preserving(repo, theirs_tree, ours_tree, preserve)
        repo.update_ref(repo.current_branch(), theirs_tip, op="merge",
                        expect=ours_tip)
        print(f"{bold('fast-forwarded')} {dim('to')} {amber(short(theirs_tip))}")
        return
    if base == theirs_tip:
        print(green("already contains that branch")); return
    base_tree = read_tree(repo, parse_commit(repo, base)["tree"]) if base else {}
    merged, conflicts, auto_merged = {}, [], []
    for rel in sorted(set(base_tree) | set(ours_tree) | set(theirs_tree)):
        if rel in skip:
            merged[rel] = ours_tree.get(rel)   # keep our version, don't touch
            continue
        b = base_tree.get(rel); o = ours_tree.get(rel); t = theirs_tree.get(rel)
        if o == t:                        merged[rel] = o
        elif t == b:                      merged[rel] = o     # only we changed
        elif o == b:                      merged[rel] = t     # only they changed
        elif o is None or t is None:      conflicts.append((rel, "changed vs deleted"))
        else:
            # both sides changed the same file: try a line-level 3-way merge,
            # but only when every side can be reconstructed byte-for-byte
            bl = _mergeable_lines(repo, b[1] if b else None)
            ol = _mergeable_lines(repo, o[1])
            tl = _mergeable_lines(repo, t[1])
            if bl is None or ol is None or tl is None:
                conflicts.append((rel, "not safely auto-mergeable "
                                       "(binary, CRLF, or no trailing newline)"))
                continue
            if o[0] != t[0]:              # same content path, different mode
                conflicts.append((rel, "executable bit differs"))
                continue
            lines, n = merge3(bl, ol, tl)
            if n:
                conflicts.append((rel, f"{n} overlapping change(s)"))
                continue
            merged[rel] = (o[0], repo.put("blob",
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
    # write the merged tree, but DON'T touch preserved files on disk — their
    # holders' in-progress edits must survive. We do this by telling checkout
    # those paths are already correct (current == target for them).
    checkout_current = dict(ours_tree)
    checkout_target = dict(merged)
    for p in preserve:
        checkout_target[p] = ours_tree.get(p)
        checkout_current[p] = ours_tree.get(p)   # marks as unchanged -> skipped
    checkout_target = {k: v for k, v in checkout_target.items() if v is not None}
    checkout_current = {k: v for k, v in checkout_current.items() if v is not None}
    _checkout_preserving(repo, checkout_target, checkout_current, preserve)
    with repo.transaction():            # commit point: one transaction
        tree_hash = build_tree(repo, merged)
        # A merge that skipped locked files did NOT incorporate all of
        # theirs — recording theirs_tip as a parent would be false ancestry:
        # a later merge would say "already contains that branch" and the
        # skipped changes could never arrive. A partial merge is therefore a
        # single-parent commit; re-running the merge after the locks release
        # picks up exactly the skipped files (everything else is content-
        # identical by then and merges clean).
        parents = [ours_tip] if skip else [ours_tip, theirs_tip]
        label = (f"partial merge {args.branch} into {repo.current_branch()} "
                 f"({len(skip)} locked file(s) skipped)" if skip else
                 f"merge {args.branch} into {repo.current_branch()}")
        h = make_commit(repo, tree_hash, parents, label)
        repo.update_ref(repo.current_branch(), h, op="merge", expect=ours_tip)
    print(f"{bold('merged')} {args.branch} {dim('→')} {bold(repo.current_branch())} "
          f"{dim('as')} {amber(short(h))}")
    if auto_merged:
        leaf(dim(f"{len(auto_merged)} file(s) auto-merged line by line"))
    if skip:
        leaf(yellow(f"skipped {len(skip)} locked file(s) — kept your version, "
                    "locks untouched: " + ", ".join(sorted(skip)[:4])
                    + (" …" if len(skip) > 4 else "")))
        leaf(dim(f"this merge is recorded as partial — run "
                 f"'sb merge {args.branch}' again after the locks release "
                 "to bring in the skipped files"))

TEST_GUIDE = f"""\

{bold('setting up test scripts')}
Tests are plain executable scripts inside {bold('sb-tests/<stage>/')} in your
repo. Any language works — sb only cares about the {bold('exit code')}:
exit {bold('0')} means pass, anything else means fail.

{amber('stages')}
  {amber('\u251c\u2500\u2500\u2500')} pre-save    {dim('runs before every save — the gate for bad snapshots')}
  {amber('\u251c\u2500\u2500\u2500')} pre-merge   {dim('runs before a merge is committed')}
  {amber('\u2514\u2500\u2500\u2500')} pre-publish  {dim('runs before a release is recorded')}

{amber('quick start')}
  {amber('\u251c\u2500\u2500\u2500')} sb test new pre-save smoke     {dim('scaffold sb-tests/pre-save/smoke.sh')}
  {amber('\u251c\u2500\u2500\u2500')} $EDITOR sb-tests/pre-save/smoke.sh
  {amber('\u251c\u2500\u2500\u2500')} sb test                        {dim('run every stage now')}
  {amber('\u2514\u2500\u2500\u2500')} sb save "<message>"            {dim('gates now run automatically')}

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

def cmd_publish(args):
    if args.label == "list":               # word form of -l, like 'sb test list'
        args.list, args.label = True, None
    repo = need_repo()
    if args.list:
        recs = [e for e in repo.journal_entries()
                if e["op"] in ("publish", "deploy")]
        if not recs:
            print(dim("no releases recorded")); return
        try:
            repo.verify_journal()
            chain = "journal chain ok " + amber("\u2713")
        except TamperedJournal as e:
            chain = red("JOURNAL TAMPERED: " + str(e))
        print(f"{len(recs)} release(s)")
        tree_print([f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(e['ts']))}  "
                    f"{amber(short(e['detail']['commit']))}  "
                    f"{bold(e['detail']['label'])}  on {e['detail']['branch']}  "
                    + dim(f"by {e['detail']['author']}") for e in recs])
        print(dim("record integrity: ") + chain)
        return
    sync_locks(repo, quiet=True)
    head = repo.head_commit()
    if not head:
        die("nothing to publish — no saves yet")
    ensure_clean(repo)
    print(bold("gate 1/2") + dim(" · full store verification"))
    if not _verify(repo, quiet=True):
        die("verification failed — refusing to publish from a damaged store\n"
            "       (run 'sb verify' for the full report)")
    leaf("store intact " + amber("\u2713"))
    c = parse_commit(repo, head)
    tree = read_tree(repo, c["tree"])
    # record which gate scripts ran, by content hash, so the release record
    # says exactly what was (or wasn't) checked
    gate_scripts = {rel: h for rel, (mode, h) in tree.items()
                    if rel.startswith(f"{TESTS_DIR}/pre-publish/")}
    print(bold("gate 2/2") + dim(" · pre-publish tests on the HEAD tree"))
    tests_passed = run_stage(repo, "pre-publish", tree, commit=head,
                             quiet_if_empty=False)
    if not tests_passed:
        if not args.no_verify:
            die("pre-publish tests failed — publish blocked (--no-verify to override)")
        print(yellow("tests failed but --no-verify given — proceeding"))
    name, email = author()
    record = {
        "commit": head, "branch": repo.current_branch(),
        "label": args.label or "release", "author": f"{name} <{email}>",
        "tests": {"scripts": gate_scripts, "passed": tests_passed}}
    if args.no_verify:
        record["skipped_tests"] = True
    link = repo.journal("publish", record)
    print(f"{bold('published')} {amber(short(head))} {dim('as')} "
          f"{bold(args.label or 'release')}")
    leaf(dim("journaled · anchor ") + amber(link[:16])
         + dim(f"  (list: sb publish -l · get files: "
               f"sb export {args.label or 'release'})"))

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
            entries = [(m, k, h, n) for m, k, h, n in entries]
        except CorruptObject as e:
            flag("object", str(e)); return
        except (ValueError, TypeError):
            flag("object", f"tree {short(th)} does not decode to tree "
                           f"entries"); return
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
            what = (f"missing object {short(e.args[0])}"
                    if isinstance(e, KeyError) else str(e))
            flag("object", f"branch {b}: {what}")

    # 1b. saves kept from removed branches: 'sb branch -r' keeps their
    #     history, so verify keeps checking it.
    reachable = len(seen_commits)
    for (ch,) in repo.db.execute(
            "SELECT hash FROM objects WHERE kind='commit'").fetchall():
        if ch in seen_commits:
            continue
        try:
            for c in walk_history(repo, ch):
                if c["hash"] in seen_commits:
                    continue
                seen_commits.add(c["hash"])
                objects += 1
                check_tree(c["tree"])
        except (CorruptObject, KeyError) as e:
            what = (f"missing object {short(e.args[0])}"
                    if isinstance(e, KeyError) else str(e))
            flag("object", f"removed-branch save {short(ch)}: {what}")
    unreachable = len(seen_commits) - reachable

    # 1c. remaining rows (orphaned blobs/trees left by an interrupted
    #     operation, or the in-progress content a lock is protecting):
    #     re-hash those too, so every stored object is checked.
    for h, kind in repo.db.execute("SELECT hash, kind FROM objects").fetchall():
        if h in seen_commits or h in seen_trees or h in seen_blobs:
            continue
        objects += 1
        if kind == "tree":
            check_tree(h)
            continue
        try:
            repo.get(h)
        except CorruptObject as e:
            flag("object", str(e))

    # 2. the journal hash chain, end to end
    chain_ok, head_link, n_entries = True, None, 0
    try:
        n_entries, head_link = repo.verify_journal()
    except TamperedJournal as e:
        chain_ok = False
        flag("journal", str(e))

    # 3. branch tips must match what the journal last recorded — a ref
    #    edited behind sb's back (e.g. direct SQL) is caught here. Iterating
    #    the journal can itself fail on a tampered row; that is a finding,
    #    not a crash.
    expected = {}
    try:
        for e in repo.journal_entries():
            if e["op"] in REF_OPS:
                d = e["detail"]
                expected[d["branch"]] = d["new"]
            elif e["op"] == "branch-remove":
                expected.pop(e["detail"]["branch"], None)
    except (TamperedJournal, KeyError, TypeError) as e:
        chain_ok = False
        flag("journal", f"journal is not readable for the ref check: {e}")
    for b in repo.branches():
        cur = repo.tip(b) or ""
        if b in expected and expected[b] != cur:
            flag("refs", f"branch '{b}' points at {short(cur)} but the "
                         f"journal last recorded {short(expected[b])} "
                         f"(moved outside sb?)")
        elif b not in expected and cur:
            # a non-empty ref the journal has never heard of was injected
            # outside sb (direct SQL); an empty ref is just a fresh branch
            flag("refs", f"branch '{b}' ({short(cur)}) exists in refs but "
                         f"was never recorded in the journal "
                         f"(added outside sb?)")
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
        try:
            links = {e["link"] for e in repo.journal_entries()}
        except TamperedJournal:
            links = set()
        links.add(repo.meta("repo_id"))
        if any(l.startswith(a) for l in links):
            anchor_ok = True
        else:
            flag("journal", f"anchor {a[:16]} is NOT in the journal "
                            f"chain (history was replaced wholesale?)")

    if not quiet:
        cats = {c for c, _ in problems}
        extra = (dim(f"  ({unreachable} save(s) kept from removed branches)")
                 if unreachable else "")
        print(f"checked {bold(str(objects))} {dim('objects across')} "
              f"{bold(str(len(seen_commits)))} {dim('save(s)')}" + extra)
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
                        + dim("  (save it · check later: sb verify -a <hash>)"))
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
        if e["op"] in REF_OPS:
            what = f"{d.get('branch','')}: {short(d.get('old',''))} → {short(d.get('new',''))}"
            audit = [t for f, t in (("skipped_tests", "no-verify"),
                                    ("skipped_secret_scan", "secrets-override"),
                                    ("global_force", "global-force"),
                                    ("secrets_redacted", "redacted"),
                                    ("secrets_present", "secrets-present"),
                                    ("initial", "initial-save"),
                                    ("seeded_from", "seeded"))
                     if d.get(f)]
            if audit:
                what += "  " + yellow("· " + " · ".join(audit))
        elif e["op"] == "init":
            what = f"repository created  {short(d.get('repo_id',''))}"
        elif e["op"] == "switch":
            what = f"to {d.get('to','')}"
        elif e["op"] == "branch-remove":
            what = f"{d.get('branch','')} (was {short(d.get('old',''))})"
        elif e["op"] in ("publish", "deploy"):
            what = f"{d.get('label','')} @ {short(d.get('commit',''))}"
        elif e["op"] in ("shared", "durability"):
            what = f"{d.get('old','?')} → {d.get('new','?')}"
        elif e["op"] == "unlock":
            paths = d.get("paths", [])
            what = (", ".join(paths[:3]) + (" …" if len(paths) > 3 else "")
                    + (yellow("  · forced") if d.get("forced") else ""))
        elif e["op"] == "lock-revert":
            paths = d.get("paths", [])
            kept = d.get("kept", {}) or {}
            what = (", ".join(paths[:3]) + (" …" if len(paths) > 3 else "")
                    + dim("  · put back to the holder's version")
                    + (dim("  · kept " + ", ".join(short(h) for h in
                       list(kept.values())[:3])) if kept else ""))
        elif e["op"] == "ignore":
            what = d.get("pattern", "")
        elif e["op"] == "identity":
            what = f"uid {d.get('uid','?')} → {d.get('name','?')} <{d.get('email','?')}>"
        elif e["op"] == "pack":
            what = f"{d.get('output','')} ({d.get('payload','')})"
        elif e["op"] == "export":
            what = f"{d.get('output','')} of {d.get('of','')}"
        elif e["op"] == "unpack":
            what = (f"from {d.get('source','')}"
                    + ("  · merged into existing" if d.get("merged") else ""))
        else:
            what = " ".join(f"{k}={v}" for k, v in sorted(d.items()))[:60]
        print(f"{dim('#%-4d' % e['seq'])} {when}  {bold('%-10s' % e['op'])} "
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
        f"version  sandbox {VERSION}  " + dim(f"· {AUTHOR}"),
        f"store    {repo.vdir / DB_NAME}  "
        + dim(f"({db_size:,} bytes on disk · {raw:,} bytes of content)"),
        f"branch   {bold(repo.current_branch())}  "
        + dim(f"of {len(repo.branches())}"),
        f"objects  {counts.get('commit',0)} save(s) · "
        f"{counts.get('tree',0)} tree(s) · {counts.get('blob',0)} blob(s)",
        f"journal  {n_journal} entries · anchor "
        + amber(repo.chain_head()[:16]),
        f"locks    {len(repo.locks())} active  "
        + dim("(sb locks)"),
        f"you      {name} <{email}>  "
        + dim("(attribution only — no keys, no signatures)"),
    ])

def cmd_durability(args):
    repo = need_repo()
    if args.value is None:
        cur = repo.meta("durability") or "full"
        print(f"durability {bold(cur)}")
        leaf(dim("full = newest commit survives power loss (default) · "
                 "normal = faster, may lose the last commit on power loss"))
        return
    val = args.value.lower()
    if val not in ("full", "normal"):
        die("durability must be 'full' or 'normal'\n"
            "       usage: sb durability [full | normal]")
    with repo.transaction():            # setting + journal: one transaction
        old_val = repo.meta("durability") or "full"
        repo.set_meta("durability", val)
        repo.journal("durability", {"old": old_val, "new": val})
    repo.db.execute(
        f"PRAGMA synchronous={'FULL' if val == 'full' else 'NORMAL'}")
    print(f"durability set to {bold(val)}")

def cmd_locks(args):
    repo = need_repo()
    sync_locks(repo, quiet=True)
    locks = repo.locks()
    if not locks:
        print(dim("no active locks")); return
    name, email = author()
    print(f"{len(locks)} active lock(s)")
    rows = []
    for p in sorted(locks):
        l = locks[p]
        mine = l["email"] == email
        left = max(0, (l["since"] + LOCK_TTL - int(time.time())) // 60)
        note = (("you" if mine else l["owner"]) + " · " + _ago(l["since"])
                + f" · expires in {left}m")
        if l["held"] == LOCK_DELETED:
            note += " · file removed"
        elif l["held"]:
            note += " · holding " + short(l["held"])
        rows.append(f"{cyan(p)}  " + dim(note))
    tree_print(rows)
    leaf(dim("a locked file is its holder's until they save: other people's "
             "edits to it are put back\n       release yours: sb unlock "
             "<path>  ·  someone else's: --force"))

def cmd_unlock(args):
    repo = need_repo()
    sync_locks(repo, quiet=True)
    name, email = author()
    force = getattr(args, "force", False)
    locks = repo.locks()
    targets = list(args.paths) if args.paths else \
        [p for p, l in locks.items() if force or l["email"] == email]
    if not targets:
        print(dim("nothing to unlock" + ("" if force else
                  " (you hold no locks; --force to release others')")))
        return
    freed, denied, missing = [], [], []
    for p in targets:
        l = locks.get(p)
        if not l:
            missing.append(p)
        elif l["email"] == email or force:
            freed.append(p)
        else:
            denied.append(p)
    if freed:
        with repo.transaction():        # release + journal: one transaction
            owners = sorted({locks[p]["owner"] for p in freed})
            repo.clear_locks(freed)
            repo.journal("unlock", {"paths": sorted(freed), "forced": force,
                                    "owners": owners, "by": name})
    if freed:
        print(f"unlocked {len(freed)} file(s)" + (dim(" (forced)") if force else ""))
        tree_print([cyan(p) for p in sorted(freed)])
        leaf(dim("their content stays on disk — whoever edits next takes "
                 "the lock"))
    if missing:
        leaf(dim(f"{len(missing)} not locked: " + ", ".join(sorted(missing)[:4])))
    if denied:
        die(f"{len(denied)} held by others — add --force to release: "
            + ", ".join(sorted(denied)[:4]))

def cmd_salvage(args):
    """Write any stored content back out to a file. Its main use is the
    other side of a lock revert: when your edit to someone else's locked
    file is put back, the bytes you wrote are stored and their hash printed
    (and journaled), so nothing you typed is ever actually destroyed."""
    repo = need_repo()
    h = (args.hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{4,64}", h):
        die("a content hash is 4-64 hex characters (sb prints 10)\n"
            "       find one in 'sb journal' next to a lock-revert entry")
    rows = repo.db.execute(
        "SELECT hash FROM objects WHERE kind='blob' AND hash LIKE ? LIMIT 3",
        (h + "%",)).fetchall()
    if not rows:
        die(f"no stored content starts with '{h}'")
    if len(rows) > 1:
        die(f"'{h}' matches {len(rows)} stored objects — give more characters")
    full = rows[0][0]
    data = repo.get(full)[1]                    # re-hashed on read
    dest = Path(args.dest) if args.dest else Path(f"salvaged-{short(full)}")
    if dest.exists():
        die(f"{dest} already exists — choose another name")
    if dest.parent and not dest.parent.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"{bold('salvaged')} {amber(short(full))} {dim('→')} "
          f"{cyan(str(dest))} " + dim(f"({len(data):,} bytes)"))

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
    repo.journal("ignore", {"pattern": args.pattern})
    print(f"ignoring {cyan(args.pattern)}")
    leaf(dim(".sbignore updated"))

def cmd_selftest(args):
    """Adversarial self-test. Exercises the failure classes a source-history
    tool must survive: crash injection at the ref/journal boundary, symlink
    escape and file/directory path safety, save consistency under mid-gate
    mutation, merge byte-fidelity, compare-and-swap under concurrent saves,
    full-store verification, archive salt uniqueness, branch bootstrapping,
    and the shared content-lock model.
    Exits 0 if everything passes, non-zero otherwise."""
    import shutil as _sh, threading as _th, io as _io, tarfile as _tar
    import sqlite3 as _sql, zlib as _zl
    SELF = str(Path(__file__).resolve())
    passed, failed = [], []

    def check(name, cond):
        (passed if cond else failed).append(name)
        print(("  " + amber("\u2514\u2500\u2500\u2500 ") if False else "  ")
              + (green("ok  ") if cond else red("FAIL")) + "  " + name)

    def run(*a, cwd=None, expect=None):
        r = subprocess.run([sys.executable, SELF, *a], cwd=cwd,
                           capture_output=True, text=True)
        if expect is not None and r.returncode != expect:
            raise AssertionError(f"{a} -> {r.returncode}\n{r.stdout}{r.stderr}")
        return r

    def fresh():
        d = Path(tempfile.mkdtemp(prefix="sbtest-"))
        return d, os.getcwd()

    cases = []
    def case(fn):
        cases.append(fn); return fn

    @case
    def atomic_ref_journal():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("f").write_text("1"); run("save", "one")
            repo = Repo(Path(".").resolve())
            tip0 = repo.tip("main")
            n0 = repo.db.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
            orig = Repo.journal
            def boom(self, op, detail): raise RuntimeError("crash")
            Repo.journal = boom
            try: repo.update_ref("main", "ab" * 32, op="save")
            except RuntimeError: pass
            finally: Repo.journal = orig
            ok = (repo.tip("main") == tip0
                  and repo.db.execute("SELECT COUNT(*) FROM journal")
                          .fetchone()[0] == n0)
            check("atomic: ref rolls back with failed journal", ok)
            check("atomic: verify clean after rollback",
                  run("verify").returncode == 0)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def symlink_escape():
        d, old = fresh(); os.chdir(d)
        outside = Path(tempfile.mkdtemp(prefix="outside-"))
        try:
            run("init"); Path("seed").write_text("s"); run("save", "s")
            os.mkdir("realdir"); Path("realdir/victim").write_text("v")
            run("save", "add victim")
            repo = Repo(Path(".").resolve())
            tree, _ = head_tree_files(repo)
            _sh.rmtree("realdir"); os.symlink(str(outside), "realdir")
            try: checkout_tree(repo, tree, {})
            except CheckoutConflict: pass
            check("path: symlinked parent cannot redirect checkout",
                  not (outside / "victim").exists())
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)
            _sh.rmtree(outside, ignore_errors=True)

    @case
    def file_dir_transition():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); os.mkdir("a"); Path("a/b").write_text("in")
            run("save", "dir")
            run("branch", "other"); run("switch", "other")
            _sh.rmtree("a"); Path("a").write_text("file"); run("save", "file")
            r1 = run("switch", "main"); r2 = run("switch", "other")
            check("path: file<->dir transition works",
                  r1.returncode == 0 and r2.returncode == 0)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def untar_symlink_safe():
        d, old = fresh(); os.chdir(d)
        outside = Path(tempfile.mkdtemp(prefix="outside-"))
        try:
            dest = Path("dest"); dest.mkdir()
            os.symlink(str(outside), str(dest / "sub"))
            buf = _io.BytesIO()
            with _tar.open(fileobj=buf, mode="w") as t:
                info = _tar.TarInfo("sub/evil"); data = b"pwned"
                info.size = len(data); t.addfile(info, _io.BytesIO(data))
            blocked = False
            try: _untar_files(buf.getvalue(), dest)
            except (CheckoutConflict, SystemExit): blocked = True
            check("path: untar refuses symlinked parent",
                  blocked and not (outside / "evil").exists())
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)
            _sh.rmtree(outside, ignore_errors=True)

    @case
    def save_consistency():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("app.py").write_text("x=1"); run("save", "first")
            run("test", "new", "pre-save", "m.py")
            Path("sb-tests/pre-save/m.py").write_text(
                'import os,sys\n'
                'open(os.path.join(os.environ["SB_REPO"],"app.py"),"w")'
                '.write("M\\n")\nsys.exit(0)\n')
            Path("app.py").write_text("x=2")
            r = run("save", "block")
            check("consistency: mid-gate mutation blocks save",
                  "changed while" in (r.stdout + r.stderr)
                  and "first" in run("log", "-n", "1").stdout)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def branch_starts_saved():
        d, old = fresh(); os.chdir(d)
        try:
            run("init")
            Path("a.txt").write_text("hello\n")      # never saved by hand
            r = run("branch", "feat")
            seeded = "Initial branch creation" in run("log").stdout
            run("switch", "feat", expect=0)          # no save needed first
            clean = "clean" in run("status").stdout
            Path("b.txt").write_text("new\n")
            run("save", "work on feat")
            run("switch", "main", expect=0)
            m = run("merge", "feat")
            check("branch: a new branch saves the folder immediately",
                  r.returncode == 0 and seeded and clean)
            check("branch: mergeable at once, with no manual first save",
                  m.returncode == 0 and Path("b.txt").exists()
                  and Path("a.txt").read_text() == "hello\n")
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def branch_captures_unsaved_work():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("a").write_text("1"); run("save", "a")
            Path("b").write_text("2")                # unsaved when branching
            run("branch", "feat", expect=0)
            run("switch", "feat", expect=0)
            check("branch: unsaved work goes into the initial save",
                  "clean" in run("status").stdout and Path("b").exists())
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def merge_keeps_our_only_file():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("seed").write_text("s"); run("save", "s")
            run("branch", "feat")
            Path("mine.txt").write_text("mine\n"); run("save", "only on main")
            r = run("merge", "feat")
            check("merge: a file only our side has is kept, not deleted",
                  r.returncode == 0 and Path("mine.txt").exists())
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def merge_crlf_conflicts():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("seed").write_text("s"); run("save", "s")
            Path("w.txt").write_bytes(b"a\r\nb\r\nc\r\n"); run("save", "base")
            run("branch", "x"); run("switch", "x")
            Path("w.txt").write_bytes(b"a\r\nB\r\nc\r\n"); run("save", "x")
            run("switch", "main")
            Path("w.txt").write_bytes(b"A\r\nb\r\nc\r\n"); run("save", "m")
            check("merge: CRLF conflicts instead of silent rewrite",
                  run("merge", "x").returncode == 2)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def merge_addadd():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("seed").write_text("s"); run("save", "s")
            run("branch", "b1"); run("switch", "b1")
            Path("f").write_text(""); run("save", "empty")
            run("switch", "main"); run("branch", "b2"); run("switch", "b2")
            Path("f").write_text("hi\n"); run("save", "nonempty")
            run("switch", "main"); run("merge", "b1")
            r = run("merge", "b2")
            check("merge: add/add does not crash",
                  "Traceback" not in (r.stdout + r.stderr))
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def cas_lost_update():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("f").write_text("1"); run("save", "one")
            repo = Repo(Path(".").resolve())
            Path("f").write_text("2"); run("save", "two")
            aborted = False
            try:
                repo.update_ref("main", "cd" * 32, op="save", expect="00" * 32)
            except SystemExit:
                aborted = True
            check("concurrency: stale ref update aborts (CAS)", aborted)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def concurrent_saves():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("f0").write_text("0"); run("save", "base")
            def w(i):
                Path(f"f{i}").write_text(str(i)); run("save", f"s{i}")
            ts = [_th.Thread(target=w, args=(i,)) for i in range(1, 6)]
            for t in ts: t.start()
            for t in ts: t.join()
            check("concurrency: parallel saves keep store valid",
                  run("verify").returncode == 0)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def verify_full_store():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("seed").write_text("s"); run("save", "s")
            run("branch", "tmp"); run("switch", "tmp")
            Path("only.txt").write_text("tmp-only\n"); run("save", "on tmp")
            run("switch", "main"); run("branch", "tmp", "-r")
            target = hash_obj("blob", b"tmp-only\n")
            db = _sql.connect(".sb/sandbox.db")
            db.execute("UPDATE objects SET data=? WHERE hash=?",
                       (_zl.compress(b"CORRUPT"), target))
            db.commit(); db.close()
            r = run("verify")
            check("verify: corruption in removed-branch history is caught",
                  r.returncode == 2 and ("CORRUPTION" in r.stdout
                                         or "does not match" in r.stdout))
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def archive_salt_unique():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("f").write_text("secret"); run("save", "s")
            run("pack", "a.sbox", "-k", "pw"); run("pack", "b.sbox", "-k", "pw")
            a = Path("a.sbox").read_bytes(); b = Path("b.sbox").read_bytes()
            check("crypto: per-archive salt differs",
                  a[4] >= 2 and a[5:21] != b[5:21] and a[21:80] != b[21:80])
            run("unpack", "a.sbox", "out", "-k", "pw", expect=0)
            check("crypto: salted archive round-trips",
                  Path("out/f").read_text() == "secret")
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def restored_mtime():
        if sys.platform.startswith("win"):
            check("statcache: restored-mtime edit (skipped on win)", True); return
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); p = Path("s.txt"); p.write_text("aaaa\n")
            past = time.time() - 864000; os.utime(p, (past, past))
            run("save", "base"); run("status")
            p.write_text("bbbb\n"); os.utime(p, (past, past))
            r = run("status")
            check("statcache: restored-mtime edit detected",
                  "modified" in r.stdout and "s.txt" in r.stdout)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def secret_redaction():
        d, old = fresh(); os.chdir(d)
        xd = Path(tempfile.mkdtemp(prefix="sbexp-"))
        try:
            run("init"); Path("seed").write_text("s"); run("save", "seed")
            fake = "AKIA" + "A" * 16
            Path("cfg.py").write_text(f'key = "{fake}"\nx = 1\n')
            r = run("save", "with secret")
            saved = r.returncode == 0
            run("export", "main", str(xd / "chk"))
            exported = (xd / "chk/cfg.py").read_text()
            check("secrets: save proceeds with the secret redacted",
                  saved and "<REDACTED>" in exported and fake not in exported
                  and "x = 1" in exported)
            check("secrets: the working file is never rewritten",
                  fake in Path("cfg.py").read_text())
            r2 = run("save", "again")
            check("secrets: an unchanged redacted file doesn't loop",
                  "nothing" in (r2.stdout + r2.stderr))
            # a private key block is redacted in full, not just its header
            body = ("-----BEGIN RSA PRIVATE KEY-----\n"
                    "MIIEowIBAAKCAQEAsecretsecretsecret\n"
                    "-----END RSA PRIVATE KEY-----\n")
            Path("id_rsa").write_text(body)
            run("save", "key")
            run("export", "main", str(xd / "chk2"))
            exp = (xd / "chk2/id_rsa").read_text()
            check("secrets: private key blocks are redacted whole",
                  "MIIEow" not in exp and "<REDACTED>" in exp)
            # --allow-secrets stores the bytes verbatim, journaled
            Path("cfg2.py").write_text(f'k2 = "{fake}"\n')
            run("save", "verbatim", "--allow-secrets")
            run("export", "main", str(xd / "chk3"))
            check("secrets: --allow-secrets saves verbatim",
                  fake in (xd / "chk3/cfg2.py").read_text())
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)
            _sh.rmtree(xd, ignore_errors=True)

    @case
    def lock_content_wins():
        """The 1.4 rule: a locked file IS its holder's version. Anyone may
        type into it; on the next command their bytes are put back and kept
        in the store, and only the holder can commit it."""
        d, old = fresh(); os.chdir(d)
        xd = Path(tempfile.mkdtemp(prefix="sbexp-"))
        env0 = dict(os.environ)
        def as_user(n, e, *a, **k):
            os.environ["SB_NAME"] = n; os.environ["SB_EMAIL"] = e
            return run(*a, **k)
        try:
            as_user("Lead", "l@co", "init")
            Path("f.txt").write_text("v1\n")
            as_user("Lead", "l@co", "save", "seed")
            Path("f.txt").write_text("alice\n")
            as_user("Alice", "a@co", "status")          # alice takes the lock
            Path("f.txt").write_text("bob\n")           # bob types over it
            r = as_user("Bob", "b@co", "status")
            check("locks: a foreign edit is put back to the holder's version",
                  Path("f.txt").read_text() == "alice\n")
            m = re.search(r"kept as ([0-9a-f]{6,})", r.stdout)
            check("locks: the overwritten bytes are stored, not destroyed",
                  m is not None)
            if m:
                as_user("Bob", "b@co", "salvage", m.group(1),
                        str(xd / "bobs.txt"), expect=0)
                check("locks: sb salvage brings the rejected version back",
                      (xd / "bobs.txt").read_text() == "bob\n")
            as_user("Bob", "b@co", "save", "try to take it")
            as_user("Lead", "l@co", "export", "main", str(xd / "chk1"))
            check("locks: nobody but the holder can save a locked file",
                  (xd / "chk1/f.txt").read_text() == "v1\n")
            Path("f.txt").write_text("alice2\n")        # holder edits again
            as_user("Alice", "a@co", "status")
            Path("f.txt").write_text("bob2\n")
            as_user("Bob", "b@co", "status")
            check("locks: the protected version follows the holder's edits",
                  Path("f.txt").read_text() == "alice2\n")
            as_user("Alice", "a@co", "save", "alice's work")
            as_user("Lead", "l@co", "export", "main", str(xd / "chk2"))
            check("locks: the holder's save lands and frees the file",
                  (xd / "chk2/f.txt").read_text() == "alice2\n"
                  and not Repo(Path(".").resolve()).locks())
            # putting a file back the way it was saved retires its lock
            Path("f.txt").write_text("carol\n")
            as_user("Carol", "c@co", "status")
            Path("f.txt").write_text("alice2\n")
            as_user("Carol", "c@co", "status")
            check("locks: a lock retires when nothing is left to protect",
                  not Repo(Path(".").resolve()).locks())
        finally:
            os.environ.clear(); os.environ.update(env0)
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)
            _sh.rmtree(xd, ignore_errors=True)

    @case
    def shared_locks():
        d, old = fresh(); os.chdir(d)
        xd = Path(tempfile.mkdtemp(prefix="sbexp-"))
        env0 = dict(os.environ)
        def as_user(n, e, *a, **k):
            os.environ["SB_NAME"] = n; os.environ["SB_EMAIL"] = e
            return run(*a, **k)
        try:
            as_user("Lead", "l@co", "init")
            Path("a.py").write_text("v1"); Path("b.py").write_text("v1")
            as_user("Lead", "l@co", "save", "seed")   # shared is always on
            # alice edits a.py, bob edits b.py — independent locks
            Path("a.py").write_text("alice"); as_user("Alice", "a@co", "status")
            Path("b.py").write_text("bob"); as_user("Bob", "b@co", "status")
            repo = Repo(Path(".").resolve())
            locks = repo.locks()
            check("shared: independent edits lock to their own users",
                  locks.get("a.py", {}).get("email") == "a@co"
                  and locks.get("b.py", {}).get("email") == "b@co")
            # bob saves — only b.py committed, a.py stays at v1 in history
            as_user("Bob", "b@co", "save", "bob edit")
            as_user("Lead", "l@co", "export", "main", str(xd / "chk1"))
            check("shared: save commits only your files",
                  (xd / "chk1/a.py").read_text() == "v1"
                  and (xd / "chk1/b.py").read_text() == "bob")
            check("shared: others' locks survive your save",
                  "a.py" in Repo(Path(".").resolve()).locks())
            # expiry auto-saves the abandoned edit (attributed to alice),
            # then auto-reverts it — history AND disk return to v1, and the
            # saved edit is recoverable via sb restore <auto-save hash>
            os.environ["SB_LOCK_TTL"] = "1"; time.sleep(2)
            r = as_user("Lead", "l@co", "status")
            as_user("Lead", "l@co", "export", "main", str(xd / "chk2"))
            lg = as_user("Lead", "l@co", "log").stdout
            check("shared: expired lock auto-saves then auto-reverts",
                  (xd / "chk2/a.py").read_text() == "v1"
                  and Path("a.py").read_text() == "v1"
                  and "auto-save" in lg and "auto-revert" in lg)
            m_h = re.search(r"restore ([0-9a-f]{4,})", lg)
            rr = as_user("Lead", "l@co", "restore", m_h.group(1)) if m_h else None
            check("shared: the auto-saved edits are recoverable via restore",
                  rr is not None and rr.returncode == 0
                  and Path("a.py").read_text() == "alice")
            if rr is not None:              # put the tree back for what follows
                as_user("Lead", "l@co", "undo")
            del os.environ["SB_LOCK_TTL"]
            # merge --ignore skips a locked file, leaving it and its lock alone
            Path("c.py").write_text("base"); as_user("Lead", "l@co", "save", "c base")
            as_user("Lead", "l@co", "branch", "feat")
            as_user("Lead", "l@co", "switch", "feat")
            Path("c.py").write_text("feature"); as_user("Lead", "l@co", "save", "c feat")
            as_user("Lead", "l@co", "switch", "main")
            Path("c.py").write_text("carol edit"); as_user("Carol", "c@co", "status")
            r = as_user("Lead", "l@co", "merge", "feat")
            blocked = r.returncode != 0 and "locked" in (r.stdout + r.stderr).lower()
            # --ignore: merge proceeds, c.py kept at our version, lock survives
            r2 = as_user("Lead", "l@co", "merge", "feat", "--ignore")
            repo2 = Repo(Path(".").resolve())
            as_user("Lead", "l@co", "export", "main", str(xd / "chk3"))
            skipped_ok = (r2.returncode == 0
                          and (xd / "chk3/c.py").read_text() != "feature"
                          and "c.py" in repo2.locks())
            check("shared: merge blocked by lock; --ignore skips it, lock kept",
                  blocked and skipped_ok)
            # unlock releases a held lock (Carol's, via --force)
            r3 = as_user("Lead", "l@co", "unlock", "c.py", "--force")
            check("shared: sb unlock --force releases others' lock",
                  r3.returncode == 0
                  and "c.py" not in Repo(Path(".").resolve()).locks())
        finally:
            os.environ.clear(); os.environ.update(env0)
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)
            _sh.rmtree(xd, ignore_errors=True)

    print(bold("sb selftest") + dim(f" · {len(cases)} cases"))
    for fn in cases:
        try:
            fn()
        except Exception as e:
            failed.append(fn.__name__)
            print("  " + red("FAIL") + f"  {fn.__name__}: "
                  f"{type(e).__name__}: {e}")
    print()
    if failed:
        leaf(red(f"{len(passed)} passed, {len(failed)} failed: "
                 + ", ".join(failed)))
        sys.exit(1)
    leaf(green(f"all {len(passed)} checks passed"))

# ------------------------------------------------------- portable archive ---
# 'sb pack' seals the entire repository (the single sandbox.db) plus a small
# manifest into one encrypted .sbox file; 'sb unpack' reverses it. Encryption
# is provided by vox (embedded below), so both commands work fully offline.
SBOX_MAGIC = b"SBOX"
SBOX_VERSION = 2      # v2 adds a random 16-byte per-archive salt (see below)
SBOX_SALT_LEN = 16

def _sbox_seal(vox, manifest, body, passphrase):
    """Encrypt an archive with a fresh random salt mixed into the key, so two
    archives sealed with the same passphrase derive different keys and an
    attacker cannot amortize a password guess across archives. The salt lives
    in the (authenticated) cleartext header."""
    salt = os.urandom(SBOX_SALT_LEN)
    header = SBOX_MAGIC + bytes([SBOX_VERSION]) + salt
    eff_key = salt.hex() + ":" + passphrase
    blob = vox.encrypt(_frame(manifest, body), eff_key, associated_data=header)
    return header + blob

def _sbox_open(vox, raw, passphrase):
    """Inverse of _sbox_seal. Handles v2 (salted) and v1 (legacy, fixed-salt)
    archives so older .sbox files still open."""
    ver = raw[4]
    if ver >= 2:
        salt = raw[5:5 + SBOX_SALT_LEN]
        header = raw[:5 + SBOX_SALT_LEN]
        blob = raw[5 + SBOX_SALT_LEN:]
        eff_key = salt.hex() + ":" + passphrase
        return vox.decrypt(blob, eff_key, associated_data=header)
    header, blob = raw[:5], raw[5:]          # v1: passphrase used directly
    return vox.decrypt(blob, passphrase, associated_data=header)

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

SALT_LEN        = 64        # synthetic nonce length (SIV)
TAG_LEN         = 64        # AEAD authentication tag length
KDF_ITERS       = 300_000   # PBKDF2 work factor
KDF_KEY_LEN     = 64        # master key length

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
    associated_data: bytes = b""
) -> bytes:
    # encrypts plaintext using AEAD
    # associated_data is authenticated but not encrypted

    ctx = _get_context(passkey.encode())
    return _aead_encrypt(ctx, plaintext, associated_data)


def decrypt(
    ciphertext: bytes,
    passkey: str,
    *,
    associated_data: bytes = b""
) -> bytes:
    # verifies authenticity before decryption

    ctx = _get_context(passkey.encode())
    return _aead_decrypt(ctx, ciphertext, associated_data)

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

def _write_archive(out: Path, data: bytes):
    """Write a sealed archive next to its final name via a randomized,
    exclusively-created temp file (O_EXCL + no-follow), then atomically
    rename it into place. A predictable temp name in a shared directory
    could be pre-planted as a symlink; this cannot be."""
    tmp = out.with_name(f".{out.name}.{os.urandom(6).hex()}.sbtmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                 0o600)
    try:
        view = memoryview(data)
        off = 0
        while off < len(view):
            off += os.write(fd, view[off:])
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, out)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise

def _unframe(payload: bytes):
    if len(payload) < 4:
        die("archive payload is truncated — not a complete sandbox archive")
    n = int.from_bytes(payload[:4], "big")
    if not 0 < n <= len(payload) - 4:
        die("archive payload is malformed (manifest length out of range)")
    try:
        manifest = json.loads(payload[4:4 + n])
    except ValueError:
        die("archive manifest is not valid JSON — the file was altered")
    if not isinstance(manifest, dict):
        die("archive manifest is malformed — the file was altered")
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
    """Extract an archive's files into dest. Names are validated lexically,
    then every write goes through a no-follow, descriptor-relative path so a
    pre-existing symlinked parent in the destination can never redirect a
    write outside dest (the same protection checkout uses)."""
    import tarfile, io
    dest.mkdir(parents=True, exist_ok=True)
    root_fd = os.open(str(dest), os.O_RDONLY | _O_DIRECTORY)
    n = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as t:
            for m in t.getmembers():
                if not m.isreg():
                    continue
                parts = Path(m.name).parts
                if (m.name.startswith("/")
                        or any(p in ("..", SB_DIR) for p in parts)
                        or not all(safe_name(p) for p in parts)):
                    die(f"archive contains an unsafe path: {m.name!r} — "
                        f"refusing")
                rel = "/".join(parts)
                try:
                    pfd, leaf = _safe_parent_fd(root_fd, rel)
                except CheckoutConflict as e:
                    die(f"refusing to extract {rel!r}: {e}")
                try:
                    payload = t.extractfile(m).read()
                    _remove_at(pfd, leaf)
                    _write_file_at(pfd, leaf, payload, (m.mode & 0o777) or 0o644)
                finally:
                    os.close(pfd)
                n += 1
    finally:
        os.close(root_fd)
    return n

def cmd_pack(args):
    repo = need_repo()
    # usage: sb pack [<output>] -k <passkey> [-f]
    if len(args.params) > 1:
        die("too many arguments — usage: sb pack [<output>] -k <passkey>")
    key = _get_key(args, confirm=True)
    out_name = args.params[0] if args.params else None
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
    _write_archive(out, _sbox_seal(vox, manifest, body, key))
    with contextlib.suppress(sqlite3.Error, OSError):
        repo.journal("pack", {"output": out.name, "payload": payload_kind,
                              "sha256": manifest["db_sha256"]})
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
            + out.name + " -k <passkey>"),
    ])

def cmd_unpack(args):
    # usage: sb unpack <file.sbox> [<destination>] -k <passkey> [-f] [-i]
    usage = "usage:  sb unpack <file.sbox> [<destination>] -k <passkey> [-f] [-i]"
    if not args.params:
        die(usage)
    if len(args.params) > 2:
        die("too many arguments — " + usage)
    key = _get_key(args)
    path_name = args.params[0]
    dest_name = args.params[1] if len(args.params) > 1 else None
    src = Path(path_name)
    if not src.is_file():
        die(f"no such file: {src}")
    raw = src.read_bytes()
    if len(raw) < 5 or raw[:4] != SBOX_MAGIC:
        die(f"{src} is not a sandbox archive (bad magic)")
    ver = raw[4]
    if ver > SBOX_VERSION:
        die(f"archive format {ver} is newer than this sb understands "
            f"({SBOX_VERSION}) — upgrade sb")
    vox = load_vox()
    try:
        payload = _sbox_open(vox, raw, key)
    except Exception:
        die("could not open the archive — wrong pass-key or the file was "
            "altered\n       (vox verifies authenticity before decrypting)")
    manifest, body = _unframe(payload)
    if sha256_hex(body) != manifest.get("db_sha256"):
        die("archive integrity check failed — the contents did not match "
            "their recorded hash")
    kind = manifest.get("payload", "repo")
    dest = Path(dest_name) if dest_name else Path(manifest.get("repo_name", "sandbox"))
    if dest.exists() and not dest.is_dir():
        die(f"{dest} exists and is not a folder — choose another destination")
    # any non-empty destination counts as merging; a plain folder of
    # files gets the same protection as an existing repository
    merging = dest.is_dir() and any(dest.iterdir())
    if merging and not args.ignore:
        what = (f"{dest / SB_DIR} (an sb repository)"
                if (dest / SB_DIR).exists() else f"{dest} is not empty")
        die(f"{what} — unpack into a fresh folder,\n"
            "       or merge into this one deliberately with -i / --ignore\n"
            "       (matching files are overwritten; everything else is kept)")
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
        # full repository: stage the store in a private temp area and verify
        # it END TO END before a single byte lands in the destination — a
        # damaged or hostile archive must not leave a half-installed repo
        stage = Path(tempfile.mkdtemp(prefix="sb-unpack-"))
        try:
            (stage / SB_DIR).mkdir()
            (stage / SB_DIR / DB_NAME).write_bytes(body)
            try:
                srepo = Repo(stage.resolve())
                ok = _verify(srepo, quiet=True)
                tree, _ = head_tree_files(srepo)
                srepo.db.close()
            except (sqlite3.Error, CorruptObject, TamperedJournal) as e:
                die("the archive's repository failed verification — nothing "
                    f"was written to {dest}\n       ({e})")
            if not ok:
                die("the archive's repository failed verification — nothing "
                    f"was written to {dest}\n       (its store, journal, or "
                    "refs are damaged or were tampered with)")
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        shutil.rmtree(stage, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        sbdir = dest / SB_DIR
        if sbdir.is_symlink():
            die(f"{sbdir} is a symlink — refusing to install the store "
                "through it")
        sbdir.mkdir(exist_ok=True)
        db_path = sbdir / DB_NAME
        if db_path.is_symlink():
            die(f"{db_path} is a symlink — refusing to install the store "
                "through it")
        if merging:
            # replacing an existing store: drop any stale WAL/SHM sidecars so
            # SQLite cannot pair old write-ahead pages with the new database
            for side in ("-wal", "-shm"):
                stale = db_path.with_name(DB_NAME + side)
                if stale.exists():
                    stale.unlink()
        fd = os.open(str(db_path),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW,
                     0o600)
        try:
            view = memoryview(body)
            off = 0
            while off < len(view):
                off += os.write(fd, view[off:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass
        repo = Repo(dest.resolve())
        with contextlib.suppress(sqlite3.Error, OSError):
            repo.journal("unpack", {"source": src.name,
                                    "sealed_by": who.get("name", "?"),
                                    "merged": bool(merging)})
        tree, _ = head_tree_files(repo)
        checkout_tree(repo, tree, {})
        n = len(tree)
        held = None

    print(f"{bold('unpacked')} {amber(str(dest))} {dim('·')} {dim(str(n) + ' file(s)')}")
    rows = [f"sealed by  {who.get('name','?')} <{who.get('email','?')}>  {dim('· ' + when)}",
            f"branch     {manifest.get('branch','main')} "
            + dim("· anchor ") + amber(manifest.get("chain_head","")[:16])]
    if merging:
        rows.append(dim("merged into the existing folder — matching files "
                        "overwritten, others untouched"))
    if held:
        rows.append(dim(held))
    else:
        rows.append("verified before install " + amber("\u2713")
                    + dim(" — store, journal and refs all agree"))
    tree_print(rows)

def _resolve_version(repo, what):
    """Resolve a release label, branch name, or save-hash prefix to a commit.
    Returns (commit_hash, human description of how it matched)."""
    recs = [e for e in repo.journal_entries()
            if e["op"] in ("publish", "deploy")
            and e["detail"].get("label") == what]
    if recs:
        d = recs[-1]["detail"]
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(recs[-1]["ts"]))
        return d["commit"], f"release '{what}' ({when})"
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
    die(f"nothing named '{what}' — not a release label, branch, or save hash\n"
        f"       (see labels: sb publish -l · see saves: sb log)")

def cmd_export(args):
    repo = need_repo()
    # usage: sb export <version> [<destination>] [-k <passkey>]
    usage = "usage:  sb export <version> [<destination>] [-k <passkey>]"
    if not args.params:
        die(usage)
    if len(args.params) > 2:
        die("too many arguments — " + usage)
    what = args.params[0]
    dest_name = args.params[1] if len(args.params) > 1 else None
    commit_hash, how = _resolve_version(repo, what)
    c = parse_commit(repo, commit_hash)
    tree = read_tree(repo, c["tree"])
    if not tree:
        die(f"{how} contains no files")
    name, email = author()

    if args.key is not None:                    # encrypted .sbox export
        key = args.key or _get_key(args, confirm=True)
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
        _write_archive(out, _sbox_seal(vox, manifest, body, key))
        with contextlib.suppress(sqlite3.Error, OSError):
            repo.journal("export", {"output": out.name, "of": what,
                                    "commit": commit_hash,
                                    "sha256": manifest["db_sha256"]})
        print(f"{bold('exported')} {amber(out.name)} {dim('·')} "
              f"{dim(f'{out.stat().st_size:,} bytes')}")
        tree_print([
            f"version  {how} {dim('·')} {amber(short(commit_hash))}",
            f"holds    files only · {len(tree)} file(s), no history",
            dim(f"ship it: sb unpack {out.name} /path/to/production "
                f"-k <passkey>"),
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

def _get_key(args, *, confirm=False):
    """Resolve the archive pass-key. Precedence: -k, then SB_PASSKEY, then an
    interactive getpass prompt. Passing -k on the command line is convenient
    but exposes the key to shell history and process listings; the prompt and
    the env var avoid that."""
    if args.key:
        return args.key
    env = os.environ.get("SB_PASSKEY")
    if env:
        return env
    if not sys.stdin.isatty():
        die("a pass-key is required: pass -k <passkey>, set SB_PASSKEY, or "
            "run interactively to be prompted")
    key = getpass.getpass("pass-key: ")
    if not key:
        die("no pass-key entered")
    if confirm:
        if getpass.getpass("confirm pass-key: ") != key:
            die("pass-keys did not match")
    return key

def _share_parser(cmd):
    """Uniform parser for pack / unpack / export. Options may appear
    anywhere on the line (parse_intermixed_args), same as every other
    sb command; there are no legacy positional-key forms."""
    sp = SBParser(prog=f"sb {cmd}", add_help=False)
    sp.add_argument("params", nargs="*")
    sp.add_argument("-k", "--key", metavar="<passkey>")
    if cmd in ("pack", "unpack"):
        sp.add_argument("-f", "--files-only", action="store_true")
    if cmd == "unpack":
        sp.add_argument("-i", "--ignore", action="store_true")
    return sp
# ---------------------------------------------------------------- CLI -------
# One usage line per command, shown when its arguments don't parse.
USAGES = {
    "sb":         "sb <command> [arguments]",
    "sb save":    'sb save "<message>" [--allow-secrets] [--no-verify]',
    "sb log":     "sb log [-n <count>]",
    "sb diff":    "sb diff [<path>]",
    "sb restore": "sb restore <anchor | save | release-label | branch>",
    "sb undo":    "sb undo [-p <path>]",
    "sb branch":  "sb branch [<name>] [-r]",
    "sb switch":  "sb switch <branch>",
    "sb merge":   "sb merge <branch> [--no-verify] [-i]",
    "sb test":    "sb test [<stage> | guide | list | new <stage> <name>]",
    "sb publish": "sb publish [<label>] [-l] [--no-verify]",
    "sb verify":  "sb verify [-a <hash>]",
    "sb journal": "sb journal [-n <count>]",
    "sb who":     "sb who [<name>] [<email>]",
    "sb durability": "sb durability [full|normal]",
    "sb locks":   "sb locks",
    "sb unlock":  "sb unlock [<path>...] [--force]",
    "sb salvage": "sb salvage <hash> [<path>]",
    "sb status":  "sb status [--deep]",
    "sb ignore":  "sb ignore <pattern>",
    "sb pack":    "sb pack [<output>] -k <passkey> [-f]",
    "sb unpack":  "sb unpack <file.sbox> [<destination>] -k <passkey> [-f] [-i]",
    "sb export":  "sb export <version> [<destination>] [-k <passkey>]",
}

def _arg_error(prog, message):
    """Report an argument failure in sb's own error style: the cleaned
    message, that command's usage line, and a pointer to the menu."""
    # asking for help is not an error — show the menu and leave happy
    if "unrecognized arguments" in message and \
            any(t in message.split() for t in ("-h", "--help")):
        print(HELP)
        sys.exit(0)
    # a mistyped command gets a short, human answer, not a choice dump
    m = re.search(r"invalid choice: '([^']*)'", message)
    if m:
        die(f"'{m.group(1)}' is not an sb command\n"
            f"       see the full menu:  sb help")
    # everything else: cleaned-up argparse message + the right usage line
    message = message.replace(
        "the following arguments are required:", "missing:")
    lines = [f"{prog}: {message}"]
    if prog in USAGES:
        lines.append(f"       usage:  {USAGES[prog]}")
    lines.append("       see the full menu:  sb help")
    die("\n".join(lines))

class SBParser(argparse.ArgumentParser):
    """argparse that reports failures through _arg_error instead of the
    stock usage dump."""

    def __init__(self, *a, **kw):
        kw.setdefault("add_help", False)
        super().__init__(*a, **kw)

    def error(self, message):
        _arg_error(self.prog.strip(), message)

CMD_W = 33           # width of the command column in the help menu
def _row(cmd, desc, last=False):
    conn = amber("\u2514\u2500\u2500\u2500" if last else "\u251c\u2500\u2500\u2500")
    return f"  {conn} {cmd.ljust(CMD_W)}{dim(desc)}"

def _opt(flag, desc, cont=True):
    """An option line, indented beneath its command — visually subordinate,
    never mistakable for a command of its own."""
    bar = amber("\u2502") if cont else " "
    return f"  {bar}       {dim(flag.ljust(CMD_W - 3))}{dim(desc)}"

HELP = f"""
  {bold('sandbox (sb)')}   {dim('version ' + VERSION)}
  {dim('optimized local version control · ' + AUTHOR)}

{amber('work')}
{_row('init', 'start tracking this folder')}
{_row('status', 'what changed since the last save')}
{_opt('--deep', 'hash every file instead of trusting the stat cache')}
{_row('save "<message>"', 'snapshot your changes')}
{_opt('--allow-secrets', 'save detected secrets verbatim (no redaction)')}
{_opt('--no-verify', 'skip the pre-save tests')}
{_opt('--global-force', "save everyone's edits, not just yours")}
{_row('log', 'history of saves, newest first')}
{_opt('-n, --limit <count>', 'show only the newest <count> saves')}
{_row('diff [<path>]', 'line-by-line changes, all files or one path')}
{_row('undo', 'revert the last save, keeping history')}
{_opt('-p, --path <path>', 'bring back just one file or folder instead')}
{_row('restore <version>', 'return to any past anchor, save, or release', last=True)}

{amber('branches')}
{_row('branch [<name>]', 'list branches, or create one — a new branch')}
{_opt('', 'saves this folder at once, so it is mergeable straight away')}
{_opt('-r, --remove', 'remove branch <name> instead of creating it')}
{_row('switch <branch>', 'move between branches')}
{_row('merge <branch>', 'bring <branch> into the current one', last=True)}
{_opt('--no-verify', 'skip the pre-merge tests', cont=False)}
{_opt('-i, --ignore', 'skip files locked by others', cont=False)}

{amber('quality')}
{_row('test [<stage>]', 'run test gates in a clean checkout')}
{_row('test guide', 'how to set up test scripts')}
{_row('test new <stage> <name>', 'scaffold a test script')}
{_row('test list', 'show discovered tests')}
{_row('publish [<label>]', 'verify + test + record a release')}
{_opt('-l, --list', 'show recorded releases')}
{_opt('--no-verify', 'record the release even if tests fail')}
{_row('verify', 're-check objects, journal, and branch tips', last=True)}
{_opt('-a, --anchor <hash>', 'also confirm a saved anchor is in the chain', cont=False)}

{amber('share')}
{_row('pack [<output>]', 'seal the repo into an encrypted .sbox')}
{_opt('-k, --key <passkey>', 'pass-key to encrypt with (required)')}
{_opt('-f, --files-only', 'seal only the saved files, no history')}
{_row('unpack <file> [<destination>]', 'restore a .sbox archive')}
{_opt('-k, --key <passkey>', 'pass-key it was sealed with (required)')}
{_opt('-f, --files-only', 'write just the files, no .sb directory')}
{_opt('-i, --ignore', 'merge into an existing folder, overwriting matches')}
{_row('export <version> [<destination>]', 'files of a release, branch, or save', last=True)}
{_opt('-k, --key <passkey>', 'write an encrypted .sbox instead of a folder', cont=False)}

{amber('shared editing')}
{_row('locks', 'who holds which file, and for how long')}
{_opt('', 'editing a file locks it: your version wins until you save,')}
{_opt('', "and anyone else's edit to it is put back on the next command")}
{_row('unlock [<path>...]', "release your locks (--force: others')")}
{_row('salvage <hash> [<path>]', 'write stored content back out to a file', last=True)}
{_opt('', 'how you get back an edit a lock put back', cont=False)}

{amber('repository')}
{_row('journal', 'log of every operation')}
{_opt('-n, --limit <count>', 'show only the newest <count> entries')}
{_row('info', 'stats and chain head')}
{_row('who [<name>] [<email>]', 'set or show how saves are attributed')}
{_row('durability [full|normal]', 'crash/power-loss durability')}
{_row('ignore <pattern>', 'add a .sbignore pattern', last=True)}
"""

def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP); return
    if argv[0] in ("-V", "--version", "version"):
        print(f"sb {VERSION} · {AUTHOR}"); return
    if argv[0] in ("pack", "unpack", "export"):
        # parse_intermixed_args lets options sit anywhere among positionals,
        # e.g. 'sb unpack backup.sbox -k KEY restored'
        args = _share_parser(argv[0]).parse_intermixed_args(argv[1:])
        args.cmd = argv[0]
    else:
        p = SBParser(prog="sb")
        sub = p.add_subparsers(dest="cmd", parser_class=SBParser)
        sub.add_parser("init")
        stp = sub.add_parser("status")
        stp.add_argument("--deep", action="store_true")
        dur = sub.add_parser("durability"); dur.add_argument("value", nargs="?")
        sp = sub.add_parser("save"); sp.add_argument("message", nargs="?")
        sp.add_argument("--allow-secrets", action="store_true")
        sp.add_argument("--no-verify", action="store_true")
        sp.add_argument("--global-force", action="store_true")
        lp = sub.add_parser("log")
        lp.add_argument("-n", "--limit", type=int, default=0, metavar="<count>")
        dp = sub.add_parser("diff"); dp.add_argument("path", nargs="?")
        up = sub.add_parser("undo"); up.add_argument("-p", "--path", metavar="<path>")
        rp = sub.add_parser("restore"); rp.add_argument("target")
        bp = sub.add_parser("branch"); bp.add_argument("name", nargs="?")
        bp.add_argument("-r", "--remove", action="store_true")
        bp.add_argument("--allow-secrets", action="store_true")
        wp = sub.add_parser("switch"); wp.add_argument("target")
        mp = sub.add_parser("merge"); mp.add_argument("branch")
        mp.add_argument("--no-verify", action="store_true")
        mp.add_argument("-i", "--ignore", action="store_true")
        lkp = sub.add_parser("locks"); lkp.add_argument("args", nargs="*")
        ulp = sub.add_parser("unlock"); ulp.add_argument("paths", nargs="*")
        ulp.add_argument("--force", action="store_true")
        svp = sub.add_parser("salvage"); svp.add_argument("hash")
        svp.add_argument("dest", nargs="?")
        tp = sub.add_parser("test"); tp.add_argument("args", nargs="*")
        dpl = sub.add_parser("publish"); dpl.add_argument("label", nargs="?")
        dpl.add_argument("-l", "--list", action="store_true")
        dpl.add_argument("--no-verify", action="store_true")
        vp = sub.add_parser("verify")
        vp.add_argument("-a", "--anchor", metavar="<hash>")
        jp = sub.add_parser("journal")
        jp.add_argument("-n", "--limit", type=int, default=0, metavar="<count>")
        sub.add_parser("info")
        who = sub.add_parser("who"); who.add_argument("name", nargs="?")
        who.add_argument("email", nargs="?")
        gp = sub.add_parser("ignore"); gp.add_argument("pattern")
        sub.add_parser("selftest")
        # parse_known_args so leftovers can be blamed on the command the
        # user actually typed ('sb log: ...'), not the bare 'sb' parser
        args, extra = p.parse_known_args(argv)
        if extra:
            prog = f"sb {args.cmd}" if args.cmd else "sb"
            _arg_error(prog, "unrecognized arguments: " + " ".join(extra))
    if args.cmd is None:
        print(HELP); return
    try:
        globals()[f"cmd_{args.cmd}"](args)
    except CorruptObject as e:
        die(str(e) + " — run 'sb verify' for a full report")
    except TamperedJournal as e:
        die(f"journal integrity: {e} — run 'sb verify' for a full report")
    except CheckoutConflict as e:
        die(str(e))
    except KeyError as e:
        die(f"missing object {short(str(e.args[0]) if e.args else '?')} — "
            f"the store references content it does not hold; run 'sb verify'")
    except sqlite3.Error as e:
        die(f"store error: {e} — the database may be locked or damaged; "
            f"run 'sb verify'")
    except BrokenPipeError:
        raise                            # handled by the top-level guard
    except OSError as e:
        die(f"file system error: {e}")

if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
