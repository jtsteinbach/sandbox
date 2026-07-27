#!/usr/bin/env python3
# sandbox (sb): version control in a single file.
# version 1.3 · jts.gg/sandbox

import sys, os, io, json, time, zlib, hashlib, fnmatch, difflib, re
import argparse, contextlib
import sqlite3, subprocess, tempfile, getpass, shutil, stat
from pathlib import Path

VERSION = "1.3"
AUTHOR = "jts.gg/sandbox"
FORMAT_VERSION = 1
CHUNK_THRESHOLD = 8 * 1024 * 1024   # bigger than this is stored in pieces
CHUNK_SIZE = 1024 * 1024            # one piece
SB_DIR = ".sb"
DB_NAME = "sandbox.db"

# journal ops that move a branch tip. verify checks refs against these
REF_OPS = ("save", "merge", "undo", "restore", "branch", "ref",
           "autosave")

# === output ===
# three colors: bold white, dim gray, one amber accent. red means failure
def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else str(s)
def dim(s):    return _c("2", s)             # gray: secondary text
def bold(s):   return _c("1", s)             # white: names and emphasis
def amber(s):  return _c("38;5;215", s)      # amber: connectors and ids
def red(s):    return _c("31", s)            # red: failures only
# aliases, so call sites stay inside the palette
def green(s):  return bold(s)                # success reads as bold white
def yellow(s): return amber(s)               # highlights / ids read as amber
def cyan(s):   return amber(s)               # paths / ids read as amber

def tree_print(lines, indent="  "):
    # Print lines under the previous message with light connector glyphs.
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
    # a stored object failed its integrity check
    pass

class TamperedJournal(Exception):
    # The journal hash chain does not verify.
    pass

# === hashing ===
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def canonical(obj) -> bytes:
    # Deterministic JSON encoding used for trees, commits and journal links.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()

def hash_file(path, size):
    # the same hash as a blob of that content, read a piece at a time
    hasher = hashlib.sha256()
    hasher.update(f"blob {size}\0".encode())
    with open(path, "rb") as f:
        while True:
            c = f.read(CHUNK_SIZE)
            if not c:
                break
            hasher.update(c)
    return hasher.hexdigest()

def hash_obj(kind: str, data: bytes) -> str:
    return sha256_hex(f"{kind} {len(data)}\0".encode() + data)

