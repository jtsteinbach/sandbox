#!/usr/bin/env python3
# sandbox test suite
# an adversarial suite for sb.py, kept out of the tool itself so the tool
# stays one file. run it next to sb.py:
#
#   ./sb_test.py                 the fixed cases
#   ./sb_test.py --fuzz 50       plus random histories
#   ./sb_test.py --sb ../sb.py   test a copy somewhere else
#
# exits 0 when everything passes, 1 otherwise.

import argparse, importlib.util, os, sys
from pathlib import Path

def _find_sb(explicit=None):
    # next to this script by default, then the working directory
    names = ["sb.py", "sandbox.py", "sb"]
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            sys.exit(f"no such file: {p}")
        return p
    here = Path(__file__).resolve().parent
    for d in (here, Path.cwd()):
        for n in names:
            if (d / n).is_file():
                return (d / n).resolve()
    sys.exit("could not find sb.py next to this script — pass --sb <path>")

def _load(path: Path):
    # load the tool as a module so the tests can reach its internals, and
    # remember the path so they can also drive it as a command
    spec = importlib.util.spec_from_file_location("sb_under_test", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sb_under_test"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod

_ap = argparse.ArgumentParser(add_help=True, description="sandbox test suite")
_ap.add_argument("--fuzz", nargs="?", type=int, const=25, default=0,
                 help="also run N random histories (default 25)")
_ap.add_argument("--sb", default=None, help="path to sb.py")
_args = _ap.parse_args()

SB_PATH = str(_find_sb(_args.sb))
_sb = _load(Path(SB_PATH))

# the suite below is written against the tool's own names, so bring them in
globals().update({k: v for k, v in vars(_sb).items()
                  if not k.startswith("__")})

def _fuzz(trials):
    # The fixed cases below check known situations. This checks the property
    # itself against inputs nobody thought of: build a random tree, mutate it
    # in shape changing ways, branch, diverge, merge, restore, and export.
    # After every step the store must verify and the working tree must be
    # exactly what was put in. A failure prints its seed so it can be replayed.
    import random as _rnd
    ok = True
    for trial in range(trials):
        rng = _rnd.Random(trial)
        d = Path(tempfile.mkdtemp(prefix="sb-fuzz-"))
        try:
            bad = _fuzz_once(rng, d)
            if bad:
                ok = False
                print(f"  {red('FUZZ')}  seed {trial}: {bad}")
        except Exception as e:                  # a crash is also a failure
            ok = False
            print(f"  {red('FUZZ')}  seed {trial}: {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(d, ignore_errors=True)
    print(("  " + green("ok") + f"    fuzz: {trials} random histories held"
           if ok else "  " + red(f"fuzz: {trials} trials, failures above")))
    return ok

def _fuzz_blob(rng, n):
    kind = rng.randrange(5)
    if kind == 0: return b""
    if kind == 1: return bytes(rng.randrange(256) for _ in range(n))
    if kind == 2: return ("\r\n".join("l%d" % i for i in range(n // 4))).encode()
    if kind == 3: return ("\n".join("l%d" % i for i in range(n // 4))).encode()
    return ("\n".join("l%d" % i for i in range(n // 4))).encode() + b"\nno-eol"

def _fuzz_snap(root: Path):
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [x for x in dn if x != SB_DIR]
        for f in fn:
            q = Path(dp) / f
            rel = str(q.relative_to(root))
            out[rel] = (("L", os.readlink(q)) if q.is_symlink()
                        else ("F", hash_obj("blob", q.read_bytes())))
        for x in list(dn):
            q = Path(dp) / x
            if q.is_symlink():
                out[str(q.relative_to(root))] = ("L", os.readlink(q))
    return out

def _fuzz_run(d, *a):
    return subprocess.run([sys.executable, "/home/claude/sandbox.py", *a],
                          cwd=str(d), capture_output=True, text=True)

def _fuzz_once(rng, d: Path):
    names = ["a.txt", "sub/b.txt", "sub/deep/c.txt", "sp ace.txt", "shape",
             "sub"]
    _fuzz_run(d, "init")
    (d / "seed").write_text("seed")
    for i in range(rng.randrange(2, 6)):
        q = d / ("f%d" % i)
        q.write_bytes(_fuzz_blob(rng, rng.randrange(1, 400)))
        if rng.random() < 0.2:
            os.chmod(q, 0o755)
    if rng.random() < 0.4:
        os.symlink("seed", d / "lnk")
    _fuzz_run(d, "save", "base")
    if _fuzz_run(d, "verify").returncode != 0:
        return "verify failed after the first save"
    want = _fuzz_snap(d)
    _fuzz_run(d, "branch", "b"); _fuzz_run(d, "switch", "b")
    for q in list(d.glob("f*")):
        if rng.random() < 0.5:
            q.write_bytes(_fuzz_blob(rng, rng.randrange(1, 400)))
    _fuzz_run(d, "save", "theirs")
    _fuzz_run(d, "switch", "main")
    if _fuzz_snap(d) != want:
        return "switching away and back did not restore the tree"
    # shape changes: a path becomes a file, a directory, a link, or nothing
    for step in range(4):
        n = rng.choice(names); q = d / n
        try:
            if q.is_dir() and not q.is_symlink():
                shutil.rmtree(q)
            elif q.exists() or q.is_symlink():
                q.unlink()
            act = rng.randrange(4)
            if act == 0:
                q.parent.mkdir(parents=True, exist_ok=True)
                q.write_bytes(_fuzz_blob(rng, rng.randrange(1, 300)))
            elif act == 1:
                q.parent.mkdir(parents=True, exist_ok=True)
                q.mkdir(); (q / "inner").write_text("in")
            elif act == 2:
                q.parent.mkdir(parents=True, exist_ok=True)
                os.symlink("seed", q)
        except OSError:
            pass
        _fuzz_run(d, "save", "shape%d" % step)
        if _fuzz_run(d, "verify").returncode != 0:
            return f"verify failed after a shape change at {n}"
    if _fuzz_run(d, "merge", "b").returncode == 2:
        _fuzz_run(d, "merge", "--abort")
    if _fuzz_run(d, "verify").returncode != 0:
        return "verify failed after the merge"
    live = _fuzz_snap(d)
    ex = d.parent / (d.name + "-ex")
    try:
        if _fuzz_run(d, "export", "main", str(ex)).returncode == 0:
            for k, v in _fuzz_snap(ex).items():
                if k in live and live[k] != v:
                    return f"export does not match the working tree at {k}"
    finally:
        shutil.rmtree(ex, ignore_errors=True)
    return None

def run_suite(args):
    # adversarial self test: crash injection at the ref and journal
    # boundary, symlink escapes, files swapping with directories, mutation
    # during a gate, merge fidelity, racing saves, store verification,
    # archive salt, branch bootstrapping and the content lock model.
    # exits 0 when everything passes
    import shutil as _sh, threading as _th, io as _io, tarfile as _tar
    import sqlite3 as _sql, zlib as _zl
    SELF = "/home/claude/sandbox.py"
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
            arc = Path("arc.tar")
            with _tar.open(str(arc), mode="w") as t:
                info = _tar.TarInfo("sub/evil"); data = b"pwned"
                info.size = len(data); t.addfile(info, _io.BytesIO(data))
            blocked = False
            try: _untar_files_from(arc, dest)
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
    def merge_crlf_keeps_endings():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("seed").write_text("s"); run("save", "s")
            Path("w.txt").write_bytes(b"a\r\nb\r\nc\r\n"); run("save", "base")
            run("branch", "y"); run("switch", "y")
            Path("w.txt").write_bytes(b"a\r\nB\r\nc\r\n"); run("save", "y")
            run("switch", "main")
            Path("w.txt").write_bytes(b"A\r\nb\r\nc\r\n"); run("save", "m")
            run("merge", "y")
            check("merge: CRLF file merges and keeps its endings",
                  Path("w.txt").read_bytes() == b"A\r\nB\r\nc\r\n")
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def merge_binary_conflict_and_abort():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("seed").write_text("s"); run("save", "s")
            Path("bin").write_bytes(b"\x00\x01base"); run("save", "base")
            run("branch", "x"); run("switch", "x")
            Path("bin").write_bytes(b"\x00\x01theirs"); run("save", "x")
            run("switch", "main")
            Path("bin").write_bytes(b"\x00\x01ours"); run("save", "m")
            r = run("merge", "x")
            check("merge: binary conflict stops the merge",
                  r.returncode == 2)
            check("merge: an open merge blocks a switch",
                  run("switch", "x").returncode != 0)
            run("merge", "--abort")
            check("merge: abort puts the folder back",
                  Path("bin").read_bytes() == b"\x00\x01ours"
                  and run("switch", "x").returncode == 0)
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
        # a locked file is its holder's version. anyone can type into it,
        # but the next command puts it back and keeps their bytes
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
            # alice edits a.py, bob edits b.py, so two locks
            Path("a.py").write_text("alice"); as_user("Alice", "a@co", "status")
            Path("b.py").write_text("bob"); as_user("Bob", "b@co", "status")
            repo = Repo(Path(".").resolve())
            locks = repo.locks()
            check("shared: independent edits lock to their own users",
                  locks.get("a.py", {}).get("email") == "a@co"
                  and locks.get("b.py", {}).get("email") == "b@co")
            # bob saves, so only b.py lands and a.py stays at v1
            as_user("Bob", "b@co", "save", "bob edit")
            as_user("Lead", "l@co", "export", "main", str(xd / "chk1"))
            check("shared: save commits only your files",
                  (xd / "chk1/a.py").read_text() == "v1"
                  and (xd / "chk1/b.py").read_text() == "bob")
            check("shared: others' locks survive your save",
                  "a.py" in Repo(Path(".").resolve()).locks())
            # expiry saves the abandoned edit under alice, then reverts it,
            # so history and disk both return to v1 and sb restore gets it
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
            # merge --ignore skips a locked file and leaves its lock alone
            Path("c.py").write_text("base"); as_user("Lead", "l@co", "save", "c base")
            as_user("Lead", "l@co", "branch", "feat")
            as_user("Lead", "l@co", "switch", "feat")
            Path("c.py").write_text("feature"); as_user("Lead", "l@co", "save", "c feat")
            as_user("Lead", "l@co", "switch", "main")
            Path("c.py").write_text("carol edit"); as_user("Carol", "c@co", "status")
            r = as_user("Lead", "l@co", "merge", "feat")
            blocked = r.returncode != 0 and "locked" in (r.stdout + r.stderr).lower()
            # merge proceeds, c.py keeps our version, the lock survives
            r2 = as_user("Lead", "l@co", "merge", "feat", "--ignore")
            repo2 = Repo(Path(".").resolve())
            as_user("Lead", "l@co", "export", "main", str(xd / "chk3"))
            skipped_ok = (r2.returncode == 0
                          and (xd / "chk3/c.py").read_text() != "feature"
                          and "c.py" in repo2.locks())
            check("shared: merge blocked by lock; --ignore skips it, lock kept",
                  blocked and skipped_ok)
            # unlock --force releases carol's lock
            r3 = as_user("Lead", "l@co", "unlock", "c.py", "--force")
            check("shared: sb unlock --force releases others' lock",
                  r3.returncode == 0
                  and "c.py" not in Repo(Path(".").resolve()).locks())
        finally:
            os.environ.clear(); os.environ.update(env0)
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)
            _sh.rmtree(xd, ignore_errors=True)

    @case
    def symlinks_tracked():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("real.txt").write_text("hi")
            os.symlink("real.txt", "link.txt"); run("save", "with a link")
            repo = Repo(Path(".").resolve())
            tree, _ = head_tree_files(repo)
            check("symlink: stored with link mode",
                  tree.get("link.txt", ("", ""))[0] == SYMLINK_MODE)
            run("branch", "x"); run("switch", "x")
            os.unlink("link.txt"); Path("link.txt").write_text("now a file")
            run("save", "link becomes file"); run("switch", "main")
            check("symlink: restored as a link on switch back",
                  os.path.islink("link.txt")
                  and os.readlink("link.txt") == "real.txt")
            run("export", "main", str(d / "ex"))
            check("symlink: exported as a link",
                  os.path.islink(str(d / "ex" / "link.txt")))
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def symlink_escape_stays_safe():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("f").write_text("1"); run("save", "s")
            # a link that points outside the repo is content, not a door:
            # its target string is stored, and it is never followed on write
            os.symlink("/etc/passwd", "evil"); run("save", "link out")
            run("branch", "x"); run("switch", "x")
            os.unlink("evil"); run("save", "drop it"); run("switch", "main")
            check("symlink: absolute-target link round-trips as a link",
                  os.path.islink("evil")
                  and os.readlink("evil") == "/etc/passwd")
            check("symlink: repo file untouched by the link",
                  Path("f").read_text() == "1")
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def ignore_keeps_tracked_file():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("keep.log").write_text("data"); run("save", "one")
            run("ignore", "*.log")
            repo = Repo(Path(".").resolve())
            work = snapshot_worktree(repo, write=False)
            check("ignore: an already-tracked file is not dropped",
                  "keep.log" in work)
            Path("new.log").write_text("fresh")
            work2 = snapshot_worktree(repo, write=False)
            check("ignore: a new match is still ignored",
                  "new.log" not in work2)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def rename_with_edit():
        d, old = fresh(); os.chdir(d)
        try:
            run("init")
            Path("a.txt").write_text("alpha\nbeta\ngamma\ndelta\nepsilon\n")
            run("save", "base")
            os.rename("a.txt", "b.txt")
            Path("b.txt").write_text("alpha\nbeta\nGAMMA\ndelta\nepsilon\n")
            r = run("status")
            check("rename: moved-and-edited file reads as a rename",
                  "a.txt" in r.stdout and "b.txt" in r.stdout
                  and "\u2192" in r.stdout)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def rename_aware_merge():
        d, old = fresh(); os.chdir(d)
        try:
            run("init")
            Path("a.txt").write_text("one\ntwo\nthree\nfour\nfive\n")
            run("save", "base"); run("branch", "y"); run("switch", "y")
            os.rename("a.txt", "b.txt"); run("save", "rename")
            run("switch", "main")
            Path("a.txt").write_text("one\ntwo\nTHREE\nfour\nfive\n")
            run("save", "edit"); run("merge", "y")
            check("merge: their rename carries our edit to the new name",
                  Path("b.txt").exists() and not Path("a.txt").exists()
                  and "THREE" in Path("b.txt").read_text())
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def merge_conflict_in_worktree():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("f").write_text("one\ntwo\nthree\n")
            run("save", "base"); run("branch", "y"); run("switch", "y")
            Path("f").write_text("one\nTHEIRS\nthree\n"); run("save", "y")
            run("switch", "main")
            Path("f").write_text("one\nOURS\nthree\n"); run("save", "m")
            r = run("merge", "y")
            body = Path("f").read_text()
            check("merge: conflict markers land in the file",
                  r.returncode == 2 and CONFLICT_MARK in body)
            check("merge: a save is refused while markers remain",
                  run("save", "nope").returncode != 0)
            Path("f").write_text("one\nBOTH\nthree\n")
            check("merge: resolving and saving finishes the merge",
                  run("save", "resolved").returncode == 0)
            repo = Repo(Path(".").resolve())
            c = parse_commit(repo, repo.tip("main"))
            check("merge: the finishing save has two parents",
                  len(c["parents"]) == 2)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def merge_abort_restores():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("f").write_text("a\nb\nc\n")
            run("save", "base"); run("branch", "y"); run("switch", "y")
            Path("f").write_text("a\nY\nc\n"); run("save", "y")
            run("switch", "main"); Path("f").write_text("a\nM\nc\n")
            run("save", "m"); run("merge", "y")
            check("merge: a switch is blocked mid-merge",
                  run("switch", "y").returncode != 0)
            run("merge", "--abort")
            check("merge: abort puts our version back",
                  Path("f").read_text() == "a\nM\nc\n"
                  and run("switch", "y").returncode == 0)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def big_file_chunked():
        d, old = fresh(); os.chdir(d)
        try:
            run("init")
            blob = os.urandom(CHUNK_THRESHOLD + 3 * CHUNK_SIZE)
            Path("big.bin").write_bytes(blob); run("save", "big v1")
            db = Path(SB_DIR) / DB_NAME
            s1 = db.stat().st_size
            data = bytearray(blob); data[CHUNK_SIZE + 5] ^= 0xFF
            Path("big.bin").write_bytes(bytes(data)); run("save", "big v2")
            s2 = db.stat().st_size
            check("chunked: a one-chunk edit costs about one chunk",
                  (s2 - s1) < 3 * CHUNK_SIZE)
            repo = Repo(Path(".").resolve())
            tree, _ = head_tree_files(repo)
            check("chunked: content reassembles to the exact bytes",
                  repo.get(tree["big.bin"][1])[1] == bytes(data))
            check("chunked: verify accepts the chunked store",
                  run("verify").returncode == 0)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def streaming_archive_roundtrip():
        d, old = fresh(); os.chdir(d)
        try:
            run("init")
            Path("small.txt").write_text("hello")
            os.symlink("small.txt", "l")
            Path("big.bin").write_bytes(os.urandom(CHUNK_THRESHOLD + 1024))
            run("save", "one")
            run("pack", "-k", "pw")
            out = d / "out"
            r = run("unpack", str(next(d.glob("*.sbox"))), str(out), "-k", "pw")
            ok = (r.returncode == 0
                  and (out / "small.txt").read_text() == "hello"
                  and os.path.islink(str(out / "l"))
                  and (out / "big.bin").stat().st_size
                      == CHUNK_THRESHOLD + 1024)
            check("archive: streamed pack/unpack round-trips", ok)
            bad = run("unpack", str(next(d.glob("*.sbox"))),
                      str(d / "bad"), "-k", "WRONG")
            check("archive: a wrong pass-key is refused",
                  bad.returncode != 0)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def locked_file_read_only():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("f").write_text("v1")
            os.chmod("f", 0o664); run("save", "one")
            Path("f").write_text("edited"); run("status")
            bits = os.lstat("f").st_mode & 0o777
            check("lock: group and other write are dropped while held",
                  not (bits & 0o022) and (bits & 0o200))
            run("save", "two")
            check("lock: original permissions come back after save",
                  (os.lstat("f").st_mode & 0o777) == 0o664)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def redacted_file_is_not_dirty():
        d, old = fresh(); os.chdir(d)
        try:
            run("init"); Path("f").write_text("seed"); run("save", "seed")
            Path("cfg.py").write_text(
                "key = '" + "AKIA" + "IOSFODNN7EXAMPLE'\n")
            run("save", "with a key")
            # the working file keeps the real value, history holds <REDACTED>,
            # so it differs forever. that must not read as unsaved work or
            # every later command would be blocked by it
            r = run("status")
            check("redaction: a redacted file is not listed as modified",
                  "modified" not in r.stdout)
            check("redaction: it is reported as redacted instead",
                  "redacted in history" in r.stdout)
            check("redaction: it holds no lock",
                  "cfg.py" not in Repo(Path(".").resolve()).locks())
            run("branch", "z")
            check("redaction: switch is not blocked by it",
                  run("switch", "z").returncode == 0)
            check("redaction: the real value is still on disk",
                  "AKIA" in Path("cfg.py").read_text())
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def chunk_list_tampering():
        d, old = fresh(); os.chdir(d)
        try:
            run("init")
            payload = b"".join(bytes([i % 251]) * CHUNK_SIZE
                               for i in range(CHUNK_THRESHOLD // CHUNK_SIZE + 2))
            Path("big.bin").write_bytes(payload); run("save", "big")
            before = Path("big.bin").read_bytes()
            # every chunk still hashes correctly, but the LIST is reordered.
            # per chunk checks all pass, so only a hash of the reassembled
            # whole can catch this
            db = _sql.connect(str(Path(SB_DIR) / DB_NAME))
            h, blob = db.execute(
                "SELECT hash, data FROM objects WHERE kind='chunked'").fetchone()
            refs = json.loads(_zl.decompress(blob))
            refs[0], refs[1] = refs[1], refs[0]
            db.execute("UPDATE objects SET data=? WHERE hash=?",
                       (_zl.compress(canonical(refs)), h))
            db.commit(); db.close()
            Path("big.bin").unlink()
            r = run("undo", "-p", "big.bin")
            check("chunked: a reordered chunk list is refused on checkout",
                  r.returncode != 0)
            check("chunked: nothing is written when it is refused",
                  not Path("big.bin").exists())
            check("chunked: verify reports the tampering",
                  run("verify").returncode == 2)
            del before
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def symlink_merge_is_atomic():
        d, old = fresh(); os.chdir(d)
        try:
            run("init")
            for n in "abc":
                Path(n).write_text(n)
            os.symlink("a", "link"); run("save", "base")
            run("branch", "y"); run("switch", "y")
            os.unlink("link"); os.symlink("b", "link"); run("save", "theirs")
            run("switch", "main")
            os.unlink("link"); os.symlink("c", "link"); run("save", "ours")
            run("merge", "y")
            # a link target is one path: line merging it would build a
            # target with newlines and conflict markers inside
            t = os.readlink("link") if os.path.islink("link") else None
            check("symlink: differing targets conflict instead of merging",
                  t == "c")
            check("symlink: the link stays a single valid path",
                  t is not None and "\n" not in t and CONFLICT_MARK not in t)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def identical_rename_merges():
        d, old = fresh(); os.chdir(d)
        try:
            run("init")
            Path("old.txt").write_text("one\ntwo\nthree\nfour\nfive\nsix\n")
            run("save", "base"); run("branch", "y"); run("switch", "y")
            os.rename("old.txt", "new.txt")
            Path("new.txt").write_text("one\ntwo\nthree\nfour\nfive\nSIX\n")
            run("save", "their rename and edit"); run("switch", "main")
            os.rename("old.txt", "new.txt")
            Path("new.txt").write_text("ONE\ntwo\nthree\nfour\nfive\nsix\n")
            run("save", "our rename and edit")
            r = run("merge", "y")
            # both renamed the same file the same way, editing different
            # lines. merging against the OLD name's content is what makes
            # this clean; an empty base would conflict on the whole file
            body = Path("new.txt").read_text() if Path("new.txt").exists() else ""
            check("merge: an identical rename on both sides still merges",
                  r.returncode == 0 and body == "ONE\ntwo\nthree\nfour\nfive\nSIX\n")
            check("merge: the old name is gone afterwards",
                  not Path("old.txt").exists())
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def rename_survives_shifted_bytes():
        d, old = fresh(); os.chdir(d)
        try:
            run("init")
            import random as _rnd
            _rnd.seed(7)
            body = bytes(_rnd.randrange(256) for _ in range(120000))
            Path("data.bin").write_bytes(body); run("save", "base")
            # inserting near the front shifts every later byte. fixed size
            # blocks would all move and score zero; content defined pieces
            # keep their boundaries
            Path("moved.bin").write_bytes(body[:900] + b"XX" * 600 + body[900:])
            Path("data.bin").unlink()
            r = run("status")
            check("rename: shifted binary content is still one rename",
                  "renamed" in r.stdout and "moved.bin" in r.stdout)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    @case
    def commands_do_not_thrash_a_locked_file():
        d, old = fresh(); os.chdir(d)
        try:
            run("init")
            Path("big.bin").write_bytes(b"\0" + os.urandom(CHUNK_THRESHOLD))
            run("save", "base")
            with open("big.bin", "r+b") as f:
                f.seek(50); f.write(b"edit")
            run("status")                       # takes the lock
            c0 = os.lstat("big.bin").st_ctime_ns
            run("status"); run("status")
            # re-applying the same permission bits would bump ctime, which
            # invalidates the stat cache and re-reads the file every command
            check("lock: repeat commands do not disturb the file",
                  os.lstat("big.bin").st_ctime_ns == c0)
        finally:
            os.chdir(old); _sh.rmtree(d, ignore_errors=True)

    fuzz_trials = getattr(args, "fuzz", 0) or 0
    print(bold("sb selftest") + dim(f" · {len(cases)} cases"
                                    + (f" + {fuzz_trials} fuzz" if fuzz_trials
                                       else "")))
    for fn in cases:
        try:
            fn()
        except Exception as e:
            failed.append(fn.__name__)
            print("  " + red("FAIL") + f"  {fn.__name__}: "
                  f"{type(e).__name__}: {e}")
    fuzz_ok = _fuzz(fuzz_trials) if fuzz_trials else True
    print()
    if failed or not fuzz_ok:
        leaf(red(f"{len(passed)} passed, {len(failed)} failed"
                 + (": " + ", ".join(failed) if failed else "")
                 + ("" if fuzz_ok else " · fuzz found a failing seed")))
        sys.exit(1)
    leaf(green(f"all {len(passed)} checks passed"
               + (f" · {fuzz_trials} random histories held" if fuzz_trials
                  else "")))

if __name__ == "__main__":
    print(dim(f"testing {SB_PATH}"))
    run_suite(_args)
