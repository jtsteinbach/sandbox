# sandbox (sb)

**Version 1.2** · [jts.gg/sandbox](https://jts.gg/sandbox)
**License** · [jts.gg/license](https://jts.gg/license)

Safe version control in one file. One command vocabulary you can learn in five minutes. No dependencies beyond Python 3.9+, no cryptography libraries, and no command that destroys saved history.

Sandbox is not a git clone and does not use git's repository format. It keeps the two ideas git got right (content addressing and a Merkle DAG of snapshots) and drops the staging area, detached HEADs, destructive commands, and the loose-file repository layout.

New in 1.2: shared mode (one repository, a whole team, per-file locks), rename detection, a journal that records every operation including gate bypasses, archives verified end to end before install, and hardened write paths throughout.

---

## Table of contents

1. [Installation](#1-installation)
2. [Why Sandbox exists](#2-why-sandbox-exists)
3. [Quickstart](#3-quickstart)
4. [Core concepts](#4-core-concepts)
5. [Command reference](#5-command-reference)
6. [Shared mode](#6-shared-mode)
7. [Test gates](#7-test-gates)
8. [The secret scanner](#8-the-secret-scanner)
9. [Security model](#9-security-model)
10. [Anchors](#10-anchors)
11. [Portable archives (.sbox)](#11-portable-archives-sbox)
12. [The storage format](#12-the-storage-format)
13. [Ignoring files](#13-ignoring-files)
14. [Everyday workflows](#14-everyday-workflows)
15. [Sandbox versus git](#15-sandbox-versus-git)
16. [Environment variables](#16-environment-variables)
17. [Exit codes](#17-exit-codes)
18. [FAQ](#18-faq)
19. [Troubleshooting](#19-troubleshooting)
20. [Known limitations and roadmap](#20-known-limitations-and-roadmap)

---

## 1. Installation

Requirements: Python 3.9+ (standard library only) on Linux, macOS, or WSL. Sandbox's symlink-safe write paths use POSIX directory descriptors, so native Windows is not supported; use WSL there.

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

To upgrade, re-run the install command. Sandbox refuses to open repositories created by a newer format than it understands, so upgrades are safe and downgrades fail with a clear message instead of corrupting anything. `sb selftest` runs the built-in test suite (20 checks: crash injection, symlink escapes, races, corruption, archive crypto, shared-mode locking) whenever you want proof the installed copy behaves.

---

## 2. Why Sandbox exists

Version control solves a real problem: change things without fear, and know exactly what happened later. Git solves it too, but behind an interface where `checkout` means four different things, a mistyped `reset --hard` erases an afternoon, and the repository itself is thousands of loose files that a power cut can leave half-written.

Sandbox is built on three decisions.

First, safety is structural. No Sandbox command discards saved history. `undo` creates new history that reverts the old. `switch` refuses to run over unsaved work. The store is one SQLite database, so every operation is a single atomic transaction; a crash mid-save leaves you exactly where you were.

Second, simple doesn't mean toy. There is no staging area (a save snapshots everything that isn't ignored), no detached HEAD, and no rebase. There are real branches, three-way merges with automatic conflict-free merging, rename detection, versioned test gates, release records, per-file team locking, and a full-store integrity verifier.

Third, security claims are kept narrow and true. Sandbox promises integrity, tamper evidence, and leak prevention. Section 9 states how each works, what it defends against, and what it doesn't. There are no keys and no signatures; everything rests on SHA-256 from the standard library, used for content addressing and hash chaining.

---

## 3. Quickstart

Daily loop, from init to a shipped release:

```bash
cd my-project
sb init                          # creates .sb/sandbox.db, branch "main"
sb who "Ada" "ada@example.com"   # how your saves are attributed (once, globally)

# work, then snapshot
sb status                        # what changed? (renames detected)
sb diff                          # line-by-line changes
sb save "add login form"         # snapshot everything

# experiment on a branch
sb branch idea
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

Shared mode (`sb shared on`), test gates, and anchors are covered below.

---

## 4. Core concepts

### Saves

A save is a complete snapshot of every tracked file, with a message, an author, and a timestamp. There is no staging area and no partial commit: what you see in your working folder, minus ignored files, is what gets saved. This makes "the tests passed on this save" mean something, because the save is exactly the tree that was tested. (Shared mode refines this: your save commits your files and leaves teammates' in-progress edits alone. Section 6.)

Every save records the hash of its parent, so saves form a chain, and with merges a DAG. Changing any byte of any past save would change its hash and break every link after it.

### Branches

A branch is a named pointer to a save. Creating one is instant (`sb branch idea`). You are always on exactly one branch; there is no detached-HEAD state. Switching rewrites the working folder to match the branch's latest save, and refuses to run if you have unsaved changes.

### The object store

Content lives in a content-addressed store: the key for each object is the SHA-256 hash of its content, so identical files are stored once regardless of how many saves contain them.

| kind | contents |
|---|---|
| `blob` | raw file bytes |
| `tree` | a directory listing: `[[mode, kind, hash, name], ...]` as canonical JSON |
| `commit` | `{tree, parents, author, email, time, message}` as canonical JSON |

Every object is re-hashed on every read, not just during `verify`. A damaged blob raises an error the moment anything touches it.

### The journal

The journal is an append-only log inside the store recording every operation: saves, merges, undos, restores, branch creation and removal, switches, releases, lock releases, ignore rules, shared/durability changes, identity registrations, and pack/export/unpack events. Each entry embeds the SHA-256 link of the previous entry, forming a hash chain rooted in a random repository ID chosen at `init`.

The consequences:

- Deleting or editing a journal entry breaks the chain at that point.
- Moving, deleting, or injecting a branch tip behind Sandbox's back (direct SQL included) is caught by `sb verify`, which cross-checks refs against the journal.
- Replacing an object is caught by the content re-hash.

Gate bypasses are part of the record: a save made with `--no-verify` or `--allow-secrets` carries that fact in its journal entry, and `sb journal` shows it inline (`· no-verify · secrets-override`). Skipping a gate is allowed; hiding it is not.

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
- Pass-keys are always `-k <passkey>` across `pack`, `unpack`, and `export`.
- Subactions are words: `sb test list`, `sb test new`, `sb publish list` (`-l` also works).
- Mistakes get a one-line explanation, the correct usage line for that command, and a pointer to `sb help`, not a parser dump.

`<angle brackets>` are required, `[square brackets]` optional. Commands work from anywhere inside the repository. Colors appear only when output is a terminal.

### `sb init`

Creates the repository: `.sb/sandbox.db`, branch `main`, and a journal seeded with a random repository ID. The database is created with `0600` permissions. Fails if a repository already exists here.

### `sb status [--deep]`

Current branch, latest save, and every change relative to it: `renamed old → new`, `new`, `modified`, `deleted`. In shared mode, also shows who holds which locks. `--deep` bypasses the stat cache and re-hashes every file.

### `sb save "<message>" [--allow-secrets] [--no-verify] [--global-force]`

Snapshots every tracked file as a new save. The message is required. In order:

1. Diff against the last save; if nothing changed, no save is created.
2. Secret scan over every added or modified file (Section 8). Findings block; `--allow-secrets` overrides.
3. Pre-save test gates in a clean checkout of the candidate tree (Section 7). Failures block; `--no-verify` overrides.
4. Re-check that the worktree didn't change while being scanned and tested; refuse rather than commit an untested state.
5. Store blobs, build the tree, write the commit, move the branch tip, and journal it, in one transaction. Bypass flags are journaled with it.

In shared mode, `save` commits only your files (Section 6); `--global-force` sweeps in everyone's edits and is journaled as such.

### `sb log [-n <count>]`

Save history, newest first: hash, date, author, message, a `(merge)` marker, and a change summary per save (`+2 new · ~1 modified · old.txt → new.txt`). `-n 5` limits output.

### `sb diff [<path>]`

Unified diff between the working folder and the last save. Renames show as one `@@ old → new  renamed (content identical)` line. Binary files show as a one-line size summary. With `<path>`, limits output to that file or folder.

### `sb undo [-p <path>]`

Reverts the latest save by creating a new save whose content equals the previous one. The undone save stays in the log and journal; running `sb undo` again redoes. Requires a clean working tree.

With `-p <path>`, brings just that file or folder back from the last save, overwriting the working copy. No new save is created. These writes go through the same symlink-safe atomic machinery as checkout, so a symlink planted at the path or in a parent cannot redirect the write outside the repository.

### `sb restore <anchor | save | release-label | branch>`

Returns the current branch to any past state, as a new save. Nothing is rewound or deleted; `sb undo` afterward takes you straight back. Requires a clean working tree.

Targets: an anchor (8–64 hex characters of a journal chain link, resolving to the branch tip as the journal recorded it at that moment), a save-hash prefix from `sb log` (4+ characters), a release label (`sb restore rel-3`), or a branch name. Ambiguous targets are rejected with a list of what matched.

### `sb branch [<name>] [-r]`

No argument: list branches. With a name: create one at the current save. With `-r`: delete the named branch's pointer (never the current branch, never the last one). Its saves stay in the store and journal, and `verify` keeps checking them.

### `sb switch <branch>`

Rewrites the working folder to the branch's latest save and updates the branch pointer. Refuses with unsaved changes. Writes are atomic and symlink-safe; directories emptied by the switch are pruned.

### `sb merge <branch> [--no-verify] [-i]`

Three-way merge into the current branch, using the best common ancestor as the base (a true lowest-common-ancestor search, correct after prior merges; criss-cross histories resolve deterministically).

- If the current branch is an ancestor of the target, the tip fast-forwards (still gated by pre-merge tests).
- Files changed on one side take that side.
- Files changed on both sides get a line-level three-way merge; non-overlapping edits combine, overlapping edits conflict.
- On conflict, the merge stops before touching anything. Your working folder is unchanged, and each conflicting file is listed with the reason. Reconcile on one branch, save, merge again. There is no in-progress merge state.

The merge is conservative on purpose: adjacent-line edits, same-point insertions, CRLF files, binaries, and files without a trailing newline count as conflicts rather than being guessed at or rewritten.

Pre-merge gates run against the merged tree itself before it is committed.

With `-i` (`--ignore`), in shared mode: skip files locked by others. A merge that skipped files is recorded as partial (a single-parent save, not a merge commit), so re-running the merge after the locks release brings in the skipped files. Sandbox does not record ancestry it didn't actually merge.

### `sb test [<stage>]` / `sb test new <stage> <name>` / `sb test list` / `sb test guide`

Run gates manually (all stages or one of `pre-save`, `pre-merge`, `pre-publish`), scaffold a new script, list discovered tests, or print the built-in walkthrough.

### `sb publish [<label>]` / `sb publish list` / `--no-verify`

Records the current save as a release, behind two gates: full store verification (Sandbox refuses to publish from a damaged store), then pre-publish tests on a clean checkout of the exact tree. Passing both writes a `publish` entry into the journal: what, from which branch, by whom, when, and the content hashes of the gate scripts that ran. `sb publish -l` lists releases and reports whether the chain protecting them still verifies.

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

The operation log, with detail for every operation type: ref moves with old → new hashes and any bypass flags, releases, switches, branch removals, lock releases (with `· forced` when forced), ignore rules, shared/durability changes, identity registrations, and pack/export/unpack events. Ends by re-verifying the chain.

### `sb info`

One-screen overview: store location and size, current branch, object counts, journal length, current anchor, attribution.

### `sb who [<name>] [<email>]`

Shows or sets how saves are attributed, stored in `~/.config/sandbox/profile.json` (`SB_HOME` overrides the location; `SB_NAME`/`SB_EMAIL` override per command). Attribution, not authentication (Section 9).

### `sb shared [on|off]`

Shows or sets shared mode (Section 6). Journaled.

### `sb durability [full|normal]`

Shows or sets crash durability. `full` (default): the newest committed transaction survives power loss. `normal`: faster, still crash-safe, but may lose the most recent commit on power loss. Journaled.

### `sb locks`

All shared-mode locks: who holds what, since when, and when each expires.

### `sb unlock [<path>...] [--force]`

Releases your locks (all, or the named paths). `--force` releases locks held by others; the journal records the paths, the prior owners, and that it was forced.

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

`sb version` (also `-V`) prints `sb 1.2 · jts.gg/sandbox`. `sb selftest` runs the 20-check built-in suite: atomic rollback, symlink and path escapes, mid-gate mutation, CRLF merge safety, concurrent-save races, corruption detection, archive crypto, the stat cache, and the shared-mode locking protocol.

---

## 6. Shared mode

Shared mode lets a small team work in one repository — one folder, one database — with per-file locks instead of clone/push/pull. Turn it on once per repository:

```bash
sb shared on
```

### The rules

- Editing a file locks it to you. There is no daemon; locks are picked up the next time any Sandbox command scans the tree, and attributed to the actual editor (below).
- A lock lasts until you `sb save`, or one hour (`SB_LOCK_TTL`). On expiry, your edits are auto-saved as a commit in your name and the lock frees, so an abandoned lock can't block the team and no work is lost.
- While you hold a lock, only you can save that file. `sb save` commits your locked/changed files and leaves everyone else's in-progress edits alone, on disk and in the commit. `sb save --global-force` sweeps in everyone's edits, and the journal says so.
- A merge that would change a file someone else has locked is refused. `sb merge <branch> -i` skips those files; the merge proceeds for everything else, each skipped file keeps your current version, and the result is recorded as a partial merge (single parent), so re-running the merge after the locks release picks up what was skipped.
- `sb locks` shows who holds what. `sb unlock` releases yours. `sb unlock --force` releases someone else's, journaled with the prior owner's name.

### Who gets the lock

A file on disk carries no record of who edited it, so a lock found at scan time must not simply go to whoever ran the scan. If Alice edits and Bob runs `sb status`, Bob must not become the owner of Alice's edit. The signal Sandbox uses is the file's owner uid: each teammate writes as their own OS account. Sandbox keeps a uid → identity registry (every Sandbox command anyone runs in shared mode records which OS account maps to which `sb who` identity) and locks each discovered edit to the account that owns the file. Someone who has never run Sandbox still gets correct locks under their system account name, and expired-lock auto-saves are committed under the real editor's name.

Where the signal doesn't exist, attribution falls back to the invoking user: deletions (nothing left to stat) and uid-squashing network mounts. One editor-dependent detail: editors that save by write-temp-then-rename (most IDEs) make the editor the file's owner, so attribution is exact; an editor that truncates in place leaves the original creator as owner. New files always attribute correctly. Saving promptly closes the window either way.

### Where shared mode belongs

Shared mode is designed for one machine with multiple user accounts, or a directly attached shared disk. The store is SQLite in WAL mode, and SQLite's documentation is explicit that WAL does not work reliably over NFS or SMB mounts, because file locking on network filesystems is broken in ways no application can compensate for. If your shared drive is a network mount, treat shared mode as unsupported there and move work with `.sbox` archives instead.

---

## 7. Test gates

Test gates turn "we should run the tests" into "the tests ran, or the operation didn't happen."

### How it works

Put executable scripts in these folders. They are ordinary tracked files, so they version, branch, and merge with your code:

```
sb-tests/
  pre-save/       runs before every save (shared-mode saves included)
  pre-merge/      runs before every merge (including fast-forwards)
  pre-publish/    runs before every release
```

Scripts run sorted by name (use prefixes: `10-lint.sh`, `20-unit.py`), each inside a pristine temporary checkout of the exact candidate tree:

- A pre-save gate sees your candidate files in a clean directory, so nothing ignored or untracked leaks in. In shared mode it sees the exact merged tree your partial save will produce.
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

`.py` scripts run under the same Python as Sandbox; executables run directly; anything else runs under `sh`.

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

## 8. The secret scanner

The most common irreversible mistake in version control is committing a credential; history is permanent, and rotating a leaked key is an incident. Sandbox scans every file being added or modified, at save time, for:

- AWS access keys (`AKIA…` / `ASIA…`)
- Private key blocks (`-----BEGIN … PRIVATE KEY-----`, including RSA/EC/OpenSSH/DSA/PGP)
- GitHub tokens (`ghp_…`, `gho_…`, and friends)
- Slack tokens (`xoxb-…` etc.)
- Google API keys (`AIza…`)
- Stripe live keys (`sk_live_…`, `rk_live_…`)
- JWTs
- Generic assignments like `password = "…"` or `api_key: '…'` with long quoted values

Findings block the save (exit `2`) with file, line number, and pattern name. In order of preference:

1. Remove the secret; load it from the environment or an ignored config file.
2. Ignore the file: `sb ignore .env`, then save.
3. Override with `sb save "msg" --allow-secrets` when it's a false positive. The override is journaled.

The scan applies to shared-mode saves too. Binary files and files over 1 MB are skipped. Caveats: pattern matching catches known credential shapes, not every secret, and only files touched by the current save are scanned. Treat it as a seatbelt, not a substitute for keeping secrets out of tracked files.

---

## 9. Security model

Sandbox makes three promises. Each comes with its mechanism, what it defends against, and what it doesn't.

### Promise 1 — Integrity: what you get back is what you put in

Mechanism: every object is stored under the SHA-256 of its content and re-hashed on every read. Every save embeds its tree hash and parent hashes, so each save transitively fixes the exact bytes of every file in it and every save before it. Every operation commits as one SQLite transaction (WAL mode, `synchronous=FULL` by default). Working-folder writes use exclusive randomized temp files, fsync, and atomic rename through no-follow parent directory descriptors; this applies to checkout, switch, merge, restore, and `undo -p`. A crash between the database and the working folder surfaces as ordinary unsaved changes in `status`.

Defends against: disk corruption, torn and partial writes, power loss mid-operation, truncated or bit-flipped objects, malformed objects that hash correctly but don't parse, crafted trees attempting path traversal on checkout, and symlinks attempting to redirect any write outside the repository.

Does not defend against: loss of the database file itself. Integrity detection is not a backup.

### Promise 2 — Tamper evidence: changes made behind Sandbox's back are detectable

Mechanism: the hash-chained journal. Every operation appends an entry whose link is `SHA-256(canonical entry ‖ previous link)`, rooted in a random per-repository ID. `sb verify` recomputes the chain and cross-checks every branch tip against the journal's record, flagging tips that were moved, branches that were deleted, and refs that were injected. Gate bypasses are part of the chained record.

Defends against: editing or deleting journal entries; manipulating refs via direct database access; replacing objects; modification by anything that isn't sb.

Does not defend against: an attacker with write access to the database and knowledge of Sandbox's format can rewrite the entire store into a new, internally consistent history. With no secret material anywhere, internal consistency is recomputable by anyone; this is inherent to keyless designs. Anchors (Section 10) close the gap: a chain-head value recorded outside the machine cannot be reproduced by any rewrite. What Sandbox will not do is ship the appearance of cryptographic authenticity (signatures, badges) without the key management that would make it meaningful.

### Promise 3 — Leak prevention: credentials are stopped before they enter permanent history

Mechanism: the save-time secret scanner (Section 8), on by default, overridable only explicitly, overrides journaled.

Defends against: accidental commits of recognizable credentials.

Does not defend against: unrecognizable secrets, or secrets already in older saves.

### What Sandbox does not claim

- Confidentiality of the store. The database is created `0600`; full-disk encryption is the right layer for confidentiality at rest. (`.sbox` archives are encrypted; that covers transport and cold storage.)
- Authentication. The author on a save is attribution for humans reading history. Shared mode's uid-based lock attribution improves accuracy; it is still not authentication.
- Access control. Sandbox is a local tool; file permissions are the access control.

### Why the cryptography was removed

An earlier design signed every commit with Ed25519, with a hand-rolled pure-Python fallback. That fails security review on three grounds: hand-rolled signature code is where implementation vulnerabilities live; keys with no management story (generated silently, stored in a dotfile, never rotated or bound to anything a verifier could trust) prove only that some key signed something; and a claim users trust more than its mechanism deserves is worse than no claim. The same standard was applied in 1.2 to the embedded encryption module, which carried an unused "asymmetric" mode whose construction did not deliver public-key security; it was deleted rather than documented around. Every property the removed code was supposed to provide is covered by the re-hashing, the chain, the ref cross-check, and anchors.

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
```

`-f`/`--files-only` seals just the current save's files, without history.

To restore, on any machine with Sandbox, fully offline:

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

Sandbox's integrity model uses no cryptographic keys (Section 9); nothing in save, merge, verify, the journal, or anchors depends on a secret. Archive confidentiality is a separate concern, handled by [vox](https://jts.gg/vox) (v1.7.3, symmetric core), a small single-file encryption module embedded inside Sandbox and loaded into memory only while `pack`, `unpack`, or `export -k` runs. No separate file, no install step, no network. The unused legacy asymmetric interface was removed from the embedded copy in 1.2.

vox is a misuse-resistant authenticated cipher: an SIV-style AEAD built on HMAC-SHA512, with PBKDF2-HMAC-SHA512 key stretching at 300,000 iterations. Since sbox format v2, a random per-archive salt is mixed into key derivation, so two archives sealed with the same passphrase use different keys and a password guess can't be amortized across archives. Two practical consequences:

- A wrong pass-key or a single altered byte means the archive will not open. vox verifies authenticity before decrypting, and Sandbox re-hashes the recovered payload against `db_sha256`; full-repo archives additionally go through the staged verification battery.
- The pass-key is the only thing standing between the archive and its contents. There is no recovery and no key file. A weak pass-key is a weak archive.

Archives are written whole and held in memory while sealing and opening: comfortable to a few GB, not a streaming format (Section 20).

---

## 12. The storage format

The entire repository is one SQLite database: `.sb/sandbox.db`, in WAL mode, created `0600`, with `synchronous=FULL` by default (`sb durability normal` trades the newest-commit-on-power-loss guarantee for speed; the change is journaled).

- Crash safety: every operation commits as a single ACID transaction. (Fossil, the VCS written by SQLite's author, made the same bet fifteen years ago.)
- No small-file sprawl: the repo is one file, so `cp` is a valid backup and `rsync` sees one changed file.
- Real queries: prefix resolution is an indexed `LIKE`; the stat cache and locks are ordinary tables.
- Inspectable with standard database tooling, and anything changed with that tooling behind Sandbox's back is flagged by `verify`.

### Schema

| table | contents |
|---|---|
| `meta` | key/value: `format` version, random `repo_id` (chain root), current `branch`, settings, uid → identity registry |
| `objects` | `hash → kind, size, zlib(data)`: the content-addressed store |
| `refs` | `name → commit hash`: branch tips (empty string = branch with no saves) |
| `journal` | `seq, ts, op, detail(JSON), prev, link`: the append-only hash chain |
| `statcache` | `path → size, mtime, ctime, inode, hash`: change detection without re-reading |
| `locks` | `path → owner, email, since, base`: shared-mode per-file locks |

### Object encodings

An object's hash is `SHA-256("<kind> <length>\0" + data)`. Trees and commits are canonical JSON (sorted keys, no whitespace). A tree is `[[mode, kind, hash, name], …]` sorted by name; a commit is `{tree, parents, author, email, time, message}`. Modes are `100644` (file), `100755` (executable), `040000` (directory).

### The stat cache

`status` and `save` detect changes by comparing each file's size, mtime, ctime, and inode against the cache; on a full match the previous hash is reused and the file isn't read. mtime alone can be restored from userspace (`touch -d`, archive extraction), but ctime is kernel-maintained and the inode changes when an editor replaces a file, so a same-size edit with a restored mtime still misses the cache and gets re-read. Files touched within the last two seconds always bypass the cache; during a save a cached hash is only trusted if the blob exists in the store; `--deep` bypasses the cache entirely. A miss only costs a re-read; the cache fails toward correctness.

### What write paths guarantee

Tree entry names are validated on read (no `/`, `\`, NUL, `.`, `..`, empty, or `.sb`), so a hostile tree can't write outside the repository. Every worktree write (checkout, switch, merge, restore, `undo -p`, archive extraction) goes through a parent directory opened with no-follow semantics, an exclusively-created randomized temp file, a complete-write loop, fsync, and atomic rename. Archive outputs (`pack`, `export -k`) use the same discipline. Directory pruning never touches `.sb`.

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

Two behaviors to know, the second being a real footgun: ignored files are invisible to `save`/`status` but never deleted by Sandbox; and ignoring an already-tracked file makes it show as `deleted`, so the next save removes it from the tree (the file stays on disk but leaves history going forward). If you meant "stop tracking but keep the file," that's what happens. If you didn't, check `status` before saving.

---

## 14. Everyday workflows

**Solo project, straight line.** `sb init`, then work / `sb status` / `sb save` in a loop. Add a pre-save syntax gate (`sb test new pre-save 10-syntax`) on day one.

**Safe experiment.** `sb branch spike && sb switch spike`, hack with saves as checkpoints. If it works: `sb switch main && sb merge spike`. If it doesn't: switch back and never merge.

**"I broke it ten minutes ago."** `sb diff` to see the damage; `sb undo -p <file>` to reclaim one file from the last save; `sb undo` to revert the whole last save.

**"I deleted everything."** Nothing saved is ever lost: `sb save "oops"` (yes, save the wipe), then `sb undo`. Both the deletion and its reversal live in history.

**"It worked last Tuesday."** Find last Tuesday (an anchor you noted, a save in `sb log`, or a release label) and `sb restore <it>`.

**Small team, one machine or one attached disk.** `sb shared on`. Everyone works in the same folder; editing a file locks it to the actual editor; `sb save` commits only your files; `sb locks` shows who holds what; expired locks auto-save in their owner's name. Merges refuse to clobber locked work; `sb merge feat -i` proceeds around it as a recorded partial merge and completes on re-merge.

**Release with a paper trail.** Keep the real suite in `sb-tests/pre-publish/`. Ship with `sb publish v1.4`: Sandbox verifies the store, runs the suite against a clean checkout of exactly what's shipping, and journals the record, including the content hashes of the gate scripts that ran. `sb publish -l` is the release history.

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

## 15. Sandbox versus git

| | **Sandbox** | **git** |
|---|---|---|
| Mental model | work → save | work → stage → commit (+ index states) |
| Staging area | none; a save is what you see | the index, with its own command set |
| Detached HEAD | impossible | routine source of confusion |
| Destroying history | no command does it | `reset --hard`, `push -f`, dropped stashes, expired reflog |
| Undo | `sb undo`, a new save, reversible | `revert` vs `reset` vs `restore` vs `checkout` |
| Repository format | one crash-safe SQLite file | thousands of loose files + packfiles + refs + index |
| Operation audit log | hash-chained journal of every operation, bypasses included, cross-checked vs refs | reflog: per-machine, expiring, mutable, unchained |
| Tamper evidence | chain + tip cross-check + external anchors | commit DAG only; refs/reflog unprotected |
| Secret prevention | built into every save, overrides journaled | third-party hooks you must install |
| Test enforcement | versioned gates on clean checkouts, on by default | hooks: unversioned, per-clone, easily absent |
| Renames | exact-content detection in status/diff/log | similarity-based detection, rename-aware merges |
| Merge conflicts | auto-merge non-overlap; on conflict, stops cleanly, worktree untouched | conflict markers + in-progress merge state |
| Small-team sharing | shared mode: one repo, per-file locks | clone/push/pull, remotes |
| Remotes / distributed collaboration | not yet (roadmap) | git's core strength |
| Platform | Linux / macOS / WSL | everywhere |
| Ecosystem | one file, zero deps | vast |

Summary: git is a distributed collaboration system you can also use alone; Sandbox is a safety-and-integrity system for individuals and small teams. If you need GitHub-style multi-party collaboration today, use git, possibly with Sandbox alongside it (they coexist; Sandbox ignores `.git`, add `.sb` to `.gitignore`).

---

## 16. Environment variables

| variable | effect | default |
|---|---|---|
| `SB_NAME` | attribution name for saves (overrides `sb who`) | profile, else OS username |
| `SB_EMAIL` | attribution email | profile, else `<name>@local` |
| `SB_HOME` | folder for the global profile | `~/.config/sandbox` |
| `SB_TEST_TIMEOUT` | seconds allowed per test script | `120` |
| `SB_LOCK_TTL` | seconds a shared-mode lock lives before auto-save + release | `3600` |

Inside test scripts, Sandbox also exports `SB_STAGE`, `SB_BRANCH`, `SB_COMMIT`, `SB_REPO` (Section 7).

---

## 17. Exit codes

| code | meaning |
|---|---|
| `0` | success |
| `1` | usage or state error (not a repo, unsaved changes, unknown branch, bad arguments, non-empty unpack destination, corrupt object hit mid-operation, file-system error, …) |
| `2` | a gate stopped you: secrets found, test gates failed, merge conflicts, or `verify` found problems |
| `130` | interrupted (Ctrl-C) |

The split is script-friendly: `2` means Sandbox worked correctly and blocked something on purpose, so automation can tell "fix your command" from "fix your content."

---

## 18. FAQ

**Where did the signatures go? Is Sandbox less secure now?**
The Ed25519 signing was removed deliberately; Section 9 has the full reasoning. Keys with no management story prove nothing, and a hand-rolled fallback implementation is a liability. The properties the signatures actually provided (integrity, tamper evidence) are covered by content re-hashing, the journal chain, the ref cross-check, and anchors. The same standard removed the encryption module's unused asymmetric mode in 1.2.

**Is SHA-256 "cryptography"? I asked for none.**
It's a hash function from Python's standard library used as a content fingerprint. No keys, no signatures, no third-party crypto code. Content addressing, integrity checking, and the journal are all built on it.

**How do I back up a repository?**
Copy `.sb/sandbox.db` (any time Sandbox isn't mid-command; WAL makes even that forgiving), copy the whole project folder, or `sb pack` for an encrypted single-file backup. After restoring, run `sb verify`.

**Can I have partial commits, like `git add -p`?**
No, by design: a save is exactly your working tree, which is what makes "the tests passed on this save" meaningful. Two unrelated changes belong on two branches or in two saves. Shared mode's your-files-only save is the one exception, and it exists to protect other people's in-progress work.

**What about large or binary files?**
Stored (zlib-compressed) and versioned like anything else; `diff` summarizes them in one line and the secret scanner skips them. Every version of a large file is kept in full; there is no delta compression yet, so frequently-changing large binaries grow the store quickly.

**Does rename detection catch a file I moved and edited?**
No. Detection is exact-content only, so a moved-and-edited file shows as an add plus a delete. Similarity-based detection is on the roadmap.

**Symlinks?**
Not tracked yet; skipped with a printed note. Symlinks pointing into the repo also can't hijack Sandbox's writes: every write path refuses to follow them.

**Can two commands run at once?**
Yes. SQLite serializes writers, and racing saves are protected by a compare-and-swap on the branch tip plus a worktree drift check. A race ends with one clean success and one "run it again" error, never corruption. Covered by `sb selftest`.

**Does anything leave my machine?**
No. There is no network code in sb.

**Can I rename a branch?**
Not built in yet. Today: `sb branch new-name && sb branch old-name -r` (from another branch). Removal is journaled and never deletes saves.

**Can `unpack -i` be undone?**
No. `-i` overwrites files in place with no per-file backup. That's why it's a flag: without it, unpack refuses any non-empty destination, so overwriting only happens when you asked for it.

**Who ends up owning a lock if Bob's `sb status` discovers Alice's edit?**
Alice. Locks are attributed to the file's owner on disk, resolved through the uid → identity registry, not to whoever ran Sandbox first. Section 6 covers the edge cases.

---

## 19. Troubleshooting

**`error: not inside a sandbox repository`** — you're outside any folder containing `.sb/sandbox.db`. `cd` in, or `sb init`.

**`error: sb <command>: unrecognized arguments: …` / `missing: …`** — the flag or argument doesn't exist for that command; the correct usage line is printed below the error.

**`error: you have unsaved changes`** — `switch`, `merge`, `undo`, `restore`, and `publish` refuse to run over uncommitted work. `sb save "wip"` (saves are cheap, undo is free), or `sb undo -p <path>` for changes you want gone.

**`merge blocked — it would change files locked by others`** — shared mode protecting a teammate's in-progress edit. Wait, ask them to save, or `sb merge <branch> -i` to proceed around those files as a recorded partial merge.

**`error: <folder> is not empty — unpack into a fresh folder`** — unpack never writes into a destination that already contains anything. Pick a fresh folder, or add `-i` to overwrite matching files on purpose.

**`the archive's repository failed verification — nothing was written`** — the store inside the `.sbox` is damaged or was tampered with; unpack refused before touching the destination. Get a good copy of the archive.

**`save blocked — possible secrets detected`** — Section 8. Remove the secret, ignore the file, or `--allow-secrets` if you're sure (journaled).

**`pre-save tests failed — save blocked`** — the failing script's last 15 lines are printed above the error. Reproduce with `sb test pre-save`. Override once with `--no-verify` (journaled), then fix the gate.

**`merge stopped — these files conflict`** — the reason is printed per file; your worktree was not touched. Reconcile on one branch, save, merge again.

**`object … does not match its hash` / `verify` reports problems** — real corruption or tampering; Sandbox stopped rather than propagating it. Restore `.sb/sandbox.db` from a backup, then `sb verify`. Undamaged files can be rescued first via `sb undo -p` or `sb export` from saves whose objects are intact.

**`branch '…' changed under this operation … run the command again`** — two Sandbox commands raced; yours lost the compare-and-swap and nothing was changed. Re-run it.

**`store error: … database is locked`** — another Sandbox command has the database open for writing. Wait a moment and retry.

**`file system error: …`** — a permission or disk problem outside Sandbox's control, reported cleanly.

**A file isn't being saved** — it matches an ignore rule. Check `.sbignore` and the built-in defaults (Section 13). Symlinks are skipped with a printed note.

---

## 20. Known limitations and roadmap

- **POSIX only.** Linux, macOS, WSL. The symlink-safe write machinery relies on directory descriptors Windows doesn't provide; supporting Windows by weakening those guarantees isn't on the table, so it waits until it can be done properly.
- **No live remotes yet.** Repositories move between machines as encrypted `.sbox` archives, which works but is manual. Journal-first sync is the top roadmap item.
- **Shared mode wants a local disk.** SQLite WAL is not reliable over NFS/SMB mounts (Section 6). One machine or a directly attached disk is supported; for network mounts, use archives.
- **No symlink tracking** (skipped with a note).
- **Whole-file storage.** zlib-compressed but not delta-compressed; heavy for large, frequently-changing binaries. Archives are also held in memory while sealing/opening: fine to a few GB, not streaming.
- **Rename detection is exact-content only.** Moved-and-edited files show as add + delete; merges are not rename-aware.
- **Conservative merges.** Adjacent-line edits and same-point insertions conflict rather than merge, and conflicts are resolved on a branch rather than via in-worktree conflict markers.
- **No branch rename, no per-save tags** yet.
- **`unpack -i` keeps no backup.**
- **Anchors are manual.** Automatic anchoring is on the roadmap.
- **Ignoring a tracked file drops it from the next save** (Section 13). Correct under the snapshot model, surprising if unread.

---

*sb — one file, no dependencies, nothing silently destroyed.*