# === the store ===
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
    uid    INTEGER NOT NULL DEFAULT -1,
    perm   INTEGER NOT NULL DEFAULT -1
);
"""

def find_repo(start="."):
    p = Path(start).resolve()
    while True:
        if (p / SB_DIR / DB_NAME).is_file():
            return p
        if (p / SB_DIR).is_dir():          # legacy loose file layout
            if (p / SB_DIR / "objects").is_dir():
                die(f"{p / SB_DIR} uses the old loose-file format.\n"
                    "       this version stores everything in one crash-safe "
                    "database.\n       re-init in a fresh folder and copy your "
                    "files in, or keep using the old sb for that repository.")
        if p.parent == p:
            return None
        p = p.parent

_UNSET = object()   # sentinel: no expected value given, so no CAS

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
        # FULL keeps the newest commit through power loss. normal is faster
        # and still crash safe, but may lose it
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
        # one atomic unit of work. nested calls join the outermost one, so a
        # command commits or rolls back whole. any exception rolls it back
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
        # add the ctime and inode columns to an older cache, drop stale rows
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
        # add the columns a content lock needs: held, its mode, its uid.
        # an older lock has no held, so enforcement adopts what is on disk
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
            if "perm" not in cols:
                self.db.execute("ALTER TABLE locks ADD COLUMN "
                                "perm INTEGER NOT NULL DEFAULT -1")

    # === meta ===
    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self.transaction():
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))

    # === object store ===
    # a big file is stored as a list of chunk hashes rather than one blob,
    # so a later version of it only costs the chunks that actually changed
    # and no single read has to hold the whole thing
    def put(self, kind: str, data: bytes) -> str:
        if kind == "blob" and len(data) >= CHUNK_THRESHOLD:
            h = hash_obj(kind, data)
            if self.has(h):
                return h
            return self._put_chunks(
                h, len(data),
                (data[i:i + CHUNK_SIZE]
                 for i in range(0, len(data), CHUNK_SIZE)))
        h = hash_obj(kind, data)
        with self.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO objects(hash,kind,size,data) VALUES(?,?,?,?)",
                (h, kind, len(data), zlib.compress(data)))
        return h

    def _put_chunks(self, h, size, chunks):
        refs = []
        with self.transaction():
            for c in chunks:
                ch = hash_obj("chunk", c)
                if not self.has(ch):
                    self.db.execute(
                        "INSERT OR IGNORE INTO objects(hash,kind,size,data) "
                        "VALUES(?,'chunk',?,?)",
                        (ch, len(c), zlib.compress(c)))
                refs.append(ch)
            body = canonical(refs)
            self.db.execute(
                "INSERT OR IGNORE INTO objects(hash,kind,size,data) "
                "VALUES(?,'chunked',?,?)", (h, size, zlib.compress(body)))
        return h

    def put_file(self, path, size, known_hash=None):
        # store a file without ever holding it whole: hash and chunk as it
        # is read. small files take the ordinary blob path
        if size < CHUNK_THRESHOLD:
            return self.put("blob", Path(path).read_bytes())
        if known_hash and self.has(known_hash):
            return known_hash         # already stored: nothing to re-read
        hasher = hashlib.sha256()
        hasher.update(f"blob {size}\0".encode())
        refs, total = [], 0
        with self.transaction():
            with open(path, "rb") as f:
                while True:
                    c = f.read(CHUNK_SIZE)
                    if not c:
                        break
                    total += len(c)
                    hasher.update(c)
                    ch = hash_obj("chunk", c)
                    if not self.has(ch):
                        self.db.execute(
                            "INSERT OR IGNORE INTO objects"
                            "(hash,kind,size,data) VALUES(?,'chunk',?,?)",
                            (ch, len(c), zlib.compress(c)))
                    refs.append(ch)
            if total != size:
                raise CheckoutConflict(
                    f"{path} changed size while it was being read")
            h = hasher.hexdigest()
            self.db.execute(
                "INSERT OR IGNORE INTO objects(hash,kind,size,data) "
                "VALUES(?,'chunked',?,?)",
                (h, size, zlib.compress(canonical(refs))))
        return h

    def _chunk_refs(self, blob):
        try:
            refs = json.loads(zlib.decompress(blob))
        except (zlib.error, ValueError):
            raise CorruptObject("a chunk list in the store is unreadable")
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            raise CorruptObject("a chunk list in the store is malformed")
        return refs

    def _chunk_data(self, ref):
        row = self.db.execute(
            "SELECT kind, data FROM objects WHERE hash=?", (ref,)).fetchone()
        if row is None:
            raise CorruptObject(f"chunk {short(ref)} is missing from the store")
        kind, blob = row
        try:
            data = zlib.decompress(blob)
        except zlib.error:
            raise CorruptObject(f"chunk {short(ref)} is unreadable")
        if hash_obj(kind, data) != ref:
            raise CorruptObject(f"chunk {short(ref)} does not match its hash")
        return data

    def stream(self, h):
        # yield an object's bytes piece by piece, for writing straight out.
        # each chunk is checked as it goes AND the reassembled whole is
        # checked at the end: a chunk list that was reordered or repointed
        # at other valid chunks passes every per chunk check, so without
        # the running hash a tampered list would write out silently.
        # callers write through an atomic temp file, so raising at the end
        # means nothing is left on disk
        row = self.db.execute(
            "SELECT kind, size, data FROM objects WHERE hash=?",
            (h,)).fetchone()
        if row is None:
            raise KeyError(h)
        kind, size, blob = row
        if kind != "chunked":
            yield self.get(h)[1]
            return
        hasher = hashlib.sha256()
        hasher.update(f"blob {int(size)}\0".encode())
        total = 0
        for ref in self._chunk_refs(blob):
            data = self._chunk_data(ref)
            total += len(data)
            hasher.update(data)
            yield data
        if total != int(size) or hasher.hexdigest() != h:
            raise CorruptObject(
                f"object {short(h)} does not match its hash "
                f"(its chunk list is wrong)")

    def verify_object(self, h):
        # check an object without materializing it. a chunked file is
        # verified a piece at a time, so verify costs one chunk of memory
        # however large the file is. raises KeyError or CorruptObject
        row = self.db.execute(
            "SELECT kind, size FROM objects WHERE hash=?", (h,)).fetchone()
        if row is None:
            raise KeyError(h)
        if row[0] != "chunked":
            self.get(h)
            return
        for _ in self.stream(h):     # stream checks each chunk and the whole
            pass

    def has(self, h: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM objects WHERE hash=?", (h,)).fetchone() is not None

    def get(self, h: str):
        # (kind, data). rehashed on every read, so damage cannot pass quietly
        row = self.db.execute(
            "SELECT kind, data FROM objects WHERE hash=?", (h,)).fetchone()
        if row is None:
            raise KeyError(h)
        kind, blob = row
        if kind == "chunked":
            data = b"".join(self._chunk_data(r) for r in self._chunk_refs(blob))
            if hash_obj("blob", data) != h:
                raise CorruptObject(
                    f"object {short(h)} content does not match its hash")
            return "blob", data
        try:
            data = zlib.decompress(blob)
        except zlib.error:
            raise CorruptObject(f"object {short(h)} is unreadable (damaged in store)")
        if hash_obj(kind, data) != h:
            raise CorruptObject(f"object {short(h)} content does not match its hash")
        return kind, data

    def resolve(self, name_or_prefix: str):
        # branch name or unambiguous hash prefix, to a full commit hash
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

    # === refs / branch pointer ===
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
        # move a branch tip, with its journal entry, in one transaction.
        # expect makes it a compare and swap: the tip must still equal it or
        # nothing happens, so racing saves fail loudly. extra adds audit
        # fields to the journal entry
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
        # delete a branch tip, with its journal entry, in one transaction
        old = self.tip(branch)
        with self.transaction():
            self.db.execute("DELETE FROM refs WHERE name=?", (branch,))
            self.journal("branch-remove", {"branch": branch, "old": old or "",
                                           "new": ""})

    # === journal ===
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
        # recompute the chain. gives (n_entries, head_link), or raises
        # TamperedJournal at the first broken link
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

    # === stat cache ===
    # keyed on size, mtime, ctime, inode. mtime alone can be forged from
    # userspace, so ctime and inode are what catch a same size edit
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

    # === locks ===
    def locks(self):
        # {path: {owner, email, since, base, held, mode, uid}}. held is the
        # content being protected, LOCK_DELETED if the holder deleted it, or
        # empty for a lock older than content tracking
        out = {}
        for (path, owner, email, since, base, held, mode, uid,
             perm) in self.db.execute(
                "SELECT path,owner,email,since,base,held,mode,uid,perm "
                "FROM locks"):
            out[path] = {"owner": owner, "email": email, "since": since,
                         "base": base, "held": held or "",
                         "mode": mode or "100644",
                         "uid": -1 if uid is None else int(uid),
                         "perm": -1 if perm is None else int(perm)}
        return out

    def set_lock(self, path, owner, email, base, held="", mode="100644",
                 uid=-1, perm=-1):
        with self.transaction():
            self.db.execute(
                "INSERT INTO locks(path,owner,email,since,base,held,mode,uid,"
                "perm) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO NOTHING",
                (path, owner, email, int(time.time()), base or "",
                 held or "", mode or "100644",
                 -1 if uid is None else int(uid),
                 -1 if perm is None else int(perm)))

    def update_lock_held(self, path, held, mode, touch=True):
        # the holder edited again, so protect the new content. touch resets
        # the clock, making expiry mean idle time, not total time held
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
    # record which OS account this identity uses, so later edits found on
    # disk can be attributed. best effort, never blocks
    if shared_mode(repo):
        try:
            register_identity(repo)
        except sqlite3.Error:
            pass
    return repo

# === identity ===
# who made each save, for humans reading history. no keys, no signatures:
# attribution, not authentication
def _sudo_user():
    # the account that invoked sudo, as (uid, gid, name, home), else None.
    # under sudo HOME is root's, so the profile would be missed and every
    # save, pack and export would be filed under root
    uid = os.environ.get("SUDO_UID")
    if not uid:
        return None
    try:
        import pwd
        pw = pwd.getpwuid(int(uid))
        return (pw.pw_uid, pw.pw_gid, pw.pw_name, Path(pw.pw_dir))
    except (ImportError, KeyError, ValueError, OSError):
        name = os.environ.get("SUDO_USER")
        return (int(uid), -1, name, None) if name else None

def _config_dir():
    # where the profile lives: SB_HOME, else the invoking account's home,
    # else this process's home
    if os.environ.get("SB_HOME"):
        return Path(os.environ["SB_HOME"])
    su = _sudo_user()
    if su and su[3] is not None:
        return su[3] / ".config" / "sandbox"
    return Path.home() / ".config" / "sandbox"

CONFIG_DIR = _config_dir()
CONFIG_FILE = CONFIG_DIR / "profile.json"

def author():
    prof = {}
    if CONFIG_FILE.is_file():
        try:
            prof = json.loads(CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    su = _sudo_user()
    fallback = (su[2] if su and su[2] else None) or getpass.getuser()
    name = os.environ.get("SB_NAME") or prof.get("name") or fallback
    email = os.environ.get("SB_EMAIL") or prof.get("email") or f"{name}@local"
    return name, email

# === ignores ===
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

# === secret scanner ===
# stop credentials entering permanent history. a recognized one in clean
# UTF-8 text becomes <REDACTED> in the blob being committed, leaving the
# file on disk alone. a file that cannot be rewritten faithfully blocks
# instead. --allow-secrets stores it verbatim
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
MAX_SCAN_BYTES = 64 * 1024 * 1024      # text this big is still worth scanning
SCAN_WINDOW = 4 * 1024 * 1024          # decoded a window at a time, not whole

def scan_secrets(data: bytes):
    # [(line_no, label), ...] for one file's content
    if len(data) > MAX_SCAN_BYTES or b"\0" in data[:8000]:
        return []                                   # binary or enormous
    hits = []
    for i, line in _iter_lines(data):
        for label, pat in SECRET_PATTERNS:
            if pat.search(line):
                hits.append((i, label))
    return hits

def _iter_lines(data: bytes):
    # (line_no, text) without decoding the whole file at once, so a large
    # text file costs a window rather than its own size again
    lineno, start = 1, 0
    n = len(data)
    while start < n:
        end = min(n, start + SCAN_WINDOW)
        if end < n:                       # never split a line across windows
            nl = data.rfind(b"\n", start, end)
            end = nl + 1 if nl > start else end
        chunk = data[start:end].decode("utf-8", errors="replace")
        for line in chunk.splitlines():
            yield lineno, line
            lineno += 1
        start = end

REDACTED = "<REDACTED>"

# the key material is the body below the BEGIN line, so redaction covers
# the whole block: BEGIN through END, or end of file if truncated
_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    r".*?"
    r"(?:-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"
    r"|\Z)", re.S)

def redact_secrets(data: bytes):
    # replace every recognized credential with <REDACTED>.
    # gives (new_data, findings, safe), where safe is False when something
    # was found in a file that is not clean UTF-8: it cannot be rewritten
    # without corrupting it, so the caller must block. binary and oversized
    # files are skipped, and the file on disk is never touched
    if len(data) > MAX_SCAN_BYTES or b"\0" in data[:8000]:
        return data, [], True                       # binary or huge: skip
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # cannot rewrite without mangling bytes, so report and let the
        # caller block the save
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
                continue                             # handled above, as a whole block
            line, n = pat.subn(REDACTED, line)
            if n:
                findings.append((i + 1, label))
        lines[i] = line
    if not findings:
        return data, [], True
    return "\n".join(lines).encode("utf-8"), findings, True

def _redact_for_commit(repo, disk, paths, allow_secrets=False):
    # the redaction pass every commit path shares. gives blob overrides to
    # layer over disk, what was redacted for the report, and anything the
    # caller must stop on because it cannot be rewritten faithfully
    over, redacted, hard = {}, [], []
    if allow_secrets:
        return over, redacted, hard
    for rel in paths:
        if rel not in disk:
            continue                        # a deletion, nothing to scan
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

# === trees ===
_BAD_NAME = re.compile(r"^\.?\.?$")   # "", ".", ".."

def safe_name(name: str) -> bool:
    # names must be single path components. this is what stops a crafted
    # tree from writing outside the repository on checkout
    return bool(name) and "/" not in name and "\\" not in name \
        and "\0" not in name and not _BAD_NAME.match(name) and name != SB_DIR

SYMLINK_MODE = "120000"     # the blob holds the target path, not file bytes

def is_link_mode(mode):
    return mode == SYMLINK_MODE

RACY_WINDOW_NS = 2_000_000_000   # files whose mtime OR ctime is < 2s old
                                 # bypass the stat cache and get reread

# windows reports st_ctime as creation time, so a same size edit with a
# restored mtime would slip past the cache. hash everything there instead
_STATCACHE_TRUSTWORTHY = not sys.platform.startswith("win")

def tracked_paths(repo):
    # what the last save holds. an ignore rule never drops one of these:
    # ignoring only decides what gets picked up, not what gets dropped
    try:
        tree, _ = head_tree_files(repo)
        return set(tree)
    except (KeyError, CorruptObject):
        return set()

def _tracked_dirs(tracked):
    out = set()
    for rel in tracked:
        parts = rel.split("/")[:-1]
        for i in range(len(parts)):
            out.add("/".join(parts[:i + 1]))
    return out

def snapshot_worktree(repo: Repo, write=True, deep=False):
    # walk the working tree into {rel: (mode, blob_hash)}. write=True also
    # stores the blobs. the stat cache skips unchanged files unless deep is
    # set or the platform's ctime cannot be trusted
    use_cache = _STATCACHE_TRUSTWORTHY and not deep
    pats = load_ignores(repo.root)
    tracked = tracked_paths(repo)
    tdirs = _tracked_dirs(tracked)
    files, cache_updates, symlinks = {}, [], 0
    now_ns = time.time_ns()
    for dirpath, dirnames, filenames in os.walk(repo.root):
        rel_dir = os.path.relpath(dirpath, repo.root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        linked_dirs = [d for d in dirnames
                       if os.path.islink(os.path.join(dirpath, d))]
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in linked_dirs
            and (lambda rd: not is_ignored(rd, pats) or rd in tdirs)
                ((rel_dir + "/" + d).lstrip("/")))
        for d in sorted(linked_dirs):       # a link to a directory is a link
            rel = (rel_dir + "/" + d).lstrip("/")
            if is_ignored(rel, pats) and rel not in tracked:
                continue
            target = os.readlink(os.path.join(dirpath, d)).encode()
            h = repo.put("blob", target) if write else hash_obj("blob", target)
            files[rel] = (SYMLINK_MODE, h)
        for fn in sorted(filenames):
            rel = (rel_dir + "/" + fn).lstrip("/")
            if is_ignored(rel, pats) and rel not in tracked:
                continue
            p = Path(dirpath) / fn
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(p).encode()
                h = repo.put("blob", target) if write else hash_obj("blob", target)
                files[rel] = (SYMLINK_MODE, h)
                symlinks += 1
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
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
            if st.st_size >= CHUNK_THRESHOLD:
                # h may hold a cached hash the store already has, in which
                # case put_file returns without re-reading the file
                h = (repo.put_file(p, st.st_size, known_hash=h) if write
                     else hash_file(p, st.st_size))
            else:
                data = p.read_bytes()
                h = repo.put("blob", data) if write else hash_obj("blob", data)
            files[rel] = (mode, h)
            cache_updates.append((rel, st.st_size, st.st_mtime_ns,
                                  st.st_ctime_ns, st.st_ino, h))
    repo.remember(cache_updates)
    return files

def build_tree(repo: Repo, files: dict) -> str:
    # {rel: (mode, blob_hash)} into nested trees, giving the root hash
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
    # Flatten a tree object to {rel: (mode, blob_hash)}. Validates names.
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

# === commits ===
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
    # gives sorted (added, modified, deleted) path lists
    added    = sorted(p for p in work if p not in tree)
    deleted  = sorted(p for p in tree if p not in work)
    modified = sorted(p for p in work if p in tree and work[p] != tree[p])
    return added, modified, deleted

_EMPTY_BLOB = hash_obj("blob", b"")

RENAME_SIMILARITY = 0.5     # share at least half your content to be a rename

# Renaming is detected by hashes, in two layers. Identical content is an
# exact hash match, which is free. A file that moved AND was edited has a
# different hash by definition, so equality alone can never find it; what
# does find it is hashing the file in PIECES and matching the pieces.
#
# The piece boundaries are chosen by content, not by offset: a rolling hash
# over a 48 byte window cuts wherever the low bits hit a fixed pattern.
# Inserting or deleting bytes then shifts only the piece it landed in,
# leaving every other boundary where it was, so two versions of one file
# still share nearly all their piece hashes. Fixed size blocks do not have
# this property, since one inserted byte moves every later boundary and
# changes every later hash.
CDC_WINDOW = 48
CDC_MIN = 512
CDC_MAX = 16384
CDC_MASK = 0x3FF            # about one cut every 1k on random data

def _cdc_pieces(data: bytes):
    # content defined boundaries via a rolling sum over the last window
    n = len(data)
    if n <= CDC_MIN:
        yield data
        return
    start = 0
    while start < n:
        limit = min(start + CDC_MAX, n)
        cut = limit
        if limit - start > CDC_MIN:
            roll = 0
            scan = start + CDC_MIN
            for i in range(max(start, scan - CDC_WINDOW), scan):
                roll = ((roll << 1) + data[i]) & 0xFFFFFFFF
            for i in range(scan, limit):
                roll = ((roll << 1) + data[i]) & 0xFFFFFFFF
                if i >= CDC_WINDOW:
                    roll -= data[i - CDC_WINDOW] << CDC_WINDOW
                    roll &= 0xFFFFFFFF
                if (roll & CDC_MASK) == CDC_MASK:
                    cut = i + 1
                    break
        yield data[start:cut]
        start = cut

def _shingles(repo, h, cache):
    # the set of piece hashes for one object. text is split on lines, which
    # are already content defined and cheaper; anything else uses the
    # rolling hash so that inserted bytes do not shift every later piece
    if h in cache:
        return cache[h]
    out = set()
    try:
        data = repo.get(h)[1]
    except (KeyError, CorruptObject):
        cache[h] = out
        return out
    if b"\0" in data[:8000]:
        for piece in _cdc_pieces(data):
            out.add(hashlib.blake2b(piece, digest_size=8).digest())
    else:
        for line in data.split(b"\n"):
            line = line.strip()
            if line:
                out.add(hashlib.blake2b(line, digest_size=8).digest())
    cache[h] = out
    return out

def _similarity(repo, ha, hb, cache):
    a, b = _shingles(repo, ha, cache), _shingles(repo, hb, cache)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def detect_renames(new_files, old_files, added, deleted, repo=None):
    # exact content pairs first, then content similarity for what is left, so
    # a file that moved and was edited still reads as one rename
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
    left_del = [p for p in sorted(deleted) if p not in gone]
    if repo is not None and still_added and left_del:
        renames += _similar_renames(repo, new_files, old_files,
                                    still_added, left_del)
        gone = {o for o, _ in renames}
        taken = {n for _, n in renames}
        still_added = [p for p in still_added if p not in taken]
        left_del = [p for p in left_del if p not in gone]
    return renames, still_added, left_del

def _similar_renames(repo, new_files, old_files, added, deleted):
    # An inverted index from piece hash to the deleted paths holding it.
    # Only paths that share at least one piece are ever compared, so this
    # does not need a pair budget: unrelated files never meet, and the work
    # is proportional to actual shared content rather than to added*deleted.
    cache = {}
    index = {}
    for d in sorted(deleted):
        dm, dh = old_files[d]
        if is_link_mode(dm):
            continue
        for piece in _shingles(repo, dh, cache):
            index.setdefault(piece, []).append(d)
    scored = []
    for a in sorted(added):
        am, ah = new_files[a]
        if is_link_mode(am):
            continue
        hits = {}
        for piece in _shingles(repo, ah, cache):
            for d in index.get(piece, ()):
                hits[d] = hits.get(d, 0) + 1
        for d in sorted(hits):
            score = _similarity(repo, ah, old_files[d][1], cache)
            if score >= RENAME_SIMILARITY:
                scored.append((score, d, a))
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    used_old, used_new, out = set(), set(), []
    for _, d, a in scored:
        if d in used_old or a in used_new:
            continue
        used_old.add(d); used_new.add(a)
        out.append((d, a))
    return sorted(out)

# === checkout / cleanup ===
def _safe_parent_fd(root_fd: int, rel: str):
    # open the parent of rel without following any symlinked component.
    # gives (parent_fd, leaf_name); the caller closes the fd. raises
    # CheckoutConflict when a component that must be a directory is not
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
                # ELOOP is a symlink component, ENOTDIR a file in the way.
                # either way the path is unsafe
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

# O_NOFOLLOW and O_DIRECTORY are POSIX. elsewhere fall back to 0 and rely
# on the lstat check below, which windows junctions need anyway
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

class CheckoutConflict(Exception):
    # a path in the worktree blocks the target: a symlink or file where a
    # parent must be, or a directory where a file must go
    pass

def _lstat_at(dir_fd, name):
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except (FileNotFoundError, NotADirectoryError):
        return None

def _remove_at(dir_fd, name):
    # delete name under dir_fd, file or symlink or directory tree, without
    # following a symlink out of the repository
    st = _lstat_at(dir_fd, name)
    if st is None:
        return
    if stat.S_ISDIR(st.st_mode):
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
    # deletions first, so a file becoming a directory frees the name in
    # time. then creations, parents before children, so a directory becoming
    # a file clears its contents before the name is reused
    deletions = [rel for rel in current if rel not in target]
    # a path changing between file and directory shows up as old paths
    # leaving and a new one arriving. sorting orders them, and the
    # remove and create below tolerate whatever is really there
    creations = sorted(
        (rel for rel, mh in target.items()
         if current.get(rel) != mh or True),  # recheck on disk at apply time
        key=lambda r: r.count("/"))
    return deletions, creations

def _redaction_matches_target(repo, target, paths):
    # paths whose file on disk redacts to exactly what the target holds.
    # history stores <REDACTED>; the working file keeps the real value. a
    # checkout must not "restore" the redacted form over it, or the only
    # copy of a live credential is gone
    out = set()
    for p in paths:
        entry = target.get(p)
        if entry is None:
            continue
        try:
            data = (repo.root / p).read_bytes()
        except OSError:
            continue
        red, findings, safe = redact_secrets(data)
        if findings and safe and hash_obj("blob", red) == entry[1]:
            out.add(p)
    return out

def _checkout_preserving(repo, target, current, preserve):
    # checkout_tree, but never touching preserved paths: a lock holder's
    # version, or files an --ignore merge left alone
    preserve = set(preserve or ())
    # a file that differs from the target only by redaction is already
    # correct: rewriting it would destroy the secret it deliberately keeps
    candidates = {p for p in target
                  if p not in preserve and current.get(p) != target.get(p)}
    keep = _redaction_matches_target(repo, target, candidates)
    preserve |= keep
    # what is left holds a secret on disk AND genuinely differs from the
    # target, so the checkout is about to replace the only copy of it
    losing = sorted(_paths_with_secrets(repo, candidates - keep))
    if losing:
        print(yellow("note: ") + dim("replacing file(s) whose working copy "
              "holds a credential history never stored:"))
        tree_print([cyan(p) for p in losing[:5]])
    t = {k: v for k, v in target.items() if k not in preserve}
    c = {k: v for k, v in current.items() if k not in preserve}
    checkout_tree(repo, t, c, preserve=preserve)

def _paths_with_secrets(repo, paths):
    out = set()
    for p in paths:
        try:
            data = (repo.root / p).read_bytes()
        except OSError:
            continue
        if scan_secrets(data):
            out.add(p)
    return out

def checkout_tree(repo: Repo, target: dict, current: dict, preserve=None):
    # make the worktree equal target, in an order that survives files and
    # directories swapping places. every write goes through a parent opened
    # without following symlinks, so nothing can be redirected out of the
    # repository. single writes are atomic; the whole tree is not, but a
    # crash leaves ordinary unsaved changes, never a torn object
    preserve = preserve or set()
    root_fd = os.open(str(repo.root), os.O_RDONLY | _O_DIRECTORY)
    try:
        deletions, creations = _plan_checkout(target, current)
        # phase 0: check every parent chain first, so a conflict fails
        # before anything is touched
        for rel in creations:
            mode, h = target[rel]
            if current.get(rel) == (mode, h):
                pfd, leaf = _safe_parent_fd(root_fd, rel)
                try:
                    st = _lstat_at(pfd, leaf)
                    if st is not None and (stat.S_ISREG(st.st_mode)
                                           or stat.S_ISLNK(st.st_mode)):
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
        # phase 2: creations, parents first
        for rel in creations:
            if rel in preserve:
                continue
            mode, h = target[rel]
            pfd, leaf = _safe_parent_fd(root_fd, rel)
            try:
                st = _lstat_at(pfd, leaf)
                if st is not None:
                    right_shape = (stat.S_ISLNK(st.st_mode) if is_link_mode(mode)
                                   else stat.S_ISREG(st.st_mode))
                    if right_shape and current.get(rel) == (mode, h):
                        continue          # already what we want, leave it
                    _remove_at(pfd, leaf)   # wrong shape or content
                _materialize_entry(repo, pfd, leaf, mode, h)
            finally:
                os.close(pfd)
        _prune_empty_dirs(repo)
    finally:
        os.close(root_fd)

def _materialize_entry(repo, parent_fd, name, mode, h):
    # the streaming form: a large file never becomes one bytes object
    if is_link_mode(mode):
        _write_symlink_at(parent_fd, name,
                          repo.get(h)[1].decode("utf-8", "replace"))
        return
    _write_file_at(parent_fd, name, repo.stream(h),
                   0o755 if mode == "100755" else 0o644)

def _write_symlink_at(parent_fd: int, name: str, target: str):
    # made under a random name and renamed in, so a half written link is
    # never visible under the real one
    tmpname = f".sb-{os.urandom(6).hex()}.tmp"
    os.symlink(target, tmpname, dir_fd=parent_fd)
    try:
        os.replace(tmpname, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmpname, dir_fd=parent_fd)
        raise
    with contextlib.suppress(OSError):
        os.fsync(parent_fd)

def _write_file_at(parent_fd: int, name: str, data: bytes, perm: int):
    # create a randomized temp file with O_EXCL, write and fsync it, rename
    # it onto name, fsync the directory. the temp name is unpredictable, so
    # there is nothing to plant a symlink on
    tmpname = f".sb-{os.urandom(6).hex()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
    fd = os.open(tmpname, flags, perm, dir_fd=parent_fd)
    try:
        # os.write may write fewer bytes than asked; loop until all land
        pieces = (data,) if isinstance(data, (bytes, bytearray)) else data
        for piece in pieces:
            view = memoryview(piece)
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
    # remove directories left empty. never enters or removes .sb, and never
    # follows symlinks
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

# === shared locking ===
# a team shares one folder and one database. a lock protects content, not
# permission:
#
#   * editing a file locks it to you
#   * your bytes are then the file. others can edit it, but their version is
#     put back on the next command, and their bytes are stored and named in
#     the journal for 'sb salvage <hash>'
#   * only you can save it. everyone else's save skips it
#   * editing it again moves the lock forward and resets the clock
#   * it ends when you save, when the file matches its saved state, or after
#     LOCK_TTL idle, which auto saves the edits and then reverts them
#
# there is no daemon, so locks are claimed, enforced and expired at the
# start of every command that touches state.
#
# a file on disk says nothing about who edited it, so a lock must not go to
# whoever ran the scan. the signal is the file's owner uid, read through the
# uid registry. with no signal (deletions, windows, squashing mounts, one
# login for several identities) it falls back to the invoking user.
LOCK_TTL = int(os.environ.get("SB_LOCK_TTL", "3600"))   # one hour, seconds
LOCK_DELETED = "deleted"    # held marker: the holder deleted it

def shared_mode(repo):
    # locking is structural, not a setting, so this cannot return False.
    # old journals may still hold 'shared' entries; they are only displayed
    return True

def _my_uid():
    try:
        return os.getuid()
    except AttributeError:          # windows has no usable owner signal
        return None

def register_identity(repo):
    # remember which OS account maps to which identity, so edits found on
    # disk can be attributed to whoever wrote them
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
    # best known (name, email) for an OS account: the registry if they have
    # run sb here, else their system account name
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

def _restore_paths_on_disk(repo, targets, locks=None):
    # write {rel: (mode, hash) or None} onto the worktree, None meaning
    # remove. same symlink safe writes as checkout
    root_fd = os.open(str(repo.root), os.O_RDONLY | _O_DIRECTORY)
    try:
        for rel, mh in sorted(targets.items()):
            pfd, leaf_name = _safe_parent_fd(root_fd, rel)
            try:
                _remove_at(pfd, leaf_name)
                if mh is not None:
                    mode, h = mh
                    _materialize_entry(repo, pfd, leaf_name, mode, h)
            finally:
                os.close(pfd)
    finally:
        os.close(root_fd)
    _prune_empty_dirs(repo)

def redaction_only_paths(repo, tree_files, work, paths):
    # of `paths`, the ones whose only difference from the last save is that
    # the save holds their secrets redacted. the working file is meant to
    # differ, so these are not unsaved work and never count as dirty
    return {p for p in paths
            if _only_redaction_differs(repo, tree_files, work, p)}

def _blob_contains(repo, h, needle: bytes):
    # stream an object looking for a short marker, so this stays cheap and
    # constant memory even when the object is a large chunked file
    tail = b""
    try:
        for piece in repo.stream(h):
            if needle in tail + piece:
                return True
            tail = piece[-len(needle):] if len(piece) >= len(needle) else piece
    except (KeyError, CorruptObject):
        return False
    return False

def _only_redaction_differs(repo, tree_files, work, p):
    # true when the file differs from the last save only because the save
    # holds its secrets redacted. that is not abandoned work: committing it
    # would leak the credential and reverting it would delete the live one,
    # so expiry skips the file and releases its lock
    if p not in work or p not in tree_files:
        return False
    # cheapest tests first: this runs for every changed file on every
    # command, so it must not read or scan a large file to say "no"
    full = repo.root / p
    try:
        st = os.lstat(full)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SCAN_BYTES:
            return False
        with open(full, "rb") as f:
            if b"\0" in f.read(8000):
                return False          # binary is never redacted
    except OSError:
        return False
    # and only a saved copy holding the marker can be this case at all
    if not _blob_contains(repo, tree_files[p][1], REDACTED.encode()):
        return False
    try:
        data = full.read_bytes()
    except OSError:
        return False
    data2, findings, safe = redact_secrets(data)
    return (bool(findings) and safe
            and hash_obj("blob", data2) == tree_files[p][1])

# a lock is enforced by the file system as well as by sb: while someone
# holds one, the file keeps its owner's write bit and loses everyone
# else's, so a second writer's editor refuses the write instead of
# discovering the revert afterwards. sb still reverts, because a shared
# login, a writable parent directory or root can get past the bits
def _perm_bits(path):
    try:
        return os.lstat(path).st_mode & 0o7777
    except OSError:
        return None

def _chmod_quiet(path, bits):
    try:
        if os.path.islink(path):
            return False
        if (os.lstat(path).st_mode & 0o7777) == bits:
            return False              # already right: do not touch ctime
        os.chmod(path, bits)
        return True
    except OSError:
        return False

def lock_perms_on(repo, rel, perm=None):
    # drop group and other write, keep the holder's own
    full = repo.root / rel
    cur = _perm_bits(full)
    if cur is None:
        return perm if perm is not None else -1
    orig = cur if perm is None or perm < 0 else perm
    _chmod_quiet(full, cur & ~0o022)
    return orig

def lock_perms_off(repo, rel, perm):
    # put back exactly what the file had before it was locked
    full = repo.root / rel
    cur = _perm_bits(full)
    if cur is None:
        return
    if perm is not None and perm >= 0:
        _chmod_quiet(full, perm)
    else:
        _chmod_quiet(full, cur | 0o200)

def apply_lock_perms(repo, locks=None):
    # idempotent: any command may have rewritten a locked file
    for rel, l in (locks if locks is not None else repo.locks()).items():
        lock_perms_on(repo, rel, l.get("perm", -1))

def release_locks(repo, paths):
    # restore permissions first, then drop the rows
    paths = list(paths or ())
    if not paths:
        return
    locks = repo.locks()
    for rel in paths:
        if rel in locks:
            lock_perms_off(repo, rel, locks[rel].get("perm", -1))
    repo.clear_locks(paths)

def foreign_locks(repo):
    # paths locked by someone else. never overwritten by a checkout, never
    # counted as my unsaved changes, never included in my saves
    _, email = author()
    return {p for p, l in repo.locks().items() if l["email"] != email}

def _disk_state(repo, rel):
    # (mode, hash, data, st) for a path. anything but a plain file is absent
    p = repo.root / rel
    try:
        st = os.lstat(p)
    except OSError:
        return None, None, b"", None
    if stat.S_ISLNK(st.st_mode):
        try:
            data = os.readlink(p).encode()
        except OSError:
            return None, None, b"", st
        return SYMLINK_MODE, hash_obj("blob", data), data, st
    if not stat.S_ISREG(st.st_mode):
        return None, None, b"", st
    try:
        data = p.read_bytes()
    except OSError:
        return None, None, b"", st
    mode = "100755" if os.access(p, os.X_OK) else "100644"
    return mode, hash_obj("blob", data), data, st

def _lock_actor(repo, lock, st):
    # who most likely changed a locked file. the owner uid wins when it can
    # tell the two people apart, since a file owned by another account was
    # written by it. otherwise fall back to whoever is running sb
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
    # keep every locked file equal to its holder's version. against the
    # content the lock protects, what is on disk is either unchanged, the
    # holder's own new edit (protect that instead, reset the clock), or
    # someone else's (store and journal it, then put the holder's back).
    # a lock matching the last save is released, having nothing left to
    # protect. gives True when anything on disk changed
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
            # a lock older than content tracking: adopt what is on disk
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
        # always reported: someone's bytes just left the disk
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
        release_locks(repo, release)
    if advanced and not quiet:
        tree_print([f"lock on {cyan(p)} " + dim("follows your latest edit")
                    for p in sorted(advanced)])
    return bool(restore)

def process_lock_expiry(repo):
    # for each owner's abandoned edits: auto save them in that owner's name,
    # then revert those paths in history and on disk. the hash is printed and
    # journaled, so 'sb restore <hash>' brings the work back.
    #
    # the auto save keeps bytes verbatim: redacting and then reverting the
    # disk would destroy the only copy of a live credential. secrets are
    # reported and flagged, and a file differing only by an already redacted
    # secret is skipped
    register_identity(repo)
    now = int(time.time())
    expired = [(p, l) for p, l in repo.locks().items()
               if now - l["since"] >= LOCK_TTL]
    if not expired:
        return False
    # write=True: commit these exact blobs, not a later reread
    work = snapshot_worktree(repo, write=True)
    tree_files, head_c = head_tree_files(repo)
    committed = False
    # group by owner: one save + one revert each, attributed to them
    by_owner = {}
    for p, l in expired:
        by_owner.setdefault((l["owner"], l["email"]), []).append(p)
    for (owner, email), paths in by_owner.items():
        changed = [p for p in paths
                   if work.get(p) != tree_files.get(p)
                   and not _only_redaction_differs(repo, tree_files, work, p)]
        if not changed:
            continue
        prior = {p: tree_files.get(p) for p in changed}   # the state before those edits
        secretish = sorted(p for p in changed if p in work
                           and scan_secrets(repo.get(work[p][1])[1]))
        extra = {"secrets_present": secretish} if secretish else None
        h1 = _commit_subset(repo, work, tree_files, head_c, changed,
                            f"auto-save: {owner}'s expired lock(s)",
                            owner=owner, email=email, op="autosave",
                            extra=extra)
        tree_files, head_c = head_tree_files(repo)  # refresh after commit
        # back to the content before the edits. a path that did not exist
        # before is deleted again
        revert_work = {p: mh for p, mh in prior.items() if mh is not None}
        _commit_subset(repo, revert_work, tree_files, head_c, changed,
                       f"auto-revert: {owner}'s expired edits "
                       f"(bring them back: sb restore {short(h1)})",
                       owner=owner, email=email, op="autosave")
        tree_files, head_c = head_tree_files(repo)
        # and put that content back on disk
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
    release_locks(repo, [p for p, _ in expired])
    return committed

def acquire_locks_for_edits(repo, quiet=False):
    # lock every modified file that is not locked yet, each to the person
    # who edited it, recording the content they are protecting.
    #
    # write=True is deliberate: a lock has to be able to put its holder's
    # version back, so those bytes must be stored before anyone overwrites
    # the file.
    #
    # attribution goes by the file's owner uid, so Bob running 'sb status'
    # over Alice's edit creates the lock in her name
    register_identity(repo)
    name, email = author()
    me_uid = _my_uid()
    work = snapshot_worktree(repo, write=True)
    tree_files, _ = head_tree_files(repo)
    a, m, d = worktree_vs_tree(work, tree_files)
    edited = set(a) | set(m) | set(d)
    edited -= redaction_only_paths(repo, tree_files, work, edited)
    locks = repo.locks()
    # unlocked edits, grouped by the file's owner on disk
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
                              uid=-1 if me_uid is None else me_uid,
                              perm=lock_perms_on(repo, p))
            if not quiet:
                tree_print([f"locked {cyan(p)} " + dim("(your version wins "
                            "until you save)") for p in sorted(mine_new)])
        for uid, paths in sorted(theirs.items()):
            o_name, o_email = _uid_identity(repo, uid)
            for p in sorted(paths):
                mode, h = work.get(p, ("100644", None))
                repo.set_lock(p, o_name, o_email, base,
                              held=h or LOCK_DELETED, mode=mode, uid=uid,
                              perm=lock_perms_on(repo, p))
            if not quiet:
                tree_print([f"locked {cyan(p)} to {bold(o_name)} "
                            + dim("(their on-disk edit)")
                            for p in sorted(paths)])

def sync_locks(repo, quiet=False):
    # the whole lifecycle, in the only order that keeps content honest:
    # enforce, expire, then claim. every command touching state runs it
    enforce_locks(repo, quiet=quiet)
    process_lock_expiry(repo)
    acquire_locks_for_edits(repo, quiet=quiet)
    apply_lock_perms(repo)

def _ago(ts):
    s = max(0, int(time.time()) - ts)
    if s < 60:  return f"{s}s ago"
    if s < 3600: return f"{s//60}m ago"
    return f"{s//3600}h{(s%3600)//60}m ago"

def _commit_subset(repo, work, tree_files, head_c, subset, message,
                   *, owner=None, email=None, op="save", extra=None,
                   verify_drift=False, disk_ref=None, extra_parents=()):
    # commit a tree equal to the last one with only subset replaced by its
    # work version. this is how my save leaves everyone else's edits alone.
    #
    # the tree is built from blob hashes already in work, what the caller
    # scanned and gated, never from a fresh read, so what passed the gates
    # is what lands in history. a referenced blob missing from the store is
    # corruption and raises.
    #
    # verify_drift rechecks inside the transaction that disk still matches
    # disk_ref and refuses if the worktree moved while gates ran. pass the
    # original snapshot as disk_ref when work holds redacted blobs
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
        parents = ([head_c["hash"]] if head_c else []) + list(extra_parents)
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
    # refuse to run when the worktree holds changes this would throw away.
    # other people's locked files are exempt, since every checkout preserves
    # them and nothing I do can commit them
    exempt = foreign_locks(repo) | set(extra_exempt or ())
    work = snapshot_worktree(repo, write=False)
    tree, _ = head_tree_files(repo)
    a, m, d = worktree_vs_tree(work, tree)
    changed = (set(a) | set(m) | set(d)) - exempt
    changed -= redaction_only_paths(repo, tree, work, changed)
    dirty = sorted(changed)
    if dirty:
        die("you have unsaved changes — run 'sb save' first (nothing is ever\n"
            "       silently discarded), or 'sb undo -p <path>' to drop them\n"
            "       (" + ", ".join(dirty[:5])
            + (" …" if len(dirty) > 5 else "") + ")")

# === three way merge ===
def _diff_regions(base, side):
    # changed regions of side against base, in base coordinates
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
    # line level three way merge, giving (merged_lines, n_conflicts).
    # separate changes merge, overlapping ones become conflict blocks.
    # touching hunks and insertions at one point count as conflicts: a false
    # conflict is safe, a false merge is not
    ca, cb = _diff_regions(base, ours), _diff_regions(base, theirs)
    out, conflicts = [], 0
    ia = ib = pos = 0
    while ia < len(ca) or ib < len(cb):
        ra = ca[ia] if ia < len(ca) else None
        rb = cb[ib] if ib < len(cb) else None
        both_insert_here = (ra is not None and rb is not None
                            and ra[0] == ra[1] == rb[0] == rb[1])
        if both_insert_here and ra[2] == rb[2]:
            out.extend(base[pos:ra[0]]); out.extend(ra[2])
            pos = ra[1]; ia += 1; ib += 1
        elif not both_insert_here and (
                rb is None or (ra is not None and ra[1] <= rb[0])):
            out.extend(base[pos:ra[0]]); out.extend(ra[2])
            pos = ra[1]; ia += 1
        elif not both_insert_here and (ra is None or rb[1] <= ra[0]):
            out.extend(base[pos:rb[0]]); out.extend(rb[2])
            pos = rb[1]; ib += 1
        else:                                   # overlapping change group
            s = min(ra[0], rb[0]); e = max(ra[1], rb[1])
            ga, gb = [ra], [rb]
            ia += 1; ib += 1
            grew = True
            while grew:
                grew = False
                while ia < len(ca) and ca[ia][0] < e:
                    e = max(e, ca[ia][1]); ga.append(ca[ia]); ia += 1; grew = True
                while ib < len(cb) and cb[ib][0] < e:
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

def _rebuild_text(lines, eol, trailing):
    out = eol.join(lines)
    if trailing and lines:
        out += eol
    return out

def text_form(repo, h):
    # (lines, eol, trailing newline) for a file that can be merged by line,
    # or None for one that cannot. CRLF and a missing final newline are kept
    # as properties of the file rather than treated as reasons to refuse;
    # only binary, invalid UTF-8 and mixed endings are out
    if h is None:
        return ([], "\n", True)
    data = repo.get(h)[1]
    if data == b"":
        return ([], "\n", True)
    if b"\0" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    crlf = text.count("\r\n")
    if crlf and text.count("\n") != crlf:
        return None                     # mixed endings: leave them alone
    eol = "\r\n" if crlf else "\n"
    norm = text.replace("\r\n", "\n")
    if "\r" in norm:
        return None                     # lone carriage returns
    trailing = norm.endswith("\n")
    lines = norm.split("\n")
    if trailing:
        lines.pop()
    # prove the rebuild is identical before relying on it
    if _rebuild_text(lines, eol, trailing).encode("utf-8") != data:
        return None
    return lines, eol, trailing

# === test gates ===
# scripts in sb-tests/<stage>/ are tracked files, so they travel with
# branches. the stages gate saves, merges and releases.
# they run in name order inside a clean temp checkout of the candidate tree,
# never the worktree, with SB_STAGE, SB_BRANCH, SB_COMMIT and SB_REPO set.
# a bad exit or a timeout blocks the operation; --no-verify overrides
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
    # Run one gate against the candidate tree. True = gate passes.
    with tempfile.TemporaryDirectory(prefix="sb-test-") as tmp:
        tmpdir = Path(tmp)
        _materialize(repo, files, tmpdir, from_worktree)
        # discover from the candidate tree, so a merge runs its own tests
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
# {name}: {stage} gate for sandbox (sb)
# runs inside a clean checkout of the candidate tree (cwd = checkout root)
# env: SB_STAGE, SB_BRANCH, SB_COMMIT, SB_REPO.  exit 0 passes
set -eu

echo "[{name}] checking $SB_BRANCH @ $SB_COMMIT"
# your checks here, for example:
# python3 -m py_compile $(find . -name '*.py' -not -path './sb-tests/*')
# ./run_unit_tests.sh
exit 0
"""

