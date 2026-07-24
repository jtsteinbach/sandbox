# sandbox (sb)

**Version 1.3** · [jts.gg/sandbox](https://jts.gg/sandbox)
**License** · [jts.gg/license](https://jts.gg/license)

Safe version control in one file. One command vocabulary you can learn in five minutes. No dependencies beyond Python 3.9+, no cryptography libraries, and no command that destroys saved history.

sandbox is not a git clone and does not use git's repository format. It keeps the two ideas git got right (content addressing and a Merkle DAG of snapshots) and drops the staging area, detached HEADs, destructive commands, and the loose-file repository layout.

New in 1.3:

- **Shared editing is always on.** There is no `sb shared on/off` any more and no single-user mode. A repository used by one person simply never sees another person's lock.
- **Locks protect content, not permission.** While you hold a lock, your bytes *are* the file: anyone can edit it, but their version is put back to yours on the next sb command, and their bytes are kept and recoverable with the new `sb salvage`.
- **Branches are born saved.** `sb branch <name>` takes an immediate first save (`"Initial branch creation"`), so a new branch can be switched to, tested, and merged straight away.
- **Secrets are redacted, not blocked.** A recognized credential is replaced with `<REDACTED>` in the committed blob; the file on disk is never touched, and the redaction is journaled.
- **Pass-keys can come from a prompt or `SB_PASSKEY`** instead of the command line.

---

## Table of contents

1. [Installation](#1-installation)
2. [Why sandbox exists](#2-why-sandbox-exists)
3. [Quickstart](#3-quickstart)
4. [Core concepts](#4-core-concepts)
5. [Command reference](#5-command-reference)
6. [Shared editing and locks](#6-shared-editing-and-locks)
7. [Test gates](#7-test-gates)
8. [Secrets and redaction](#8-secrets-and-redaction)
9. [Security model](#9-security-model)
10. [Anchors](#10-anchors)
11. [Portable archives (.sbox)](#11-portable-archives-sbox)
12. [The storage format](#12-the-storage-format)
13. [Ignoring files](#13-ignoring-files)
14. [Everyday workflows](#14-everyday-workflows)
15. [sandbox versus git](#15-sandbox-versus-git)
16. [Environment variables](#16-environment-variables)
17. [Exit codes](#17-exit-codes)
18. [FAQ](#18-faq)
19. [Troubleshooting](#19-troubleshooting)
20. [Known limitations and roadmap](#20-known-limitations-and-roadmap)

---

## 1. Installation

Requirements: Python 3.9+ (standard library only) on Linux, macOS, or WSL. sandbox's symlink-safe write paths use POSIX directory descriptors, so native Windows is not supported; use WSL there.

System-wide (requires sudo):

```bash
curl -sL install.jts.gg/sandbox | sudo bash
```

For your user only:

```bash
curl -sL install.jts.gg/sandbox | bash
```

Confirm it worked:

```bash
sb help
```

Manual install, if you prefer to inspect first:

```bash
mkdir -p ~/.local/bin
cp sb.py ~/.local/bin/sb
chmod +x ~/.local/bin/sb
```

If `~/.local/bin` is not on your PATH, add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile.

To upgrade, re-run the install command. sandbox refuses to open repositories created by a newer format than it understands, so upgrades are safe and downgrades fail with a clear message instead of corrupting anything. Repositories from earlier versions are migrated in place the first time 1.3 opens them: the lock table gains the columns the content-lock model needs, and any lock recorded before the upgrade adopts whatever is on disk as the content it protects.

`sb selftest` runs the built-in adversarial suite (37 checks across 19 cases: crash injection, symlink escapes, file/directory transitions, races, corruption, archive crypto, secret redaction, branch bootstrapping, and the content-lock protocol) whenever you want proof the installed copy behaves.

---

## 2. Why sandbox exists

Version control solves a real problem: change things without fear, and know exactly what happened later. Git solves it too, but behind an interface where `checkout` means four different things, a mistyped `reset --hard` erases an afternoon, and the repository itself is thousands of loose files that a power cut can leave half-written.

sandbox is built on three decisions.

First, safety is structural. No sandbox command discards saved history. `undo` creates new history that reverts the old. `switch` refuses to run over unsaved work. A lock revert stores the bytes it displaces. The store is one SQLite database, so every operation is a single atomic transaction; a crash mid-save leaves you exactly where you were.

Second, simple doesn't mean toy. There is no staging area (a save snapshots everything that isn't ignored), no detached HEAD, and no rebase. There are real branches, three-way merges with automatic conflict-free merging, rename detection, versioned test gates, release records, per-file content locks for teams, and a full-store integrity verifier.

Third, security claims are kept narrow and true. sandbox promises integrity, tamper evidence, and leak prevention. Section 9 states how each works, what it defends against, and what it doesn't. There are no keys and no signatures; everything rests on SHA-256 from the standard library, used for content addressing and hash chaining.

---

## 3. Quickstart

Daily loop, from init to a shipped release:

```bash
cd my-project
sb init                          # creates .sb/sandbox.db, branch "main"
sb who "Ada" "ada@example.com"   # how your saves are attributed (once, globally)

# work, then snapshot
sb status                        # what changed? (renames detected, locks shown)
sb diff                          # line-by-line changes
sb save "add login form"         # snapshot your changes

# experiment on a branch
sb branch idea                   # creates it AND saves this folder onto it
sb switch idea                   # refuses if you'd lose unsaved work
sb save "try the risky refactor"
sb switch main
sb merge idea                    # 3-way merge; non-overlapping edits merge themselves

# mistakes
sb undo                          # reverts the last save, as a NEW save
sb undo -p src/app.py            # bring one file back from the last save
sb restore 67b3dea8b260c12a      # return to any past anchor, save, or release

# ship it
sb publish v1.0                  # verify store + run pre-publish tests + journal the release
sb publish -l                    # release history
sb export v1.0 out/              # that exact release as plain files
sb export v1.0 -k "release-key"  # or as an encrypted .sbox artifact for a server

# check integrity
sb verify                        # re-hash every object, check the journal chain
sb journal                       # the tamper-evident log of everything sb ever did

# move or back up the whole repository
sb pack -k "a-strong-pass-key"                        # seal repo + history into an encrypted .sbox
sb unpack my-project.sbox -k "a-strong-pass-key"      # restore elsewhere (verified before install)
```

Locks (Section 6) need no setup: they are always on, and editing a file is what takes one.

---

## 4. Core concepts

### Saves

A save is a complete snapshot of every tracked file, with a message, an author, and a timestamp. There is no staging area and no partial commit: what you see in your working folder, minus ignored files, is what gets saved. This makes "the tests passed on this save" mean something, because the save is exactly the tree that was tested.

One refinement, which is what makes a shared folder workable: a save commits *your* files and leaves everyone else's in-progress edits exactly as they are, on disk and in the commit (Section 6). `sb save --global-force` sweeps in everyone's edits and says so in the journal.

Every save records the hash of its parent, so saves form a chain, and with merges a DAG. Changing any byte of any past save would change its hash and break every link after it.

### Branches

A branch is a named pointer to a save, and it is never just a name: creating one (`sb branch idea`) immediately saves the working folder onto it with the message `Initial branch creation`. A new branch therefore has content from the moment it exists and can be switched to, tested, exported, and merged straight away.

If the branch you are standing on has never been saved, it is seeded with that same first commit, so both branches share one common base and an immediate merge is meaningful rather than an error.

You are always on exactly one branch; there is no detached-HEAD state. Switching rewrites the working folder to match the branch's latest save, and refuses to run if you have unsaved changes — except when the folder already equals the target branch's tree, which is what lets `sb branch x` be followed straight away by `sb switch x`.

### Locks

Editing a file locks it to you. A lock records the exact content you are protecting: while you hold it, your version is the file, other people's edits to it are put back, and only you can commit it. Locks are always on, need no configuration, and cost nothing in a repository with one user. Section 6.

### The object store

Content lives in a content-addressed store: the key for each object is the SHA-256 hash of its content, so identical files are stored once regardless of how many saves contain them.

| kind | contents |
|---|---|
| `blob` | raw file bytes |
| `tree` | a directory listing: `[[mode, kind, hash, name], ...]` as canonical JSON |
| `commit` | `{tree, parents, author, email, time, message}` as canonical JSON |

Every object is re-hashed on every read, not just during `verify`. A damaged blob raises an error the moment anything touches it.

### The journal

The journal is an append-only log inside the store recording every operation: saves, merges, undos, restores, branch creation and removal, switches, releases, lock releases, lock reverts, expiry auto-saves, ignore rules, durability changes, identity registrations, and pack/export/unpack events. Each entry embeds the SHA-256 link of the previous entry, forming a hash chain rooted in a random repository ID chosen at `init`.

The consequences:

- Deleting or editing a journal entry breaks the chain at that point.
- Moving, deleting, or injecting a branch tip behind sandbox's back (direct SQL included) is caught by `sb verify`, which cross-checks refs against the journal.
- Replacing an object is caught by the content re-hash.

Anything unusual about a save is part of the record, and `sb journal` shows it inline: `· no-verify`, `· secrets-override`, `· global-force`, `· redacted`, `· secrets-present`, `· initial-save`, `· seeded`. Skipping a gate is allowed; hiding it is not.

### Renames

`status`, `diff`, and `log` detect renames by exact content: a deleted path and an added path with byte-identical content display as `renamed old → new` instead of a separate delete and add. Detection is exact-match only. A file that was moved and also edited shows as an add plus a delete, and empty files never pair.

### Test gates

Executable scripts under `sb-tests/pre-save/`, `sb-tests/pre-merge/`, and `sb-tests/pre-publish/` run before the corresponding operation and block it on failure. They run in a pristine temporary checkout of the exact candidate tree, never your working folder. Section 7.

---

## 5. Command reference

The grammar is consistent across commands:

- Positional arguments say what: a message, a path, a branch, a version.
- Options say how, and each routine option has a short and long form: `-k`/`--key`, `-f`/`--files-only`, `-i`/`--ignore`, `-n`/`--limit`, `-r`/`--remove`, `-a`/`--anchor`, `-l`/`--list`, `-p`/`--path`. Options may appear anywhere on the line.
- The safety overrides (`--allow-secrets`, `--no-verify`, `--global-force`, `--force`) deliberately have no short form. Bypassing a gate or taking someone's lock is typed out in full. `-i` is not in this class: skipping locked files in a merge, or unpacking into a non-empty folder, is routine once you've asked for it explicitly.
- Pass-keys are always `-k <passkey>` across `pack`, `unpack`, and `export`, or `SB_PASSKEY`, or an interactive prompt.
- Subactions are words: `sb test list`, `sb test new`, `sb publish list` (`-l` also works).
- Mistakes get a one-line explanation, the correct usage line for that command, and a pointer to `sb help`, not a parser dump.

`<angle brackets>` are required, `[square brackets]` optional. Commands work from anywhere inside the repository. Colors appear only when output is a terminal.

Every command that touches repository state first synchronizes locks (enforce, expire, acquire) — that is how a daemon-less tool keeps the shared folder consistent.

### `sb init`

Creates the repository: `.sb/sandbox.db`, branch `main`, and a journal seeded with a random repository ID. The database is created with `0600` permissions. Fails if a repository already exists here.

### `sb status [--deep]`

Current branch, latest save, active locks (who holds what, and `their version wins` for other people's), and every change relative to the last save: `renamed old → new`, `new`, `modified`, `deleted`. Files that belong to someone else's lock are marked `(theirs)` and are not counted in the changes your next save would commit. `--deep` bypasses the stat cache and re-hashes every file.

### `sb save "<message>" [--allow-secrets] [--no-verify] [--global-force]`

Snapshots your changed files as a new save. The message is required. In order:

1. Synchronize locks: put other people's locked files back to their versions, expire abandoned locks, and take locks on the files you have edited.
2. Work out your file set: the files you changed, excluding every file another person holds a lock on.
3. Redaction pass: recognized credentials in your added or modified files are replaced with `<REDACTED>` in the blob about to be committed (Section 8). The file on disk is never rewritten. `--allow-secrets` commits them verbatim instead. A file that contains a credential but is not clean UTF-8 cannot be rewritten faithfully, so it blocks the save rather than being mangled.
4. Pre-save test gates in a clean checkout of the candidate tree (Section 7). Failures block; `--no-verify` overrides.
5. Re-check that the worktree didn't change while being scanned and tested; refuse rather than commit an untested state.
6. Store blobs, build the tree, write the commit, move the branch tip, release your locks, and journal it, in one transaction. Bypasses and redactions are journaled with it.

`--global-force` snapshots the entire worktree, everyone's edits included, and releases every lock. It is journaled as `global-force`.

### `sb log [-n <count>]`

Save history, newest first: hash, date, author, message, a `(merge)` marker, and a change summary per save (`+2 new · ~1 modified · old.txt → new.txt`). `-n 5` limits output.

### `sb diff [<path>]`

Unified diff between the working folder and the last save. Renames show as one `@@ old → new  renamed (content identical)` line. Binary files show as a one-line size summary. With `<path>`, limits output to that file or folder.

### `sb undo [-p <path>]`

Reverts the latest save by creating a new save whose content equals the previous one. The undone save stays in the log and journal; running `sb undo` again redoes. Requires a clean working tree — other people's locked files are exempt from that check and are never written over.

With `-p <path>`, brings just that file or folder back from the last save, overwriting the working copy. No new save is created. These writes go through the same symlink-safe atomic machinery as checkout, so a symlink planted at the path or in a parent cannot redirect the write outside the repository.

### `sb restore <anchor | save | release-label | branch>`

Returns the current branch to any past state, as a new save. Nothing is rewound or deleted; `sb undo` afterward takes you straight back. Requires a clean working tree (again, other people's locked files excepted).

Targets: an anchor (8–64 hex characters of a journal chain link, resolving to the branch tip as the journal recorded it at that moment), a save-hash prefix from `sb log` (4+ characters), a release label (`sb restore rel-3`), or a branch name. Ambiguous targets are rejected with a list of what matched.

### `sb branch [<name>] [-r] [--allow-secrets]`

No argument: list branches. With a name: create it **and immediately save the working folder onto it** as `Initial branch creation`. With `-r`: delete the named branch's pointer (never the current branch, never the last one). Its saves stay in the store and journal, and `verify` keeps checking them.

The initial save behaves like any other save: credentials are redacted (`--allow-secrets` commits them verbatim), and files another person holds a lock on are taken from the last save rather than from disk, because their in-progress work is theirs to commit. If the current branch has no saves yet, it is seeded with the same commit.

### `sb switch <branch>`

Rewrites the working folder to the branch's latest save and updates the branch pointer. Refuses with unsaved changes, unless the folder already matches the target exactly. Files locked by other people are left alone and reported. Writes are atomic and symlink-safe; directories emptied by the switch are pruned.

### `sb merge <branch> [--no-verify] [-i]`

Three-way merge into the current branch, using the best common ancestor as the base (a true lowest-common-ancestor search, correct after prior merges; criss-cross histories resolve deterministically).

- If the current branch is an ancestor of the target, the tip fast-forwards (still gated by pre-merge tests).
- Files changed on one side take that side.
- A file that exists on only one side, and never existed in the base, is kept. Deletion happens only when the base had the file and one side removed it.
- Files changed on both sides get a line-level three-way merge; non-overlapping edits combine, overlapping edits conflict.
- On conflict, the merge stops before touching anything. Your working folder is unchanged, and each conflicting file is listed with the reason. Reconcile on one branch, save, merge again. There is no in-progress merge state.

The merge is conservative on purpose: adjacent-line edits, same-point insertions, CRLF files, binaries, and files without a trailing newline count as conflicts rather than being guessed at or rewritten.

Pre-merge gates run against the merged tree itself before it is committed.

A merge that would change a file someone else has locked is refused. With `-i` (`--ignore`), those files are skipped: the merge proceeds for everything else, each skipped file keeps your current version and its lock, and the result is recorded as a partial merge (a single-parent save, not a merge commit), so re-running the merge after the locks release brings in what was skipped. sandbox does not record ancestry it didn't actually merge.

### `sb test [<stage>]` / `sb test new <stage> <name>` / `sb test list` / `sb test guide`

Run gates manually (all stages or one of `pre-save`, `pre-merge`, `pre-publish`), scaffold a new script, list discovered tests, or print the built-in walkthrough.

### `sb publish [<label>]` / `sb publish list` / `--no-verify`

Records the current save as a release, behind two gates: full store verification (sandbox refuses to publish from a damaged store), then pre-publish tests on a clean checkout of the exact tree. Passing both writes a `publish` entry into the journal: what, from which branch, by whom, when, and the content hashes of the gate scripts that ran. `sb publish -l` lists releases and reports whether the chain protecting them still verifies.

A release is a record. To get its files back out, use `sb export`.

### `sb export <version> [<destination>] [-k <passkey>]`

Materializes any version (release label, branch name, or save-hash prefix) as plain files, with executable bits preserved and no `.sb` directory. The destination defaults to `<repo>-<version>/` and must be empty. Export is read-only against your repository, every blob is re-hash-verified on the way out, and the export is journaled.

With `-k <passkey>`, produces an encrypted files-only `.sbox` artifact instead, carrying the label, commit, and sealed-by metadata. Ship it and drop it with `sb unpack <file.sbox> /path -k <passkey>` (`-i` to redeploy over a previous drop).

### `sb verify [-a <hash>]`

Re-hashes every object in the store (including history from removed branches and orphans from interrupted operations), validates every tree entry name, recomputes the entire journal chain, and cross-checks every branch tip against the journal, catching refs that were moved, deleted, or injected outside sb. Malformed objects, unreadable journal rows, and unexpected refs are reported as findings, not crashes. `-a <hash>` additionally confirms a previously noted anchor (Section 10).

Exits `0` when everything agrees, `2` with a list of problems otherwise.

```
checked 18 objects across 5 save(s)
  ├─── content hashes  all valid ✓
  ├─── journal chain   11 entries linked ✓
  ├─── branch tips     match the journal ✓
  └─── anchor          8977ecba8bd79985  (save it · check later: sb verify -a <hash>)
history is intact ✓ — store, journal and refs all agree
```

### `sb journal [-n <count>]`

The operation log, with detail for every operation type: ref moves with old → new hashes and any bypass, redaction, or initial-save markers; releases; switches; branch removals; lock releases (with `· forced` when forced); lock reverts (with the paths put back and the hashes of the bytes kept); ignore rules; durability changes; identity registrations; and pack/export/unpack events. Ends by re-verifying the chain.

### `sb info`

One-screen overview: store location and size, current branch, object counts, journal length, current anchor, attribution.

### `sb who [<name>] [<email>]`

Shows or sets how saves are attributed, stored in `~/.config/sandbox/profile.json` (`SB_HOME` overrides the location; `SB_NAME`/`SB_EMAIL` override per command). Attribution, not authentication (Section 9).

### `sb durability [full|normal]`

Shows or sets crash durability. `full` (default): the newest committed transaction survives power loss. `normal`: faster, still crash-safe, but may lose the most recent commit on power loss. Journaled.

### `sb locks`

Every active lock: the path, who holds it, how long they have held it, when it expires, and a short hash of the content being protected (`· file removed` when the holder deleted it).

### `sb unlock [<path>...] [--force]`

Releases your locks (all, or the named paths). `--force` releases locks held by others; the journal records the paths, the prior owners, and that it was forced. Releasing a lock does not change any file — it only stops sandbox putting that file back.

### `sb salvage <hash> [<path>]`

Writes any stored content back out to a file. This is the other side of a lock revert: when your edit to someone else's locked file is put back, the bytes you wrote are stored, their hash is printed and journaled, and this command brings them back. The hash is 4–64 hex characters (sb prints 10) and must be unambiguous; the destination defaults to `salvaged-<hash>` and is never overwritten if it exists.

### `sb ignore <pattern>`

Appends a pattern to `.sbignore` (Section 13). Journaled.

### `sb pack [<output>] -k <passkey> [-f]`

Seals the whole repository into one encrypted `.sbox` (Section 11). Output defaults to `<foldername>.sbox`; an existing file is never overwritten. The archive is written through an exclusively-created randomized temp file and atomically renamed, so a pre-planted symlink can't redirect it. Warns about unsaved changes, since pack seals saved history. Journaled.

`-f` seals only the current save's files, no history.

### `sb unpack <file.sbox> [<destination>] -k <passkey> [-f] [-i]`

Restores an archive. A wrong pass-key or altered archive fails cleanly and writes nothing. For a full-repository archive, the store is restored into a private staging area and fully verified first (objects re-hashed, chain recomputed, refs cross-checked); only if everything agrees is it installed and checked out. A damaged archive is refused before anything lands in the destination:

```
unpacked my-project · 3 file(s)
  ├─── sealed by  Jordan <jt@noct.gg>  · 2026-07-14 08:35
  ├─── branch     main · anchor bd40a7878f681649
  └─── verified before install ✓ — store, journal and refs all agree
```

The destination must be fresh or empty. `-i` (`--ignore`) merges into a non-empty destination instead: matching paths are overwritten with the archive's version, everything else is kept. This is the redeploy flag. There is no per-file backup.

`-f` writes only the native files with no `.sb` directory. Files-only archives unpack this way automatically. The unpack is journaled in the installed repository.

### `sb version` / `sb selftest`

`sb version` (also `-V`) prints `sb 1.3 · jts.gg/sandbox`. `sb selftest` runs the built-in adversarial suite — 37 checks across 19 cases: atomic rollback, symlink and path escapes, file/directory transitions, mid-gate mutation, CRLF merge safety, add/add merges, merge base correctness, branch bootstrapping, concurrent-save races, compare-and-swap, corruption detection, archive crypto, the stat cache, secret redaction, and the content-lock protocol end to end.

---

## 6. Shared editing and locks

A whole team can work in one repository — one folder, one database — without clone/push/pull. This is not a mode and cannot be turned off. A repository with one user simply never sees a foreign lock, and everything below collapses to ordinary solo behavior.

### The rules

- **Editing a file locks it to you.** There is no daemon; locks are claimed the next time any sandbox command scans the tree, and attributed to the actual editor (below).
- **While you hold a lock, your bytes are the file.** Anyone may open and edit it, and nothing stops them typing. But on the next sandbox command their version is put back on disk to yours. Their rejected bytes are stored and named in the journal, so `sb salvage <hash> [<path>]` writes them out again: the revert refuses to let a second writer's copy become the version of record, and destroys nothing.
- **Only you can save it.** Everyone else's `sb save` skips your locked files entirely, whatever is on disk. `sb save --global-force` is the deliberate exception, and the journal says so.
- **Your own later edits move the lock forward.** Each command that notices you changed the file again records the new content as the protected one and restarts the expiry clock.
- **A lock ends** when you `sb save` it, when you put the file back the way it was saved (nothing left to protect), or after an hour of inactivity (`SB_LOCK_TTL`, default 3600 seconds).
- **Expiry loses nothing.** The abandoned edits are auto-saved as a commit in the *owner's* name, then auto-reverted — in history and on disk — so the shared tree returns to the state the owner found it in. The auto-save hash is printed and journaled: `sb restore <hash>` brings the work back.
- **Merges never clobber locked work.** A merge that would change a file someone else has locked is refused; `sb merge <branch> -i` proceeds around it as a recorded partial merge. Switches, restores, undos, and checkouts all leave locked files untouched, and `ensure_clean` doesn't count them as your unsaved changes.
- **`sb locks`** shows who holds what and what content they are protecting. `sb unlock` releases yours; `sb unlock --force` releases someone else's, journaled with the prior owner's name.

Because expiry auto-saves are *preservation* commits, they store the bytes verbatim rather than redacting them: redacting and then reverting the disk would destroy the only copy of a live credential. If such a snapshot contains recognizable secrets, sandbox says so loudly and flags `secrets-present` in the journal (Section 8).

### Who gets the lock

A file on disk carries no record of who edited it, so a lock found at scan time must not simply go to whoever ran the scan. If Alice edits and Bob runs `sb status`, Bob must not become the owner of Alice's edit. The signal sandbox uses is the file's owner uid: each teammate writes as their own OS account. sandbox keeps a uid → identity registry (every sandbox command anyone runs records which OS account maps to which `sb who` identity) and locks each discovered edit to the account that owns the file. Someone who has never run sandbox still gets correct locks under their system account name, and expired-lock auto-saves are committed under the real editor's name.

Where the signal doesn't exist, attribution falls back to the invoking user: deletions (nothing left to stat), Windows, uid-squashing network mounts, and one login shared by several sb identities. One editor-dependent detail: editors that save by write-temp-then-rename (most IDEs) make the editor the file's owner, so attribution is exact; an editor that truncates in place leaves the original creator as owner. New files always attribute correctly. Saving promptly closes the window either way.

### Where shared editing belongs

It is designed for one machine with multiple user accounts, or a directly attached shared disk. The store is SQLite in WAL mode, and SQLite's documentation is explicit that WAL does not work reliably over NFS or SMB mounts, because file locking on network filesystems is broken in ways no application can compensate for. If your shared drive is a network mount, treat multi-user use as unsupported there and move work with `.sbox` archives instead.

---

## 7. Test gates

Test gates turn "we should run the tests" into "the tests ran, or the operation didn't happen."

### How it works

Put executable scripts in these folders. They are ordinary tracked files, so they version, branch, and merge with your code:

```
sb-tests/
  pre-save/       runs before every save
  pre-merge/      runs before every merge (including fast-forwards)
  pre-publish/    runs before every release
```

Scripts run sorted by name (use prefixes: `10-lint.sh`, `20-unit.py`), each inside a pristine temporary checkout of the exact candidate tree:

- A pre-save gate sees your candidate files in a clean directory, so nothing ignored or untracked leaks in. It sees the exact tree your save will produce, including any redactions and excluding other people's locked files.
- A pre-merge gate sees the merged result, and discovers its scripts from the merged tree, so a merge that changes the tests runs the new tests.
- A pre-publish gate sees exactly the tree being released, and the release record carries the content hashes of the scripts that ran.

Each script gets these environment variables, with the checkout root as its working directory:

| variable | meaning |
|---|---|
| `SB_STAGE` | `pre-save`, `pre-merge`, or `pre-publish` |
| `SB_BRANCH` | the current branch |
| `SB_COMMIT` | the candidate save hash, or `(worktree)` |
| `SB_REPO` | absolute path to the real repository root |

Exit 0 passes. Anything else, or exceeding the timeout (default 120s per script, `SB_TEST_TIMEOUT`), blocks the operation with exit code `2` and prints the script's last 15 lines. `--no-verify` overrides; the bypass is written into the journal entry and shown by `sb journal`.

`.py` scripts run under the same Python as sandbox; executables run directly; anything else runs under `sh`.

### Writing a gate

```bash
sb test new pre-save 10-syntax        # scaffolds sb-tests/pre-save/10-syntax.sh
sb test new pre-merge 20-unit.py      # .py gets a Python template
```

A realistic pre-save gate:

```sh
#!/bin/sh
set -eu
# fail the save on any Python syntax error
python3 -m py_compile $(find . -name '*.py' -not -path './sb-tests/*')
```

Run gates manually anytime: `sb test` (all stages), `sb test pre-merge` (one), `sb test list`.

### Keeping gates usable

Keep pre-save gates fast (seconds: syntax, lint, quick unit tests) so saving stays frictionless, and put slow suites at pre-merge or pre-publish. A gate that takes ten minutes at pre-save trains everyone to type `--no-verify`, and a gate everyone overrides is worse than no gate.

---

## 8. Secrets and redaction

The most common irreversible mistake in version control is committing a credential; history is permanent, and rotating a leaked key is an incident. sandbox scans every file being added or modified, at save time, for:

- AWS access keys (`AKIA…` / `ASIA…`)
- Private key blocks (`-----BEGIN … PRIVATE KEY-----`, including RSA/EC/OpenSSH/DSA/PGP)
- GitHub tokens (`ghp_…`, `gho_…`, and friends)
- Slack tokens (`xoxb-…` etc.)
- Google API keys (`AIza…`)
- Stripe live keys (`sk_live_…`, `rk_live_…`)
- JWTs
- Generic assignments like `password = "…"` or `api_key: '…'` with long quoted values

### What happens when one is found

The credential is replaced with `<REDACTED>` in the blob that gets committed. **Your file on disk is never touched** — the code you are running still has its key, and the save still happens. sandbox prints what it redacted and where, and the journal entry for that save carries `redacted` plus the list of files, so the substitution is on the record rather than silent.

A private key is redacted as a whole block, from `-----BEGIN` through the matching `-----END` (or end of file for a truncated block), because the key material is the base64 body *below* the line the scanner flags.

Two cases don't redact:

- **`--allow-secrets`** commits the file verbatim, journaled as `secrets-override`. Use it for false positives.
- **A file that isn't clean UTF-8** cannot be rewritten without mangling its bytes, so a credential inside one blocks the save (exit `2`) instead. Remove the secret, `sb ignore` the file, or override.

Expired-lock auto-saves are the deliberate exception described in Section 6: they preserve bytes verbatim, warn loudly, and flag `secrets-present` in the journal.

Redaction is a seatbelt, not a substitute for keeping secrets out of tracked files. The durable fix is to move the credential to an environment variable or an ignored config file (`sb ignore .env`) so it stops recurring. Binary files and files over 1 MB are skipped, pattern matching catches known credential shapes rather than every secret, and only files touched by the current save are scanned.

---

## 9. Security model

sandbox makes three promises. Each comes with its mechanism, what it defends against, and what it doesn't.

### Promise 1 — Integrity: what you get back is what you put in

Mechanism: every object is stored under the SHA-256 of its content and re-hashed on every read. Every save embeds its tree hash and parent hashes, so each save transitively fixes the exact bytes of every file in it and every save before it. Every operation commits as one SQLite transaction (WAL mode, `synchronous=FULL` by default). Working-folder writes use exclusive randomized temp files, fsync, and atomic rename through no-follow parent directory descriptors; this applies to checkout, switch, merge, restore, `undo -p`, lock reverts, and `salvage`. A crash between the database and the working folder surfaces as ordinary unsaved changes in `status`.

Defends against: disk corruption, torn and partial writes, power loss mid-operation, truncated or bit-flipped objects, malformed objects that hash correctly but don't parse, crafted trees attempting path traversal on checkout, and symlinks attempting to redirect any write outside the repository.

Does not defend against: loss of the database file itself. Integrity detection is not a backup.

### Promise 2 — Tamper evidence: changes made behind sandbox's back are detectable

Mechanism: the hash-chained journal. Every operation appends an entry whose link is `SHA-256(canonical entry ‖ previous link)`, rooted in a random per-repository ID. `sb verify` recomputes the chain and cross-checks every branch tip against the journal's record, flagging tips that were moved, branches that were deleted, and refs that were injected. Gate bypasses and redactions are part of the chained record.

Defends against: editing or deleting journal entries; manipulating refs via direct database access; replacing objects; modification by anything that isn't sb.

Does not defend against: an attacker with write access to the database and knowledge of sandbox's format can rewrite the entire store into a new, internally consistent history. With no secret material anywhere, internal consistency is recomputable by anyone; this is inherent to keyless designs. Anchors (Section 10) close the gap: a chain-head value recorded outside the machine cannot be reproduced by any rewrite. What sandbox will not do is ship the appearance of cryptographic authenticity (signatures, badges) without the key management that would make it meaningful.

### Promise 3 — Leak prevention: credentials don't enter permanent history in the clear

Mechanism: the save-time redaction pass (Section 8), on by default, overridable only explicitly, both the redaction and the override journaled. Files that cannot be redacted faithfully block the save instead.

Defends against: accidental commits of recognizable credentials.

Does not defend against: unrecognizable secrets, secrets already in older saves, or secrets in binary and oversized files, which are skipped.

### What locks are and are not

Locks are a coordination mechanism, not access control. They stop a second writer's version from becoming the version of record — on disk and in history — and they make sure nothing typed is destroyed on the way. They do not stop anyone with write access from opening the file, and they do not survive someone bypassing sandbox entirely. `sb unlock --force` is available to everyone, and journaled.

### What sandbox does not claim

- Confidentiality of the store. The database is created `0600`; full-disk encryption is the right layer for confidentiality at rest. (`.sbox` archives are encrypted; that covers transport and cold storage.)
- Authentication. The author on a save is attribution for humans reading history. uid-based lock attribution improves accuracy; it is still not authentication.
- Access control. sandbox is a local tool; file permissions are the access control.

### Why the cryptography was removed

An earlier design signed every commit with Ed25519, with a hand-rolled pure-Python fallback. That fails security review on three grounds: hand-rolled signature code is where implementation vulnerabilities live; keys with no management story (generated silently, stored in a dotfile, never rotated or bound to anything a verifier could trust) prove only that some key signed something; and a claim users trust more than its mechanism deserves is worse than no claim. The same standard was applied to the embedded encryption module, which carried an unused "asymmetric" mode whose construction did not deliver public-key security; it was deleted rather than documented around. Every property the removed code was supposed to provide is covered by the re-hashing, the chain, the ref cross-check, and anchors.

---

## 10. Anchors

The one attack a keyless store cannot detect internally is a wholesale rewrite (Promise 2's stated limit). Anchors close it with a hash and a second location.

Every `sb verify` (and `sb info`) prints the current anchor, a 16-character prefix of the latest journal entry's chain link:

```
  └─── anchor          67b3dea8b260c12a  (save it · check later: sb verify -a <hash>)
```

Copy it anywhere off the machine: a note on your phone, a message to a colleague, a line in a logbook. Later:

```bash
sb verify -a 67b3dea8b260c12a
```

Sixteen hex characters is 64 bits: short enough to jot down, far too large for a forged journal to collide with. Any 8–64 hex prefix of a chain link is accepted.

If the anchor is a link in the current chain, everything up to that moment is exactly as it was when you noted it. If it isn't found, the journal on disk is not the journal you witnessed: history was replaced, and the rewrite can't touch what's in your notebook.

Anchors also work as bookmarks: `sb restore <anchor>` returns the branch's content to the state that anchor witnessed, as a new save.

---

## 11. Portable archives (.sbox)

`sb pack` seals the repository into one encrypted, self-describing `.sbox` file, safe to email, put in cloud storage, or archive for cold storage.

```bash
sb pack -k "my-strong-pass-key"                   # -> <foldername>.sbox
sb pack release.sbox -k "my-strong-pass-key"      # choose the output name
sb pack release.sbox                              # prompts for the pass-key instead
```

The pass-key comes from `-k`, else `SB_PASSKEY`, else an interactive prompt (with confirmation when sealing). `-k` is convenient but exposes the key to shell history and process listings; the prompt and the environment variable avoid that. `-f`/`--files-only` seals just the current save's files, without history.

To restore, on any machine with sandbox, fully offline:

```bash
sb unpack my-project.sbox -k "my-strong-pass-key"             # -> ./my-project/
```

For a full-repository archive, the store is restored into a staging area and fully verified first: objects re-hashed, journal chain recomputed, refs cross-checked. Only then is it installed. A damaged or tampered archive is refused before anything is written to the destination, and the output confirms it: `verified before install ✓`. All history survives (every save, branch, and the full journal chain), and the unpack itself is journaled in the installed repository.

Unpacking requires a fresh or empty destination; `-i`/`--ignore` merges into a non-empty folder deliberately (redeploys), overwriting matching paths and keeping everything else.

### What's inside a .sbox

Every archive carries an encrypted manifest alongside the store:

| Field | Meaning |
|---|---|
| `created` / `created_by` | when the archive was sealed, and by whom |
| `repo_name` | the original folder name (the default unpack destination) |
| `branch` | the branch current at pack time |
| `chain_head` | the journal chain head at pack time; usable as an [anchor](#10-anchors) |
| `sb_version` / `sbox_version` / `repo_id` | versions and the repository's stable ID |
| `db_sha256` / `db_size` | integrity check for the sealed payload, verified before anything is written |

The manifest lives inside the encrypted blob, so an archive reveals nothing (author, branch, file names) without the pass-key. The only cleartext is the small header (`SBOX`, a format byte, and a random 16-byte per-archive salt), and it is bound to the ciphertext as authenticated data, so it can't be altered or swapped undetected.

### The encryption: vox

sandbox's integrity model uses no cryptographic keys (Section 9); nothing in save, merge, verify, the journal, or anchors depends on a secret. Archive confidentiality is a separate concern, handled by [vox](https://jts.gg/vox) (v1.7.3, symmetric core), a small single-file encryption module embedded inside sandbox and loaded into memory only while `pack`, `unpack`, or `export -k` runs. No separate file, no install step, no network. The unused legacy asymmetric interface is not present in the embedded copy.

vox is a misuse-resistant authenticated cipher: an SIV-style AEAD built on HMAC-SHA512, with PBKDF2-HMAC-SHA512 key stretching at 300,000 iterations. Since sbox format v2, a random per-archive salt is mixed into key derivation, so two archives sealed with the same passphrase use different keys and a password guess can't be amortized across archives. Two practical consequences:

- A wrong pass-key or a single altered byte means the archive will not open. vox verifies authenticity before decrypting, and sandbox re-hashes the recovered payload against `db_sha256`; full-repo archives additionally go through the staged verification battery.
- The pass-key is the only thing standing between the archive and its contents. There is no recovery and no key file. A weak pass-key is a weak archive.

Archives are written whole and held in memory while sealing and opening: comfortable to a few GB, not a streaming format (Section 20).

---

## 12. The storage format

The entire repository is one SQLite database: `.sb/sandbox.db`, in WAL mode, created `0600`, with `synchronous=FULL` by default (`sb durability normal` trades the newest-commit-on-power-loss guarantee for speed; the change is journaled).

- Crash safety: every operation commits as a single ACID transaction. (Fossil, the VCS written by SQLite's author, made the same bet fifteen years ago.)
- No small-file sprawl: the repo is one file, so `cp` is a valid backup and `rsync` sees one changed file.
- Real queries: prefix resolution is an indexed `LIKE`; the stat cache and locks are ordinary tables.
- Inspectable with standard database tooling, and anything changed with that tooling behind sandbox's back is flagged by `verify`.

### Schema

| table | contents |
|---|---|
| `meta` | key/value: `format` version, random `repo_id` (chain root), current `branch`, settings, uid → identity registry |
| `objects` | `hash → kind, size, zlib(data)`: the content-addressed store |
| `refs` | `name → commit hash`: branch tips (empty string = branch with no saves) |
| `journal` | `seq, ts, op, detail(JSON), prev, link`: the append-only hash chain |
| `statcache` | `path → size, mtime, ctime, inode, hash`: change detection without re-reading |
| `locks` | `path → owner, email, since, base, held, mode, uid`: per-file content locks |

In `locks`, `held` is the hash of the content the holder is protecting (or a `deleted` marker when they removed the file), `mode` is the file mode to restore it with, and `uid` is the OS account the holder writes as. A lock recorded by an older version has an empty `held` and adopts whatever is on disk the first time 1.3 sees it.

### Object encodings

An object's hash is `SHA-256("<kind> <length>\0" + data)`. Trees and commits are canonical JSON (sorted keys, no whitespace). A tree is `[[mode, kind, hash, name], …]` sorted by name; a commit is `{tree, parents, author, email, time, message}`. Modes are `100644` (file), `100755` (executable), `040000` (directory).

### The stat cache

`status` and `save` detect changes by comparing each file's size, mtime, ctime, and inode against the cache; on a full match the previous hash is reused and the file isn't read. mtime alone can be restored from userspace (`touch -d`, archive extraction), but ctime is kernel-maintained and the inode changes when an editor replaces a file, so a same-size edit with a restored mtime still misses the cache and gets re-read. Files touched within the last two seconds always bypass the cache; during a save a cached hash is only trusted if the blob exists in the store; `--deep` bypasses the cache entirely. A miss only costs a re-read; the cache fails toward correctness.

### What write paths guarantee

Tree entry names are validated on read (no `/`, `\`, NUL, `.`, `..`, empty, or `.sb`), so a hostile tree can't write outside the repository. Every worktree write (checkout, switch, merge, restore, `undo -p`, lock reverts, archive extraction) goes through a parent directory opened with no-follow semantics, an exclusively-created randomized temp file, a complete-write loop, fsync, and atomic rename. Archive outputs (`pack`, `export -k`) use the same discipline. Directory pruning never touches `.sb`.

---

## 13. Ignoring files

`.sbignore` in the repository root holds one glob pattern per line; `#` starts a comment. A pattern matches the full relative path, that path as a directory prefix, or any single path component:

```
# .sbignore
*.log
build
.env
node_modules
data/*.tmp
```

`sb ignore <pattern>` appends for you and journals it. Always ignored regardless of `.sbignore`: `.sb` itself, `*.sbox` archives, `.git`, `node_modules`, `__pycache__`, `*.pyc`, `.DS_Store`. `.sbignore` itself is tracked, so ignore rules travel with branches.

Two behaviors to know, the second being a real footgun: ignored files are invisible to `save`/`status` but never deleted by sandbox; and ignoring an already-tracked file makes it show as `deleted`, so the next save removes it from the tree (the file stays on disk but leaves history going forward). If you meant "stop tracking but keep the file," that's what happens. If you didn't, check `status` before saving.

---

## 14. Everyday workflows

**Solo project, straight line.** `sb init`, then work / `sb status` / `sb save` in a loop. Add a pre-save syntax gate (`sb test new pre-save 10-syntax`) on day one.

**Safe experiment.** `sb branch spike && sb switch spike` — the branch already holds your folder as its first save, so there is nothing to set up. Hack with saves as checkpoints. If it works: `sb switch main && sb merge spike`. If it doesn't: switch back and never merge.

**"I broke it ten minutes ago."** `sb diff` to see the damage; `sb undo -p <file>` to reclaim one file from the last save; `sb undo` to revert the whole last save.

**"I deleted everything."** Nothing saved is ever lost: `sb save "oops"` (yes, save the wipe), then `sb undo`. Both the deletion and its reversal live in history.

**"It worked last Tuesday."** Find last Tuesday (an anchor you noted, a save in `sb log`, or a release label) and `sb restore <it>`.

**Small team, one machine or one attached disk.** Everyone works in the same folder. Editing a file locks it to the actual editor; your version of a locked file stays put and anyone else's edit to it is put back (recoverable with `sb salvage`); `sb save` commits only your files; `sb locks` shows who holds what; abandoned locks auto-save in their owner's name after an hour and free themselves. Merges refuse to clobber locked work; `sb merge feat -i` proceeds around it as a recorded partial merge and completes on re-merge.

**"My edit got reverted."** The file belongs to someone else's lock. `sb journal` shows the `lock-revert` entry with the hash of what you wrote; `sb salvage <hash> mine.txt` gets it back, and you can hand it to the lock holder or apply it after they save.

**Release with a paper trail.** Keep the real suite in `sb-tests/pre-publish/`. Ship with `sb publish v1.4`: sandbox verifies the store, runs the suite against a clean checkout of exactly what's shipping, and journals the record, including the content hashes of the gate scripts that ran. `sb publish -l` is the release history.

**Deploy to a server.**

```bash
# on your machine
sb publish v1.4                    # gates + journaled record
sb export v1.4 -k "release-key"    # -> myapp-v1.4.sbox (encrypted, files only)
scp myapp-v1.4.sbox server:

# on the server: first deploy into a fresh folder
sb unpack myapp-v1.4.sbox /srv/www/myapp -k "release-key"

# each deploy after that merges over the previous drop
sb unpack myapp-v1.5.sbox /srv/www/myapp -k "release-key" -i
```

Rolling back is `sb export <older-label>` and the same `-i` drop.

**Compare against an old version locally.** `sb export rel-3 ./compare` materializes any past version next to your working copy without switching branches.

**Weekly trust check.** `sb verify`, copy the 16-character anchor next to the date somewhere off-machine. After that, no rewrite of any history before that moment can pass `sb verify -a <hash>`.

---

## 15. sandbox versus git

| | **sandbox** | **git** |
|---|---|---|
| Mental model | work → save | work → stage → commit (+ index states) |
| Staging area | none; a save is what you see | the index, with its own command set |
| Detached HEAD | impossible | routine source of confusion |
| Destroying history | no command does it | `reset --hard`, `push -f`, dropped stashes, expired reflog |
| Undo | `sb undo`, a new save, reversible | `revert` vs `reset` vs `restore` vs `checkout` |
| New branch | born with a save of your folder | a pointer; content comes later |
| Repository format | one crash-safe SQLite file | thousands of loose files + packfiles + refs + index |
| Operation audit log | hash-chained journal of every operation, bypasses included, cross-checked vs refs | reflog: per-machine, expiring, mutable, unchained |
| Tamper evidence | chain + tip cross-check + external anchors | commit DAG only; refs/reflog unprotected |
| Secret prevention | redacted at save time by default, journaled | third-party hooks you must install |
| Test enforcement | versioned gates on clean checkouts, on by default | hooks: unversioned, per-clone, easily absent |
| Renames | exact-content detection in status/diff/log | similarity-based detection, rename-aware merges |
| Merge conflicts | auto-merge non-overlap; on conflict, stops cleanly, worktree untouched | conflict markers + in-progress merge state |
| Small-team sharing | one repo, per-file content locks, always on | clone/push/pull, remotes |
| Remotes / distributed collaboration | not yet (roadmap) | git's core strength |
| Platform | Linux / macOS / WSL | everywhere |
| Ecosystem | one file, zero deps | vast |

Summary: git is a distributed collaboration system you can also use alone; sandbox is a safety-and-integrity system for individuals and small teams. If you need GitHub-style multi-party collaboration today, use git, possibly with sandbox alongside it (they coexist; sandbox ignores `.git`, add `.sb` to `.gitignore`).

---

## 16. Environment variables

| variable | effect | default |
|---|---|---|
| `SB_NAME` | attribution name for saves (overrides `sb who`) | profile, else OS username |
| `SB_EMAIL` | attribution email | profile, else `<name>@local` |
| `SB_HOME` | folder for the global profile | `~/.config/sandbox` |
| `SB_PASSKEY` | pass-key for `pack` / `unpack` / `export -k` when `-k` is absent | prompt |
| `SB_TEST_TIMEOUT` | seconds allowed per test script | `120` |
| `SB_LOCK_TTL` | seconds a lock survives without activity before auto-save + release | `3600` |

Inside test scripts, sandbox also exports `SB_STAGE`, `SB_BRANCH`, `SB_COMMIT`, `SB_REPO` (Section 7).

---

## 17. Exit codes

| code | meaning |
|---|---|
| `0` | success |
| `1` | usage or state error (not a repo, unsaved changes, unknown branch, bad arguments, non-empty unpack destination, corrupt object hit mid-operation, file-system error, …) |
| `2` | a gate stopped you: unredactable secrets, failed test gates, merge conflicts, or `verify` found problems |
| `130` | interrupted (Ctrl-C) |

The split is script-friendly: `2` means sandbox worked correctly and blocked something on purpose, so automation can tell "fix your command" from "fix your content."

---

## 18. FAQ

**Why didn't my secret block the save?**
Because 1.3 redacts instead of blocking. The credential is replaced with `<REDACTED>` in the committed blob, your file on disk is untouched, and the journal records which files were redacted. Only a file that can't be rewritten faithfully (not clean UTF-8) still blocks. Section 8.

**Someone else has a file locked and my edit keeps reverting. Where did my work go?**
Into the object store. The revert is journaled as `lock-revert` with the hash of exactly what you wrote; `sb salvage <hash> [<path>]` writes it back out to any filename you like. Nothing you typed is destroyed — sandbox just refuses to let a second writer's copy become the version of record.

**Can I turn locking off?**
No. Shared operation is structural in 1.3, not a setting. In a repository with one user it never does anything visible: you are always your own lock holder, and your own edits move your locks forward.

**Why does `sb branch` create a save?**
So a branch is never an empty name. It can be switched to, tested, exported, and merged the moment it exists, and there is no "save something first" step before a merge. If the branch you were on had no saves, it is seeded with the same commit so the two share a base.

**I merged a branch that doesn't have my file, and the file survived. Bug?**
Intended. In a three-way merge a file that only your side has never existed in the base, which reads as "only we changed it," so it is kept. Deleting it would mean inventing a deletion the other branch never made. A file is removed only when it existed in the base and the other side deleted it.

**Where did the signatures go? Is sandbox less secure now?**
The Ed25519 signing was removed deliberately; Section 9 has the full reasoning. Keys with no management story prove nothing, and a hand-rolled fallback implementation is a liability. The properties the signatures actually provided (integrity, tamper evidence) are covered by content re-hashing, the journal chain, the ref cross-check, and anchors.

**Is SHA-256 "cryptography"? I asked for none.**
It's a hash function from Python's standard library used as a content fingerprint. No keys, no signatures, no third-party crypto code. Content addressing, integrity checking, and the journal are all built on it.

**How do I back up a repository?**
Copy `.sb/sandbox.db` (any time sandbox isn't mid-command; WAL makes even that forgiving), copy the whole project folder, or `sb pack` for an encrypted single-file backup. After restoring, run `sb verify`.

**Can I have partial commits, like `git add -p`?**
No, by design: a save is exactly your working tree, which is what makes "the tests passed on this save" meaningful. Two unrelated changes belong on two branches or in two saves. The your-files-only save is the one exception, and it exists to protect other people's in-progress work.

**What about large or binary files?**
Stored (zlib-compressed) and versioned like anything else; `diff` summarizes them in one line and the redaction pass skips them. Every version of a large file is kept in full; there is no delta compression yet, so frequently-changing large binaries grow the store quickly.

**Does rename detection catch a file I moved and edited?**
No. Detection is exact-content only, so a moved-and-edited file shows as an add plus a delete. Similarity-based detection is on the roadmap.

**Symlinks?**
Not tracked yet; skipped with a printed note. Symlinks pointing into the repo also can't hijack sandbox's writes: every write path refuses to follow them.

**Can two commands run at once?**
Yes. SQLite serializes writers, and racing saves are protected by a compare-and-swap on the branch tip plus a worktree drift check. A race ends with one clean success and one "run it again" error, never corruption. Covered by `sb selftest`.

**Does anything leave my machine?**
No. There is no network code in sb.

**Can I rename a branch?**
Not built in yet. Today: `sb branch new-name && sb branch old-name -r` (from another branch). Removal is journaled and never deletes saves.

**Can `unpack -i` be undone?**
No. `-i` overwrites files in place with no per-file backup. That's why it's a flag: without it, unpack refuses any non-empty destination, so overwriting only happens when you asked for it.

**Who ends up owning a lock if Bob's `sb status` discovers Alice's edit?**
Alice. Locks are attributed to the file's owner on disk, resolved through the uid → identity registry, not to whoever ran sandbox first. Section 6 covers the edge cases.

---

## 19. Troubleshooting

**`error: not inside a sandbox repository`** — you're outside any folder containing `.sb/sandbox.db`. `cd` in, or `sb init`.

**`error: sb <command>: unrecognized arguments: …` / `missing: …`** — the flag or argument doesn't exist for that command; the correct usage line is printed below the error.

**`error: you have unsaved changes`** — `switch`, `merge`, `undo`, `restore`, and `publish` refuse to run over uncommitted work. `sb save "wip"` (saves are cheap, undo is free), or `sb undo -p <path>` for changes you want gone. Other people's locked files never trigger this.

**`reverted N file(s) to their lock holders' versions`** — you edited files someone else holds locks on. Their versions are back on disk; yours are stored, and the message and journal give you the hashes for `sb salvage`.

**`nothing of yours to save` / `N file(s) belong to other people's locks`** — everything you changed is locked by someone else, so there was nothing for your save to commit. Wait for them to save, or ask them to.

**`merge blocked — it would change files locked by others`** — protecting a teammate's in-progress edit. Wait, ask them to save, or `sb merge <branch> -i` to proceed around those files as a recorded partial merge.

**`save blocked — secrets in files that cannot be safely redacted (not clean UTF-8)`** — the file holds a credential but isn't text sandbox can rewrite without corrupting it. Remove the secret, `sb ignore` the file, or `--allow-secrets` (journaled). Section 8.

**`secrets redacted in the save (working files untouched)`** — not an error. The save happened; history holds `<REDACTED>` and your files are unchanged. Move the credential to an environment variable or an ignored file so it stops recurring.

**`error: <folder> is not empty — unpack into a fresh folder`** — unpack never writes into a destination that already contains anything. Pick a fresh folder, or add `-i` to overwrite matching files on purpose.

**`the archive's repository failed verification — nothing was written`** — the store inside the `.sbox` is damaged or was tampered with; unpack refused before touching the destination. Get a good copy of the archive.

**`pre-save tests failed — save blocked`** — the failing script's last 15 lines are printed above the error. Reproduce with `sb test pre-save`. Override once with `--no-verify` (journaled), then fix the gate.

**`merge stopped — these files conflict`** — the reason is printed per file; your worktree was not touched. Reconcile on one branch, save, merge again.

**`object … does not match its hash` / `verify` reports problems** — real corruption or tampering; sandbox stopped rather than propagating it. Restore `.sb/sandbox.db` from a backup, then `sb verify`. Undamaged files can be rescued first via `sb undo -p` or `sb export` from saves whose objects are intact.

**`branch '…' changed under this operation … run the command again`** — two sandbox commands raced; yours lost the compare-and-swap and nothing was changed. Re-run it.

**`store error: … database is locked`** — another sandbox command has the database open for writing. Wait a moment and retry.

**`file system error: …`** — a permission or disk problem outside sandbox's control, reported cleanly.

**A file isn't being saved** — it matches an ignore rule, or someone else holds a lock on it (`sb status` marks those `(theirs)`). Check `.sbignore` and the built-in defaults (Section 13). Symlinks are skipped with a printed note.

---

## 20. Known limitations and roadmap

- **POSIX only.** Linux, macOS, WSL. The symlink-safe write machinery relies on directory descriptors Windows doesn't provide; supporting Windows by weakening those guarantees isn't on the table, so it waits until it can be done properly. (Lock attribution also loses its uid signal there and falls back to the invoking user.)
- **No live remotes yet.** Repositories move between machines as encrypted `.sbox` archives, which works but is manual. Journal-first sync is the top roadmap item.
- **Shared editing wants a local disk.** SQLite WAL is not reliable over NFS/SMB mounts (Section 6). One machine or a directly attached disk is supported; for network mounts, use archives.
- **Locks are enforced by sandbox, not by the filesystem.** They are evaluated when a command runs, so a foreign edit lives on disk until the next command puts it back. Nothing is lost either way, but a locked file is not read-only to your editor.
- **No symlink tracking** (skipped with a note).
- **Whole-file storage.** zlib-compressed but not delta-compressed; heavy for large, frequently-changing binaries. Archives are also held in memory while sealing/opening: fine to a few GB, not streaming.
- **Rename detection is exact-content only.** Moved-and-edited files show as add + delete; merges are not rename-aware.
- **Conservative merges.** Adjacent-line edits and same-point insertions conflict rather than merge, and conflicts are resolved on a branch rather than via in-worktree conflict markers.
- **Redaction is pattern-based.** It catches known credential shapes in text files under 1 MB, and only in files the current save touches.
- **No branch rename, no per-save tags** yet.
- **`unpack -i` keeps no backup.**
- **Anchors are manual.** Automatic anchoring is on the roadmap.
- **Ignoring a tracked file drops it from the next save** (Section 13). Correct under the snapshot model, surprising if unread.

---

*sb — one file, no dependencies, nothing silently destroyed.*