TEST_TEMPLATE_PY = """#!/usr/bin/env python3
# {name}: {stage} gate for sandbox (sb)
# runs inside a clean checkout of the candidate tree (cwd = checkout root)
# env: SB_STAGE, SB_BRANCH, SB_COMMIT, SB_REPO. exit 0 passes
import os, sys

print("[{name}] checking", os.environ["SB_BRANCH"], "@", os.environ["SB_COMMIT"])
# your checks here
sys.exit(0)
"""

# === commands ===
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
    redacted_only = redaction_only_paths(repo, tree, work, modified)
    modified = [p for p in modified if p not in redacted_only]
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
    if redacted_only:
        print(dim("redacted in history (the working file keeps the real "
                  "value):"))
        tree_print([cyan(p) for p in sorted(redacted_only)])
    if not (added or modified or deleted):
        leaf("working tree clean " + dim("— nothing to save"))
        return
    renames, added_r, deleted_r = detect_renames(work, tree, added,
                                                 deleted, repo)
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
    # --global-force: snapshot the whole worktree, everyone's edits with it.
    # the worktree is read once into stored blobs, and redaction, gates and
    # the commit all work from those, so what passed the gates is what gets
    # committed. one transaction, so an abort rolls the blobs back out
    with repo.transaction():
        disk = snapshot_worktree(repo, write=True)   # what's really on disk
        tree_files, head_c = head_tree_files(repo)
        added, modified, deleted = worktree_vs_tree(disk, tree_files)
        if not (added or modified or deleted) and head_c:
            print(green("nothing changed — no save created"))
            return
        # credentials become <REDACTED> in the blob being committed, leaving
        # the file on disk alone
        over, redacted, hard_blocked = _redact_for_commit(
            repo, disk, added + modified, args.allow_secrets)
        if hard_blocked:
            _report_hard_blocked(hard_blocked)
        work = dict(disk)                  # the candidate tree to commit
        work.update(over)
        # redaction can leave a change identical to what is already saved,
        # so recheck rather than create an empty save
        a2, m2, d2 = worktree_vs_tree(work, tree_files)
        if not (a2 or m2 or d2) and head_c:
            print(green("nothing new to save — the only changes were "
                        "redacted secrets already saved as <REDACTED>"))
            return
        # tests run the stored tree, so they see the bytes being committed
        if not args.no_verify:
            if not run_stage(repo, "pre-save", work, from_worktree=False):
                die("pre-save tests failed — save blocked "
                    "(--no-verify to override)")
        # refuse if the worktree moved while gates ran. compared against
        # disk, since redaction makes work differ on purpose
        recheck = snapshot_worktree(repo, write=False)
        if recheck != disk:
            drifted = sorted(set(disk) ^ set(recheck)) or \
                sorted(p for p in disk if disk.get(p) != recheck.get(p))
            die("the working tree changed while this save was being scanned "
                "and tested\n       (" + ", ".join(drifted[:5])
                + (" …" if len(drifted) > 5 else "")
                + ")\n       nothing was saved — run 'sb save' again")
        theirs_tip, mstate = finish_merge_parents(repo, work)
        tree_hash = build_tree(repo, work)
        parents = ([head_c["hash"]] if head_c else []) \
            + ([theirs_tip] if theirs_tip else [])
        h = make_commit(repo, tree_hash, parents, args.message)
        bypass = {"global_force": True}
        if mstate:
            bypass["merged"] = mstate["branch"]
        if args.no_verify:    bypass["skipped_tests"] = True
        if args.allow_secrets: bypass["skipped_secret_scan"] = True
        if redacted:
            bypass["secrets_redacted"] = sorted(rel for rel, _ in redacted)
        repo.update_ref(repo.current_branch(), h,
                        op="merge" if mstate else "save",
                        expect=head_c["hash"] if head_c else None,
                        extra=bypass)
        if mstate:
            repo.set_meta(MERGE_STATE, "")
        # everyone's edits are in the commit, so no lock is left to protect
        release_locks(repo, list(repo.locks().keys()))
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
    # the normal save: commit only this user's edits, leave everyone else's
    # alone in the commit, release this user's locks. a file locked by
    # someone else is never included, whatever is on disk.
    #
    # same contract as the --global-force path: read the worktree once into
    # stored blobs, run redaction and gates and the commit from those, and
    # recheck disk before committing so drift aborts the save
    name, email = author()
    with repo.transaction():
        # one transaction, so an abort rolls the blobs back out
        disk = snapshot_worktree(repo, write=True)   # what's really on disk
        tree_files, head_c = head_tree_files(repo)
        locks = repo.locks()
        mine = sorted(p for p, l in locks.items() if l["email"] == email)
        # include files I changed that somehow aren't locked
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
        # credentials become <REDACTED> in the blob being committed, leaving
        # the file on disk alone
        over, redacted, hard_blocked = _redact_for_commit(
            repo, disk, changed, args.allow_secrets)
        if hard_blocked:
            _report_hard_blocked(hard_blocked)
        work = dict(disk)                  # the candidate content to commit
        work.update(over)
        # redaction can leave a change identical to what is already saved,
        # so refilter rather than create an empty save
        changed = [p for p in changed if work.get(p) != tree_files.get(p)]
        if not changed:
            print(green("nothing new to save — the only changes were "
                        "redacted secrets already saved as <REDACTED>"))
            release_locks(repo, mine)
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
        theirs_tip, mstate = finish_merge_parents(repo, work)
        if mstate:
            bypass["merged"] = mstate["branch"]
        # refuse if any of these changed on disk after the snapshot.
        # disk_ref is that snapshot, since work may hold redacted blobs
        h = _commit_subset(repo, work, tree_files, head_c, changed,
                           args.message, owner=name, email=email,
                           op="merge" if mstate else "save",
                           extra=bypass or None,
                           verify_drift=True, disk_ref=disk,
                           extra_parents=[theirs_tip] if theirs_tip else ())
        release_locks(repo, changed)
        if mstate:
            repo.set_meta(MERGE_STATE, "")
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
        # what this save changed, relative to its first parent
        try:
            ptree = (read_tree(repo, parse_commit(repo, c["parents"][0])["tree"])
                     if c["parents"] else {})
            ctree = read_tree(repo, c["tree"])
            a, m, d = worktree_vs_tree(ctree, ptree)
            rn, a, d = detect_renames(ctree, ptree, a, d, repo)
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
    renames, added, deleted = detect_renames(work, tree, added,
                                             deleted, repo)
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
    # undo without destroying anything: a new save whose content equals the
    # previous one, so undo again redoes. with -p, bring back one path from
    # the last save instead, overwriting the working copy and saving nothing
    repo = need_repo()
    block_if_merging(repo, "undo")
    sync_locks(repo, quiet=True)
    keep = foreign_locks(repo)
    if args.path:                       # targeted: one path only
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
        # same symlink safe write checkout uses, so nothing at the target or
        # above it can redirect this
        root_fd = os.open(str(repo.root), os.O_RDONLY | _O_DIRECTORY)
        try:
            for m in matches:
                mode, h = tree[m]
                try:
                    pfd, fn = _safe_parent_fd(root_fd, m)
                except CheckoutConflict as e:
                    die(str(e))
                try:
                    _remove_at(pfd, fn)      # clear whatever is in the way
                    _materialize_entry(repo, pfd, fn, mode, h)
                finally:
                    os.close(pfd)
        finally:
            os.close(root_fd)
        release_locks(repo, matches)    # the edit is gone: so is its lock
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
    # replay the journal to entry seq, giving the tips as recorded then
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
    # resolve an anchor, hash prefix, release label or branch to a commit.
    # an anchor gives the current branch's tip as of that journal entry
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
        if len(w) >= 8:                    # anchors are 8 to 64 hex (sb prints 16)
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
    # a new save whose content equals a past state. like undo, but to any
    # point, and undo afterwards returns to where you were
    repo = need_repo()
    block_if_merging(repo, "restore")
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
    target_tree = read_tree(repo, c["tree"])     # every blob rehashed on the way out
    cur_tree, _ = head_tree_files(repo)
    _checkout_preserving(repo, target_tree, cur_tree, foreign_locks(repo))
    with repo.transaction():            # commit point: one transaction
        h = make_commit(repo, c["tree"], [head], f"restore: to {how}")
        repo.update_ref(repo.current_branch(), h, op="restore", expect=head)
    print(f"{bold('restored')} {dim('to')} {how} {dim('— created')} "
          f"{amber(short(h))}")
    leaf(dim("history preserved — nothing deleted; sb undo returns you"))

def _create_branch(repo, branch, head, allow_secrets=False):
    # create the branch and save the folder onto it at once, so a branch
    # always has content and can be switched to, tested and merged straight
    # away. files someone else has locked come from the last save, not from
    # disk, and credentials are redacted as in any save. a current branch
    # with no saves is seeded with the same commit, so both share a base
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
            # nothing saved yet, so give this branch the same first save
            repo.update_ref(cur, h, op="branch", expect=None,
                            extra={"initial": True, "seeded_from": branch})
            seeded = cur
    return h, len(work), redacted, seeded

def cmd_branch(args):
    repo = need_repo()
    if args.name and not args.remove:
        block_if_merging(repo, "branch")
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
    block_if_merging(repo, "switch")
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
    # if the folder already holds the target's tree there is nothing to lose
    # and nothing to write, so branch then switch needs no save between
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
    # lowest common ancestor: a common ancestor with no descendant that is
    # also one. taking the first shared commit would hand the merge a stale
    # base. with several candidates the pick is deterministic, by time then
    # hash, and an older base only ever means more conflicts, never a wrong
    # merge
    anc_a = {c["hash"] for c in walk_history(repo, a)}
    common = [c["hash"] for c in walk_history(repo, b) if c["hash"] in anc_a]
    if not common:
        return None
    common_set = set(common)
    # drop any common ancestor that is an ancestor of another one: not lowest
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

MERGE_STATE = "merge"       # meta key holding an unfinished merge
CONFLICT_MARK = "<<<<<<< ours"

def merge_state(repo):
    raw = repo.meta(MERGE_STATE)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None

def block_if_merging(repo, what):
    st = merge_state(repo)
    if st:
        die(f"a merge of '{st['branch']}' is still open — finish it with "
            f"'sb save \"<message>\"'\n       or drop it with 'sb merge --abort', "
            f"then {what}")

def _begin_conflicted_merge(repo, args, merged, marked, conflicts, ours_tree,
                            ours_tip, theirs_tip, preserve, skip):
    # put the merge in the worktree with markers in the conflicting files,
    # so they can be resolved where they are read. nothing is committed:
    # 'sb save' finishes the merge, 'sb merge --abort' drops it
    target = dict(merged)
    for rel, h in marked.items():
        mode = (merged.get(rel) or ours_tree.get(rel) or ("100644", h))[0]
        if is_link_mode(mode):
            continue          # markers cannot live inside a link target
        target[rel] = (mode, h)
    _checkout_preserving(repo, target, ours_tree, preserve)
    repo.set_meta(MERGE_STATE, json.dumps({
        "branch": args.branch, "ours": ours_tip, "theirs": theirs_tip,
        "conflicts": sorted(rel for rel, _ in conflicts),
        "marked": sorted(marked),
        "skip": sorted(skip)}))
    repo.journal("merge-open", {"branch": args.branch, "theirs": theirs_tip,
                                "conflicts": sorted(r for r, _ in conflicts)})
    print(red(f"merge of {args.branch}: {len(conflicts)} file(s) need you"))
    tree_print([red(rel) + dim(f"  ({why})") for rel, why in conflicts])
    marked_n = len(marked)
    if marked_n:
        leaf(dim(f"{marked_n} of them now hold ours/theirs markers in the "
                 "file itself"))
    unmarked = [r for r, _ in conflicts if r not in marked]
    if unmarked:
        leaf(dim("kept at your version (nothing to mark up): "
                 + ", ".join(unmarked[:4])))
    print(dim("edit them, then 'sb save \"<message>\"' to finish the merge\n"
              "or 'sb merge --abort' to put the folder back"))

def _abort_merge(repo):
    st = merge_state(repo)
    if not st:
        die("no merge is open")
    ours_tree = read_tree(repo, parse_commit(repo, st["ours"])["tree"])
    work = snapshot_worktree(repo, write=False)
    _checkout_preserving(repo, ours_tree, work, foreign_locks(repo))
    release_locks(repo, [p for p in st["conflicts"] if p in repo.locks()])
    repo.set_meta(MERGE_STATE, "")
    repo.journal("merge-abort", {"branch": st["branch"]})
    print(f"{bold('merge aborted')} {dim('· folder is back at')} "
          f"{amber(short(st['ours']))}")

def finish_merge_parents(repo, work):
    # a save while a merge is open completes it: the commit gets both
    # parents, and any file still holding markers stops the save
    st = merge_state(repo)
    if not st:
        return None, None
    left = []
    for rel in st["conflicts"]:
        h = (work.get(rel) or (None, None))[1]
        if h is None:
            continue
        try:
            data = repo.get(h)[1]
        except (KeyError, CorruptObject):
            continue
        if CONFLICT_MARK.encode() in data:
            left.append(rel)
    if left:
        die("conflict markers are still in " + ", ".join(left[:4])
            + (" …" if len(left) > 4 else "")
            + "\n       resolve them and save again, or 'sb merge --abort'")
    return st["theirs"], st

def _tree_renames(repo, base_tree, side_tree):
    # what one side renamed, relative to the base
    added = [p for p in side_tree if p not in base_tree]
    deleted = [p for p in base_tree if p not in side_tree]
    if not added or not deleted:
        return []
    ren, _, _ = detect_renames(side_tree, base_tree, added, deleted, repo)
    return ren

def _merge_entry(repo, b, o, t):
    # decide one path. gives (kind, entry, why, marked_blob) where kind is
    # take or conflict, and marked_blob holds the marked up text when the
    # conflict is one a person can resolve in the file itself
    if o == t:              return "take", o, "same", None
    if t == b:              return "take", o, "ours", None
    if o == b:              return "take", t, "theirs", None
    if o is None or t is None:
        return "conflict", None, "changed on one side, deleted on the other", None
    if o[0] != t[0]:
        return "conflict", None, "executable bit differs", None
    if is_link_mode(o[0]):
        # a link target is one path, not lines. merging it by line could
        # build a target that points nowhere, so differing targets are a
        # conflict, and there is nothing to mark up inside a link
        return "conflict", None, "both point somewhere different now", None
    bf = text_form(repo, b[1] if b else None)
    of = text_form(repo, o[1])
    tf = text_form(repo, t[1])
    if bf is None or of is None or tf is None:
        return "conflict", None, "binary: no line by line merge", None
    lines, n = merge3(bf[0], of[0], tf[0])
    data = _rebuild_text(lines, of[1], of[2]).encode("utf-8")
    if n:
        return ("conflict", None, f"{n} overlapping change(s)",
                repo.put("blob", data))
    return "take", (o[0], repo.put("blob", data)), "auto", None

def cmd_merge(args):
    repo = need_repo()
    if getattr(args, "abort", False):
        _abort_merge(repo)
        return
    block_if_merging(repo, "merge again")
    if not args.branch:
        die("which branch? usage: sb merge <branch> [--no-verify] [-i]")
    sync_locks(repo, quiet=True)
    theirs_tip = repo.resolve(args.branch)
    if theirs_tip is None:
        die(f"unknown branch '{args.branch}'")
    ours_tip = repo.head_commit()
    if not ours_tip:
        die("current branch has no saves")
    if theirs_tip == ours_tip:
        print(green("already up to date")); return
    # a merge that would change someone else's locked file is refused.
    # --ignore skips those: everything else merges, each skipped file keeps
    # our version, and its lock stays
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
    # locked files are preserved, never merged over, so they do not count as
    # unsaved work. the rest of the tree must be clean
    ensure_clean(repo, extra_exempt=skip)
    preserve = skip | foreign_locks(repo)
    base = find_merge_base(repo, ours_tip, theirs_tip)
    ours_tree = read_tree(repo, parse_commit(repo, ours_tip)["tree"])
    theirs_tree = read_tree(repo, parse_commit(repo, theirs_tip)["tree"])
    # a fast forward takes theirs whole, so only do it when nothing skipped
    # actually differs
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
    marked = {}                 # path to the blob holding conflict markers
    ours_ren = dict(_tree_renames(repo, base_tree, ours_tree))
    theirs_ren = dict(_tree_renames(repo, base_tree, theirs_tree))
    handled, follow = set(), []
    # A renamed file keeps its identity: the merge is done against the
    # content it had under its OLD name, then recorded under the new one.
    # Without this the new path has no entry in the base, so a three way
    # merge would run with an empty base and conflict on the whole file
    # even when the two sides edited different lines.
    for old, new in sorted(ours_ren.items()):
        theirs_new = theirs_ren.get(old)
        if theirs_new is not None:
            if theirs_new != new:
                conflicts.append((old, f"renamed to {new} here and to "
                                       f"{theirs_new} there"))
                handled |= {old, new, theirs_new}
            else:                     # same rename on both sides
                follow.append((new, base_tree.get(old), ours_tree.get(new),
                               theirs_tree.get(new), old))
                handled |= {old, new}
            continue
        if old in theirs_tree:            # they kept editing the old name
            follow.append((new, base_tree.get(old), ours_tree.get(new),
                           theirs_tree.get(old), old))
            handled |= {old, new}
        else:                             # they deleted what we renamed
            conflicts.append((new, f"renamed from {old} here, deleted there"))
            handled |= {old, new}
    for old, new in sorted(theirs_ren.items()):
        if old in ours_ren or old in handled:
            continue
        if old in ours_tree:
            follow.append((new, base_tree.get(old), ours_tree.get(old),
                           theirs_tree.get(new), old))
            handled |= {old, new}
        else:                             # we deleted what they renamed
            conflicts.append((new, f"renamed from {old} there, deleted here"))
            handled |= {old, new}
    for rel in sorted(set(base_tree) | set(ours_tree) | set(theirs_tree)):
        if rel in skip:
            merged[rel] = ours_tree.get(rel)   # keep our version, don't touch
            continue
        if rel in handled:
            continue
        kind, entry, why, mark = _merge_entry(
            repo, base_tree.get(rel), ours_tree.get(rel), theirs_tree.get(rel))
        if kind == "take":
            merged[rel] = entry
            if why == "auto":
                auto_merged.append(rel)
        else:
            conflicts.append((rel, why))
            merged[rel] = ours_tree.get(rel)
            if mark is not None:
                marked[rel] = mark
    for new, b, o, t, old in follow:      # a rename with an edit behind it
        kind, entry, why, mark = _merge_entry(repo, b, o, t)
        if kind == "take":
            merged[new] = entry
            auto_merged.append(f"{old} → {new}")
        else:
            conflicts.append((new, f"{why} (renamed from {old})"))
            merged[new] = o
            if mark is not None:
                marked[new] = mark
        merged.pop(old, None)
    merged = {k: v for k, v in merged.items() if v is not None}
    if conflicts:
        _begin_conflicted_merge(repo, args, merged, marked, conflicts,
                                ours_tree, ours_tip, theirs_tip, preserve, skip)
        sys.exit(2)
    if not args.no_verify:
        if not run_stage(repo, "pre-merge", merged,
                         commit=f"merge({short(ours_tip)},{short(theirs_tip)})"):
            die("pre-merge tests failed on the merged tree — merge blocked\n"
                "       (fix on a branch and re-merge, or --no-verify to override)")
    # write the merged tree, telling checkout the preserved paths are
    # already correct so it leaves them alone
    checkout_current = dict(ours_tree)
    checkout_target = dict(merged)
    for p in preserve:
        checkout_target[p] = ours_tree.get(p)
        checkout_current[p] = ours_tree.get(p)   # reads as unchanged, so it is skipped
    checkout_target = {k: v for k, v in checkout_target.items() if v is not None}
    checkout_current = {k: v for k, v in checkout_current.items() if v is not None}
    _checkout_preserving(repo, checkout_target, checkout_current, preserve)
    with repo.transaction():            # commit point: one transaction
        tree_hash = build_tree(repo, merged)
        # a merge that skipped files did not take all of theirs, so naming
        # their tip as a parent would claim ancestry we do not have and a
        # later merge would call the branch already merged. partial merges
        # get one parent, so rerunning later picks up what was skipped
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
    block_if_merging(repo, "publish")
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
    # record the gate scripts by content hash, so the release says what ran
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
    # full store verification. problems are tagged by category, so the
    # summary never has to guess
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
            entries = json.loads(repo.get(th)[1] or b"[]")   # get() rehashes
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
                    repo.verify_object(h)
                except KeyError:
                    flag("object", f"missing blob {short(h)} ({name})")
                except CorruptObject as e:
                    flag("object", f"{e} ({name})")

    # 1. every object reachable from every branch, rehashed
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

    # 1b. history from removed branches is kept, so it is checked too
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

    # 1c. the rest: orphans from interrupted work and content held by locks,
    #     so that no stored object goes unchecked
    for h, kind in repo.db.execute("SELECT hash, kind FROM objects").fetchall():
        if h in seen_commits or h in seen_trees or h in seen_blobs:
            continue
        objects += 1
        if kind == "tree":
            check_tree(h)
            continue
        try:
            repo.verify_object(h)
        except CorruptObject as e:
            flag("object", str(e))

    # 2. the journal hash chain, end to end
    chain_ok, head_link, n_entries = True, None, 0
    try:
        n_entries, head_link = repo.verify_journal()
    except TamperedJournal as e:
        chain_ok = False
        flag("journal", str(e))

    # 3. tips must match what the journal last recorded, which catches a ref
    #    edited outside sb. a tampered row is a finding here, not a crash
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
            # a ref the journal never recorded was injected outside sb.
            # an empty one is just a branch with no saves
            flag("refs", f"branch '{b}' ({short(cur)}) exists in refs but "
                         f"was never recorded in the journal "
                         f"(added outside sb?)")
    for b in expected:
        if b not in repo.branches():
            flag("refs", f"branch '{b}' exists in the journal but was "
                         f"removed from refs outside sb")

    # 4. optional anchor: is this prefix a link in the chain? 16 hex chars
    #    is 64 bits, short enough to write down, too long to collide with
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
            release_locks(repo, freed)
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
    # write any stored content back out to a file. mainly the other half of
    # a lock revert, which stores the bytes it displaces and prints the hash
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
    data = repo.get(full)[1]                    # rehashed on read
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
        # written under sudo it would be a root owned file in someone's
        # home, which they could no longer update
        su = _sudo_user()
        if su and not os.environ.get("SB_HOME"):
            for target in (CONFIG_FILE, CONFIG_DIR):
                with contextlib.suppress(OSError):
                    os.chown(target, su[0], su[1])
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

# === portable archive ===
# 'sb pack' seals the database and a small manifest into one encrypted
# .sbox file, 'sb unpack' reverses it. encryption comes from vox, embedded
# below, so both work offline
SBOX_MAGIC = b"SBOX"
SBOX_VERSION = 2      # v2 mixes a random salt into the key
SBOX_SALT_LEN = 16

# sealing and opening run over a file a piece at a time, so a large archive
# never becomes one bytes object. the construction is vox's own: a SIV salt
# over the plaintext, an HMAC counter keystream, then a tag over the
# ciphertext. two passes over the file replace the two passes over memory
STREAM_BLOCK = 64                     # sha512 digest, the keystream block
STREAM_CHUNK = 1024 * 1024            # a multiple of STREAM_BLOCK

def _xor_bytes(a, b):
    n = len(a)
    return (int.from_bytes(a, "big") ^ int.from_bytes(b[:n], "big")).to_bytes(n, "big")

def _keystream(key, nonce, start_block, nbytes):
    import hmac
    out = bytearray()
    c = start_block
    while len(out) < nbytes:
        out += hmac.new(key, nonce + c.to_bytes(5, "big"),
                        hashlib.sha512).digest()
        c += 1
    return bytes(out[:nbytes])

def _iter_file(path, size=None):
    with open(path, "rb") as f:
        while True:
            c = f.read(STREAM_CHUNK)
            if not c:
                return
            yield c

def _sbox_seal_stream(vox, manifest, body_path, passphrase, out_path):
    # writes header + salt + ciphertext + tag, reading the body twice
    import hmac
    head = canonical(manifest)
    prefix = len(head).to_bytes(4, "big") + head       # the framed manifest
    salt_hdr = os.urandom(SBOX_SALT_LEN)
    header = SBOX_MAGIC + bytes([SBOX_VERSION]) + salt_hdr
    ctx = vox._get_context((salt_hdr.hex() + ":" + passphrase).encode())
    mac = hmac.new(ctx.mac_key, header + prefix, hashlib.sha512)
    for chunk in _iter_file(body_path):
        mac.update(chunk)
    siv = mac.digest()[:vox.SALT_LEN]
    tag = hmac.new(ctx.mac_key, siv + header, hashlib.sha512)
    tmp = out_path.with_name(f".{out_path.name}.{os.urandom(6).hex()}.sbtmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as out:
            out.write(header)
            out.write(siv)
            block = 0
            for chunk in _prefixed(prefix, _iter_file(body_path)):
                ks = _keystream(ctx.enc_key, siv, block, len(chunk))
                ct = _xor_bytes(chunk, ks)
                tag.update(ct)
                out.write(ct)
                block += (len(chunk) + STREAM_BLOCK - 1) // STREAM_BLOCK
            out.write(tag.digest())
            out.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, out_path)

def _prefixed(prefix, chunks):
    # the manifest rides in front of the body, block aligned so the
    # keystream counter stays simple
    buf = bytearray(prefix)
    for c in chunks:
        buf += c
        while len(buf) >= STREAM_CHUNK:
            yield bytes(buf[:STREAM_CHUNK])
            del buf[:STREAM_CHUNK]
    if buf:
        yield bytes(buf)

def _sbox_open_stream(vox, in_path, passphrase, out_path):
    # verifies the tag before writing a single plaintext byte, then decrypts
    # into out_path. gives the manifest; the body is what lands in the file
    import hmac
    size = in_path.stat().st_size
    with open(in_path, "rb") as f:
        head = f.read(5)
        if head[:4] != SBOX_MAGIC:
            raise ValueError("not an sbox archive")
        ver = head[4]
        if ver != SBOX_VERSION:
            raise ValueError(f"archive format {ver} is not supported")
        salt_hdr = f.read(SBOX_SALT_LEN)
        header = head + salt_hdr
        siv = f.read(vox.SALT_LEN)
        ct_start = len(header) + len(siv)
        ct_len = size - ct_start - vox.TAG_LEN
        if ct_len < 0:
            raise ValueError("archive is truncated")
        ctx = vox._get_context((salt_hdr.hex() + ":" + passphrase).encode())
        tag = hmac.new(ctx.mac_key, siv + header, hashlib.sha512)
        left = ct_len
        while left:
            c = f.read(min(STREAM_CHUNK, left))
            if not c:
                raise ValueError("archive is truncated")
            tag.update(c)
            left -= len(c)
        want = f.read(vox.TAG_LEN)
        if not hmac.compare_digest(tag.digest(), want):
            raise ValueError("authentication failed")
        # second pass: decrypt, peeling the manifest off the front
        f.seek(ct_start)
        mac = hmac.new(ctx.mac_key, header, hashlib.sha512)
        block, left, manifest, pending = 0, ct_len, None, bytearray()
        with open(out_path, "wb") as out:
            while left:
                c = f.read(min(STREAM_CHUNK, left))
                left -= len(c)
                pt = _xor_bytes(c, _keystream(ctx.enc_key, siv, block, len(c)))
                block += (len(c) + STREAM_BLOCK - 1) // STREAM_BLOCK
                mac.update(pt)
                if manifest is None:
                    pending += pt
                    if len(pending) < 4:
                        continue
                    hlen = int.from_bytes(pending[:4], "big")
                    if len(pending) < 4 + hlen:
                        continue
                    manifest = json.loads(bytes(pending[4:4 + hlen]))
                    out.write(bytes(pending[4 + hlen:]))
                    pending = bytearray()
                else:
                    out.write(pt)
            if manifest is None:
                raise ValueError("archive manifest is incomplete")
            out.flush()
    if mac.digest()[:vox.SALT_LEN] != siv:
        raise ValueError("authentication failed")   # SIV self check
    return manifest

# vox v1.7.3 (jts.gg/vox), embedded verbatim. loaded into memory only while
# pack, unpack or export runs. nothing else uses it
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
    # Load the embedded vox module in memory (no disk, no network).
    import types
    mod = types.ModuleType("vox")
    try:
        exec(compile(VOX_SOURCE, "vox.py", "exec"), mod.__dict__)
    except Exception as e:
        die(f"the embedded encryption module failed to load: {e}")
    if not (hasattr(mod, "encrypt") and hasattr(mod, "decrypt")):
        die("the embedded encryption module is missing encrypt/decrypt")
    return mod

def _snapshot_db_file(repo: Repo, into: Path) -> Path:
    # a consistent single file copy of the store, with the WAL folded in
    src = repo.vdir / DB_NAME
    tmp = into / "snap.db"
    con = sqlite3.connect(str(src))
    try:
        con.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        con.close()
    return tmp

def sha256_file(path) -> str:
    h = hashlib.sha256()
    for c in _iter_file(path):
        h.update(c)
    return h.hexdigest()

class _StreamReader(io.RawIOBase):
    # a read only file over an object's chunks, so tar never holds a whole
    # large file in memory
    def __init__(self, chunks):
        self._chunks, self._buf = iter(chunks), b""
    def readable(self):
        return True
    def readinto(self, b):
        while not self._buf:
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n

def _tar_tree_file(repo, tree, out: Path) -> Path:
    # the same tar as _tar_tree, written straight to disk
    import tarfile
    with tarfile.open(str(out), mode="w") as t:
        for rel in sorted(tree):
            mode, bh = tree[rel]
            info = tarfile.TarInfo(rel)
            info.mtime = int(time.time())
            if is_link_mode(mode):
                info.type = tarfile.SYMTYPE
                info.linkname = repo.get(bh)[1].decode("utf-8", "replace")
                info.size = 0
                t.addfile(info)
                continue
            info.size = repo.db.execute(
                "SELECT size FROM objects WHERE hash=?", (bh,)).fetchone()[0]
            info.mode = 0o755 if mode == "100755" else 0o644
            t.addfile(info, _StreamReader(repo.stream(bh)))
    return out

def _untar_open(t, dest: Path) -> int:
    # extract every member of an open tarfile into dest. names are checked
    # first, then written the same symlink safe way checkout writes. a file
    # is streamed member by member, so no single one is held whole
    dest.mkdir(parents=True, exist_ok=True)
    root_fd = os.open(str(dest), os.O_RDONLY | _O_DIRECTORY)
    n = 0
    try:
        for m in t:
            if not (m.isreg() or m.issym()):
                continue
            parts = Path(m.name).parts
            if (m.name.startswith("/")
                    or any(p in ("..", SB_DIR) for p in parts)
                    or not all(safe_name(p) for p in parts)):
                die(f"archive contains an unsafe path: {m.name!r} — refusing")
            rel = "/".join(parts)
            try:
                pfd, leaf = _safe_parent_fd(root_fd, rel)
            except CheckoutConflict as e:
                die(f"refusing to extract {rel!r}: {e}")
            try:
                if m.issym():
                    _remove_at(pfd, leaf)
                    _write_symlink_at(pfd, leaf, m.linkname)
                else:
                    _remove_at(pfd, leaf)
                    _write_file_at(pfd, leaf, _tar_pieces(t, m),
                                   (m.mode & 0o777) or 0o644)
            finally:
                os.close(pfd)
            n += 1
    finally:
        os.close(root_fd)
    return n

def _tar_pieces(t, m):
    f = t.extractfile(m)
    while True:
        c = f.read(1024 * 1024)
        if not c:
            return
        yield c

def _untar_files_from(path: Path, dest: Path) -> int:
    import tarfile
    with tarfile.open(str(path), mode="r") as t:
        return _untar_open(t, dest)

def _checkout_into(repo, tree, dest: Path) -> int:
    # write a saved tree's files into dest without building a tar at all,
    # streaming each large file straight from the store
    dest.mkdir(parents=True, exist_ok=True)
    root_fd = os.open(str(dest), os.O_RDONLY | _O_DIRECTORY)
    try:
        for rel in sorted(tree):
            mode, h = tree[rel]
            pfd, leaf = _safe_parent_fd(root_fd, rel)
            try:
                _remove_at(pfd, leaf)
                _materialize_entry(repo, pfd, leaf, mode, h)
            finally:
                os.close(pfd)
    finally:
        os.close(root_fd)
    return len(tree)

def _seal_archive(vox, repo, manifest_base, body_writer, key, out):
    # body_writer(stage_dir) gives the path to the body file. we hash it
    # streaming, fold hash and size into the manifest, then seal it into
    # `out` without body or ciphertext ever being held whole
    stage = Path(tempfile.mkdtemp(prefix="sb-seal-"))
    try:
        body_path = body_writer(stage)
        manifest = dict(manifest_base)
        manifest["db_sha256"] = sha256_file(body_path)
        manifest["db_size"] = body_path.stat().st_size
        _sbox_seal_stream(vox, manifest, body_path, key, out)
        return manifest
    finally:
        shutil.rmtree(stage, ignore_errors=True)

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
        body_writer = lambda stage: _tar_tree_file(repo, tree, stage / "body")
    else:
        payload_kind = "repo"
        body_writer = lambda stage: _snapshot_db_file(repo, stage)
    manifest_base = {
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
    }
    out = Path(out_name) if out_name else Path(f"{repo.root.name}.sbox")
    if out.suffix != ".sbox":
        out = out.with_name(out.name + ".sbox")
    if out.exists():
        die(f"{out} already exists — choose another name or remove it")
    manifest = _seal_archive(vox, repo, manifest_base, body_writer, key, out)
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
    with open(src, "rb") as f:
        head = f.read(5)
    if len(head) < 5 or head[:4] != SBOX_MAGIC:
        die(f"{src} is not a sandbox archive (bad magic)")
    ver = head[4]
    if ver != SBOX_VERSION:
        die(f"archive format {ver} is not what this sb reads "
            f"({SBOX_VERSION})")
    vox = load_vox()
    work = Path(tempfile.mkdtemp(prefix="sb-unpack-"))
    body_path = work / "body"
    try:
        manifest = _sbox_open_stream(vox, src, key, body_path)
    except ValueError:
        shutil.rmtree(work, ignore_errors=True)
        die("could not open the archive — wrong pass-key or the file was "
            "altered\n       (vox verifies authenticity before decrypting)")
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        die("could not open the archive — wrong pass-key or the file was "
            "altered\n       (vox verifies authenticity before decrypting)")
    if sha256_file(body_path) != manifest.get("db_sha256"):
        shutil.rmtree(work, ignore_errors=True)
        die("archive integrity check failed — the contents did not match "
            "their recorded hash")
    try:
        _do_unpack(args, src, manifest, body_path, dest_name)
    finally:
        shutil.rmtree(work, ignore_errors=True)

def _do_unpack(args, src, manifest, body_path, dest_name):
    kind = manifest.get("payload", "repo")
    dest = Path(dest_name) if dest_name else Path(manifest.get("repo_name", "sandbox"))
    if dest.exists() and not dest.is_dir():
        die(f"{dest} exists and is not a folder — choose another destination")
    # any destination with something in it counts as merging, so a folder of
    # loose files gets the same protection as a repository
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
        # the archive holds files and no history, so only files come out
        dest.mkdir(parents=True, exist_ok=True)
        n = _untar_files_from(body_path, dest)
        held = "files only — this archive carries no history"
    elif files_only:
        # full history, but only the files are wanted: restore the store in
        # a temp area, check out from there, keep no .sb
        stage = Path(tempfile.mkdtemp(prefix="sb-unpack-"))
        try:
            (stage / SB_DIR).mkdir()
            shutil.copyfile(body_path, stage / SB_DIR / DB_NAME)
            srepo = Repo(stage.resolve())
            tree, _ = head_tree_files(srepo)
            dest.mkdir(parents=True, exist_ok=True)
            n = _checkout_into(srepo, tree, dest)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        held = "files only — history was in the archive but not written"
    else:
        # full repository: stage the store and verify it end to end before a
        # byte reaches the destination, so a damaged archive cannot leave a
        # half installed repo
        stage = Path(tempfile.mkdtemp(prefix="sb-unpack-"))
        try:
            (stage / SB_DIR).mkdir()
            shutil.copyfile(body_path, stage / SB_DIR / DB_NAME)
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
            # drop stale WAL and SHM sidecars, so SQLite cannot pair old
            # pages with the new database
            for side in ("-wal", "-shm"):
                stale = db_path.with_name(DB_NAME + side)
                if stale.exists():
                    stale.unlink()
        fd = os.open(str(db_path),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW,
                     0o600)
        try:
            with open(body_path, "rb") as bf:
                while True:
                    c = bf.read(1024 * 1024)
                    if not c:
                        break
                    view = memoryview(c)
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
    # resolve a release label, branch or hash prefix to a commit, with a
    # readable note of how it matched
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
        manifest_base = {
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
        }
        out = Path(dest_name) if dest_name else Path(f"{repo.root.name}-{what}.sbox")
        if out.suffix != ".sbox":
            out = out.with_name(out.name + ".sbox")
        if out.exists():
            die(f"{out} already exists — choose another name or remove it")
        manifest = _seal_archive(
            vox, repo, manifest_base,
            lambda stage: _tar_tree_file(repo, tree, stage / "body"), key, out)
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
    n = _checkout_into(repo, tree, dest)
    print(f"{bold('exported')} {amber(str(dest))} {dim('·')} {dim(str(n) + ' file(s)')}")
    tree_print([
        f"version  {how} {dim('·')} {amber(short(commit_hash))}",
        dim("plain files, no .sb — the repository stays where it is"),
    ])

def _get_key(args, *, confirm=False):
    # the pass key: -k, then SB_PASSKEY, then a prompt. -k is convenient but
    # exposes the key to shell history and process listings
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
    # one parser for pack, unpack and export. options may appear anywhere on
    # the line, and the pass key has no positional form
    sp = SBParser(prog=f"sb {cmd}", add_help=False)
    sp.add_argument("params", nargs="*")
    sp.add_argument("-k", "--key", metavar="<passkey>")
    if cmd in ("pack", "unpack"):
        sp.add_argument("-f", "--files-only", action="store_true")
    if cmd == "unpack":
        sp.add_argument("-i", "--ignore", action="store_true")
    return sp
# === CLI ===
# one usage line per command, shown when its arguments do not parse
USAGES = {
    "sb":         "sb <command> [arguments]",
    "sb save":    'sb save "<message>" [--allow-secrets] [--no-verify]',
    "sb log":     "sb log [-n <count>]",
    "sb diff":    "sb diff [<path>]",
    "sb restore": "sb restore <anchor | save | release-label | branch>",
    "sb undo":    "sb undo [-p <path>]",
    "sb branch":  "sb branch [<name>] [-r]",
    "sb switch":  "sb switch <branch>",
    "sb merge":   "sb merge <branch> [--no-verify] [-i]  ·  sb merge --abort",
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
    # report an argument failure in sb's style: the cleaned message, that
    # command's usage line, and a pointer to the menu
    # asking for help is not an error: show the menu and exit 0
    if "unrecognized arguments" in message and \
            any(t in message.split() for t in ("-h", "--help")):
        print(HELP)
        sys.exit(0)
    # a mistyped command gets one short line, not a dump of every choice
    m = re.search(r"invalid choice: '([^']*)'", message)
    if m:
        die(f"'{m.group(1)}' is not an sb command\n"
            f"       see the full menu:  sb help")
    # everything else: tidied argparse message plus the right usage line
    message = message.replace(
        "the following arguments are required:", "missing:")
    lines = [f"{prog}: {message}"]
    if prog in USAGES:
        lines.append(f"       usage:  {USAGES[prog]}")
    lines.append("       see the full menu:  sb help")
    die("\n".join(lines))

class SBParser(argparse.ArgumentParser):
    # argparse reporting failures through _arg_error, not its usage dump

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
    # an option line, indented under its command so it never reads as one
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
{_opt('--no-verify', 'skip the pre-merge tests')}
{_opt('-i, --ignore', 'skip files locked by others')}
{_opt('--abort', 'a conflicted merge writes markers into the files; save to finish or --abort to drop it', cont=False)}

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
        # options may sit anywhere among positionals:
        # 'sb unpack backup.sbox -k KEY restored'
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
        mp = sub.add_parser("merge"); mp.add_argument("branch", nargs="?")
        mp.add_argument("--no-verify", action="store_true")
        mp.add_argument("--abort", action="store_true")
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
        # parse_known_args, so leftovers are blamed on the command that was
        # typed ('sb log: ...') rather than the bare 'sb' parser
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
        raise                            # handled by the guard at the bottom
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
