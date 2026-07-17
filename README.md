# sandbox (sb)

**Version 1.2** · [jts.gg/sandbox](https://jts.gg/sandbox)
**License** · [jts.gg/license](https://jts.gg/license)

**Safe, honest version control for humans.** One file. One command vocabulary you can learn in five minutes. Zero dependencies beyond Python 3.9+

sb is not a git clone and does not use git's repository format. It keeps the two ideas git got right — content addressing and a Merkle DAG of snapshots — and replaces everything that makes git hostile to daily use: the staging area, detached HEADs, destructive commands, a repository made of thousands of fragile loose files, and error messages written for git's own developers.

New in 1.2: **shared mode** (one repository, a whole team, per-file locks — no clone/push/pull), **rename detection**, a journal that records and displays **every** operation including gate bypasses, archives that are **verified end-to-end before a single byte is installed**, and hardened write paths throughout.

---

## Table of contents

1. [Installation](#1-installation)
2. [Why sb exists](#2-why-sb-exists)
3. [Five-minute quickstart](#3-five-minute-quickstart)
4. [Core concepts](#4-core-concepts)
5. [Command reference](#5-command-reference)
6. [Shared mode: one repo, many hands](#6-shared-mode-one-repo-many-hands)
7. [Test gates: quality enforcement](#7-test-gates-quality-enforcement)
8. [The secret scanner](#8-the-secret-scanner)
9. [Security model and threat model](#9-security-model-and-threat-model)
10. [Anchors: pinning history outside the machine](#10-anchors-pinning-history-outside-the-machine)
11. [Portable archives (.sbox)](#11-portable-archives-sbox)
12. [The storage format](#12-the-storage-format)
13. [Ignoring files](#13-ignoring-files)
14. [Everyday workflows](#14-everyday-workflows)
15. [sb versus git](#15-sb-versus-git)
16. [Environment variables](#16-environment-variables)
17. [Exit codes](#17-exit-codes)
18. [FAQ](#18-faq)
19. [Troubleshooting](#19-troubleshooting)
20. [Known limitations and roadmap](#20-known-limitations-and-roadmap)

---

## 1. Installation

Requirements: Python 3.9+ (standard library only — no pip, no dependencies) on **Linux, macOS, or WSL**. sb's symlink-safe write paths use POSIX directory descriptors, so native Windows is not supported — use WSL there.

**Install system-wide** (all users, requires sudo):

```bash
curl -sL install.jts.gg/sandbox | sudo bash
```

**Install for your user only:**

```bash
curl -sL install.jts.gg/sandbox | bash
```

Then confirm it worked:

```bash
sb help
```

**Manual install** (if you prefer to inspect before running):

```bash
# put it somewhere on your PATH
mkdir -p ~/.local/bin
cp sb.py ~/.local/bin/sb
chmod +x ~/.local/bin/sb
```

If `~/.local/bin` is not on your PATH, add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile.

To upgrade, re-run the install command. sb refuses to open repositories created by a *newer* format than it understands, so upgrades are always safe and downgrades fail loudly rather than corrupting anything. `sb selftest` runs the built-in adversarial test suite (20 checks: crash injection, symlink escapes, races, corruption, crypto, shared-mode locking) any time you want proof the installed copy behaves.

---

## 2. Why sb exists

Version control solves a real problem: *"I want to change things without fear, and know exactly what happened later."* Git solves that problem too — wrapped in an interface where `checkout` means four different things, where a mistyped `reset --hard` erases an afternoon of work, and where the repository itself is a directory of thousands of loose files that a power cut can leave half-written.

sb starts from three convictions.

**Safety should be structural, not disciplinary.** In sb there is no command that discards saved history. `undo` creates *new* history that reverts the old. `switch` refuses to run over unsaved work. The store is one SQLite database, so every operation is a single atomic transaction — a crash mid-save leaves you exactly where you were, never in a torn in-between state.

**A tool can be simple without being a toy.** sb has no staging area (a save snapshots everything that isn't ignored), no detached HEAD (you are always on a branch), and no rebase (history is append-only). Yet it has real branches, real three-way merges with automatic conflict-free merging, rename detection, versioned test gates that block bad saves, release records, per-file team locking, and a full-store integrity verifier.

**Security features should be honest.** sb makes exactly three security promises — integrity, tamper evidence, and leak prevention — and Section 9 states precisely how each is achieved, what it defends against, and what it deliberately does not. There are no keys to lose, no signatures to misunderstand, and no cryptography library in the dependency tree. Everything rests on one primitive: SHA-256 from Python's standard library, used for content addressing and hash chaining.

---

## 3. Five-minute quickstart

```bash
cd my-project
sb init                          # creates .sb/sandbox.db, branch "main"
sb who "Ada" "ada@example.com"   # how your saves are attributed (once, globally)

# work, then snapshot
sb status                        # what changed?
sb diff                          # show me line by line
sb save "add login form"         # snapshot everything

# experiment safely
sb branch idea                   # new branch at the current save
sb switch idea                   # move to it (refuses if you'd lose work)
...hack hack hack...
sb save "try the risky refactor"
sb switch main
sb merge idea                    # 3-way merge; non-overlapping edits merge themselves

# oops
sb undo                          # reverts the last save — as a NEW save
sb undo -p src/app.py            # bring one file back from the last save
sb restore 67b3dea8b260c12a      # return to any past anchor, save, or release

# trust, but verify
sb verify                        # re-hash every object, check the journal chain
sb journal                       # the tamper-evident log of everything sb ever did

# move it somewhere safe
sb pack -k "a-strong-pass-key"                        # seal the whole repo into an encrypted .sbox
sb unpack my-project.sbox -k "a-strong-pass-key"      # restore on another machine (verified before install)
```

That's 90% of daily use. The remaining 10% — shared mode, test gates, releases, anchors — is below.

---

## 4. Core concepts

### Saves

A **save** is a complete snapshot of every tracked file, with a message, an author, and a timestamp. There is no staging area and no partial commit: what you see in your working folder (minus ignored files) is what gets saved. This is deliberate — the staging area is the single largest source of git confusion, and "the snapshot is exactly what I'm looking at" is a property you can reason about. (Shared mode refines this: your save commits *your* files and leaves teammates' in-progress edits alone — Section 6.)

Every save records the hash of its parent save, so saves form a chain (and, with merges, a directed acyclic graph). Changing any byte of any past save would change its hash and break every link after it — history is tamper-evident by construction.

### Branches

A **branch** is a named pointer to a save. Creating one is instant and costs nothing (`sb branch idea`). You are *always* on exactly one branch; sb has no detached-HEAD state. Switching branches rewrites your working folder to match that branch's latest save — and refuses to run if you have unsaved changes.

### The object store

Every piece of content lives in a **content-addressed object store**: the key for each object is the SHA-256 hash of its content. Identical files are stored once no matter how many saves contain them. Three kinds of object exist:

| kind | contents |
|---|---|
| `blob` | raw file bytes |
| `tree` | a directory listing: `[[mode, kind, hash, name], ...]` as canonical JSON |
| `commit` | `{tree, parents, author, email, time, message}` as canonical JSON |

Every object is **re-hashed on every read**. Silent corruption cannot flow into your working folder: a damaged blob raises an error the moment anything touches it, and `sb verify` finds it proactively.

### The journal

The **journal** is sb's signature idea: an append-only log inside the store where **every operation is recorded** — every save, merge, undo, restore, branch creation and removal, switch, release, lock release, ignore rule, shared/durability setting change, identity registration, and even every `pack`, `export`, and `unpack`. Each entry embeds the SHA-256 link of the previous entry, forming a **hash chain** rooted in a random repository ID chosen at `init`.

This makes the repository's *operational history* tamper-evident, not just its content:

- Delete or edit a journal entry → the chain breaks at that point.
- Move a branch tip behind sb's back (even with direct SQL against the database) → `sb verify` catches it, because branch tips are cross-checked against what the journal last recorded — including refs *added* outside sb, which are flagged too.
- Corrupt an object → the content re-hash catches it.

Gate bypasses are part of the record: a save made with `--no-verify` or `--allow-secrets` carries that fact in its journal entry, and `sb journal` displays it inline (`· no-verify · secrets-override`). Skipping a gate is allowed; hiding that you skipped it is not.

`sb journal` shows the log; `sb verify` proves it. See Section 9 for exact guarantees and Section 10 for anchoring the chain outside the machine entirely.

### Renames

`status`, `diff`, and `log` detect renames by exact content: a deleted path and an added path with byte-identical content display as `renamed old → new` (one line in `diff`, an arrow in `log`'s change summary) instead of a confusing delete-plus-add. Detection is deliberately exact-match only — a file that was moved *and* edited shows as an add and a delete, because sb reports what it knows, never what it guesses. Empty files never pair.

### Test gates

Executable scripts in `sb-tests/pre-save/`, `sb-tests/pre-merge/`, and `sb-tests/pre-publish/` run automatically before the corresponding operation and **block it on failure**. They always run in a pristine temporary checkout of the exact candidate tree — never your dirty working folder — so "passes the gate" means "the actual thing being saved passes." Section 7 covers them fully.

---

## 5. Command reference

Every command follows the same grammar, so nothing needs memorizing:

- **Positional arguments say *what*** — a message, a path, a branch, a version, a file. Order matters only among positionals.
- **Options say *how*** — and every routine option has a short and a long form: `-k`/`--key`, `-f`/`--files-only`, `-i`/`--ignore`, `-n`/`--limit`, `-r`/`--remove`, `-a`/`--anchor`, `-l`/`--list`, `-p`/`--path`. Options may appear anywhere on the line, before or after positionals.
- **The safety overrides — `--allow-secrets`, `--no-verify`, `--global-force`, `--force` — have no short form on purpose.** Bypassing a gate or taking someone's lock is something you type out in full, never something a one-letter slip can do. (`-i` is deliberately *not* in this class: skipping locked files in a merge, or merging an unpack into a folder, is a routine action whose gate is that it never happens without the flag.)
- **Pass-keys are always `-k <passkey>`** (long: `--key`) across `pack`, `unpack`, and `export`.
- **Subactions are words**: `sb test list`, `sb test new`, `sb test guide`, `sb publish list` (`-l`/`--list` also accepted).
- **Mistakes get useful answers.** A wrong flag, a missing argument, or a typo'd command prints a one-line explanation in sb's own voice, the correct usage line for *that* command, and a pointer to `sb help` — never a raw parser dump.

Conventions: `<angle brackets>` are required, `[square brackets]` optional. All commands work from anywhere inside the repository. Colors appear only when output is a terminal, so piping to files or scripts is always clean.

### `sb init`

Creates a repository in the current folder: the `.sb/` directory containing `sandbox.db`, with a single branch `main` and a journal seeded with a random repository ID. The database file is created with `0600` permissions (private to your user). Fails if a repository already exists here; explains itself if it finds a pre-1.0 loose-file `.sb/`.

### `sb status [--deep]`

Shows the current branch, the latest save, and every change relative to it: `renamed old → new`, `new`, `modified`, `deleted`. In shared mode, also shows who holds which locks. Fast even on large trees thanks to the stat cache (Section 12); `--deep` bypasses the cache and re-hashes every file.

### `sb save "<message>" [--allow-secrets] [--no-verify] [--global-force]`

Snapshots every tracked file as a new save on the current branch. The message is **required** — history without messages is archaeology. In order, `save`:

1. Diffs the working tree against the last save; if nothing changed, no save is created.
2. Runs the **secret scanner** over every file being added or modified (Section 8). Findings block the save; `--allow-secrets` overrides deliberately.
3. Runs **pre-save test gates** in a clean checkout of the exact candidate tree (Section 7). Failures block the save; `--no-verify` overrides.
4. Re-checks that the worktree didn't change while being scanned and tested; refuses rather than commit an untested state.
5. Stores the blobs, builds the tree, writes the commit, moves the branch tip, and journals the whole thing — as one atomic transaction. Any bypass flags are journaled with it.

In shared mode, `save` commits only *your* files (Section 6); `--global-force` deliberately sweeps in everyone's edits instead.

### `sb log [-n <count>]`

Save history for the current branch, newest first: hash, date, author, message, a `(merge)` marker — and a change summary per save (`+2 new · ~1 modified · old.txt → new.txt`), so the log shows what each save *did*, not just what it said. `-n 5` (long: `--limit`) shows only the newest five.

### `sb diff [<path>]`

Unified, colorized diff between the working folder and the last save. Renames show as a single `@@ old → new  renamed (content identical)` line. Binary files show as a one-line size summary, never as raw bytes. With `<path>`, limits output to that file or everything under that folder.

### `sb undo [-p <path>]`

Reverts the effect of the latest save **by creating a new save** whose content equals the previous one. History is never rewritten and nothing is deleted — the "undone" save remains fully in the log and journal. Running `sb undo` again redoes. Requires a clean working tree, so it can never eat uncommitted work.

With `-p <path>` (long: `--path`), it instead brings just that file — or everything under that folder — back from the last save, overwriting the working copy with the saved version and its permissions. No new save is created, since only your uncommitted working copy changes. These writes go through the same symlink-safe, atomic machinery as checkout: a symlink squatting on the path (or a symlinked parent) can never redirect the restore outside the repository.

### `sb restore <anchor | save | release-label | branch>`

Returns the current branch to any past state — **by creating a new save** whose content equals that state, exactly like `undo` but to an arbitrary point. Nothing is rewound and nothing is deleted; the restore itself is a journaled, chain-linked operation, and `sb undo` immediately afterward takes you straight back. Requires a clean working tree.

The target can be named four ways: an **anchor** (8–64 hex characters of a journal chain link — resolves to the current branch's tip *as the journal recorded it at that moment*), a **save-hash prefix** from `sb log` (4+ characters, unique), a **release label** (`sb restore rel-3` puts the branch back to exactly what shipped), or a **branch name** (its current tip's content). Ambiguous targets are rejected with what matched, never guessed.

### `sb branch [<name>] [-r]`

With no argument, lists branches, marking the current one. With a name, creates a branch pointing at the current save. With `-r` (long: `--remove`), deletes the named branch's *pointer* — never the current branch, and never the last one. The saves it pointed at stay in the store and journal, and `verify` keeps re-checking them.

### `sb switch <branch>`

Moves to a branch: rewrites the working folder to match its latest save and updates the current-branch pointer. **Refuses to run with unsaved changes.** File writes are atomic (exclusive random temp + rename + fsync) through no-follow parent descriptors; directories emptied by the switch are pruned.

### `sb merge <branch> [--no-verify] [-i]`

Three-way merge of `<branch>` into the current branch, using their best common ancestor as the base (true lowest-common-ancestor computation — correct even after prior merges; criss-cross histories resolve deterministically and conservatively):

- If the current branch is an ancestor of the target, this is a **fast-forward**: the tip simply moves (still gated by pre-merge tests).
- Files changed on only one side take that side automatically.
- Files changed on **both** sides get a **line-level three-way merge**: non-overlapping edits combine automatically. Overlapping edits are conflicts.
- On conflict, the merge **stops before touching anything**: your working folder is left exactly as it was, and sb lists each conflicting file with the reason. Reconcile the file on one branch, save, and merge again. sb never leaves you in a half-merged state.

The algorithm is deliberately conservative: adjacent-line edits, same-point insertions, CRLF files, binaries, and files without a trailing newline are reported as conflicts rather than guessed or rewritten. A false conflict costs you a minute; a false merge costs you a bug.

Before the merged result is committed, **pre-merge test gates** run against the *merged tree itself*.

With `-i` (long: `--ignore`), in shared mode: skip files locked by others (Section 6). A merge that skipped files is recorded as **partial** — a single-parent save, not a merge commit — so re-running the merge after the locks release genuinely brings in the skipped files. sb never records ancestry it didn't actually merge.

### `sb test [<stage>]` / `sb test new <stage> <name>` / `sb test list` / `sb test guide`

Run gates manually against your current working tree (all stages, or one of `pre-save`, `pre-merge`, `pre-publish`), scaffold a new test script from a template, list discovered tests, or print the self-contained walkthrough. Section 7 has the long-form version.

### `sb publish [<label>]` / `sb publish list` / `-l` / `--no-verify`

Records the current save as a release, behind two gates:

1. **Full store verification** — the entire `sb verify` battery. sb refuses to publish from a damaged or tampered store.
2. **Pre-publish tests** on a clean checkout of the exact tree being released.

Passing both writes a `publish` entry into the hash-chained journal: what was released, from which branch, by whom, when — including the content hashes of the gate scripts that ran, so the record says exactly what was checked. `sb publish -l` shows all records and reports whether the chain that protects them still verifies.

A release is a *record*; to get the files of a released version back out, use `sb export`.

### `sb verify [-a <hash>]`

The "is everything intact?" button. It re-hashes **every object in the store** (including history kept from removed branches and orphans left by interrupted operations), validates every tree entry name, recomputes the **entire journal hash chain**, and cross-checks every **branch tip against the journal** — catching refs that were moved, deleted, *or injected* outside sb. Malformed objects, unreadable journal rows, and unexpected refs are all reported as findings, never crashes. With `-a <hash>` (long: `--anchor`), additionally confirms a previously noted anchor is still part of history (Section 10).

Exits `0` when everything agrees, `2` with a precise list of problems otherwise.

```
checked 18 objects across 5 save(s)
  ├─── content hashes  all valid ✓
  ├─── journal chain   11 entries linked ✓
  ├─── branch tips     match the journal ✓
  └─── anchor          8977ecba8bd79985  (save it · check later: sb verify -a <hash>)
history is intact ✓ — store, journal and refs all agree
```

### `sb journal [-n <count>]`

The append-only operation log, with meaningful detail for **every** operation type: ref moves with old → new hashes and any bypass flags, releases, switches, branch removals, lock releases (with `· forced` when `--force` was used), ignore rules, shared/durability changes, identity registrations, and pack/export/unpack events. Ends by re-verifying the chain. This is the answer to "what actually happened in this repository?"

### `sb info`

One-screen repository overview: store location and size, current branch, object counts, journal length, the current anchor, and how your saves will be attributed.

### `sb who [<name>] [<email>]`

Shows — or, with arguments, sets — how saves are attributed, stored in `~/.config/sandbox/profile.json` (override location with `SB_HOME`; override values per-command with `SB_NAME` / `SB_EMAIL`). This is **attribution for humans reading history, not authentication** (Section 9).

### `sb shared [on|off]`

Shows or sets **shared mode** — per-file locks so a team can work in one repository directly (Section 6). The change is journaled.

### `sb durability [full|normal]`

Shows or sets crash durability. `full` (the default): the newest committed transaction survives OS crash and power loss, at a small write cost. `normal`: faster; still crash-safe, but may lose the most recent commit on power loss. The change is journaled.

### `sb locks`

Shows all shared-mode file locks: who holds what, since when, and when each expires.

### `sb unlock [<path>...] [--force]`

Releases your locks (all of them, or the named paths). `--force` releases locks held by *others* — a deliberate action, journaled with the paths, the prior owners, and the fact it was forced.

### `sb ignore <pattern>`

Appends a pattern to `.sbignore` (Section 13). Journaled.

### `sb pack [<output>] -k <passkey> [-f]`

Seals the entire repository into a single encrypted `.sbox` archive (Section 11). The output defaults to `<foldername>.sbox`; an existing file is never overwritten, and the archive is written via an exclusively-created randomized temp file, then atomically renamed — a pre-planted symlink cannot redirect it. Warns if you have unsaved changes, since pack seals *saved* history. The pack event is journaled.

With `-f` (long: `--files-only`), the archive holds just the current save's files — no history, no journal.

### `sb unpack <file.sbox> [<destination>] -k <passkey> [-f] [-i]`

Restores a `.sbox` archive. A wrong pass-key or an altered archive fails cleanly and writes nothing. For a full-repository archive, the store is first restored into a **private staging area and verified end-to-end** — every object re-hashed, the journal chain recomputed, refs cross-checked — and only if everything agrees is it installed and checked out. A damaged or hostile archive is refused *before* a single byte lands in the destination:

```
unpacked my-project · 3 file(s)
  ├─── sealed by  Jordan <jt@noct.gg>  · 2026-07-14 08:35
  ├─── branch     main · anchor bd40a7878f681649
  └─── verified before install ✓ — store, journal and refs all agree
```

**The destination must be fresh or empty**: if it already contains *anything*, unpack refuses. With `-i` (long: `--ignore`), it **merges into an existing, non-empty destination** instead: files at the same path are overwritten with the archive's version, everything else is kept. This is the flag for redeploying a release over a previous drop. There is no per-file backup — point `-i` at the right folder.

With `-f` (long: `--files-only`), writes only the native files — no `.sb` directory. Archives packed with `--files-only` unpack this way automatically. The unpack event is journaled in the installed repository.

### `sb export <version> [<destination>] [-k <passkey>]`

Materializes any version — a **release label**, a **branch name**, or a **save-hash prefix** — as plain files, with no `.sb` directory and executable bits preserved. The destination defaults to `<repo>-<version>/` and must be empty; export is read-only against your repository, every blob is re-hash-verified on the way out, and the export is journaled.

With `-k <passkey>`, it instead produces an encrypted, files-only `.sbox` release artifact carrying the label, commit, and sealed-by metadata — ready to ship and drop with `sb unpack <file.sbox> /path -k <passkey>` (redeploying over a previous drop takes `-i`).

### `sb version` / `sb selftest`

`sb version` (also `-V` / `--version`) prints `sb 1.2 · jts.gg/sandbox`. `sb selftest` runs the built-in adversarial suite — 20 checks covering atomic rollback, symlink and path escapes, mid-gate mutation, CRLF merge safety, concurrent-save races, corruption detection in removed-branch history, archive crypto, the stat cache, and the full shared-mode locking protocol.

---

## 6. Shared mode: one repo, many hands

Shared mode lets a small team point at **one repository** — one folder, one database — without clone/push/pull. Coordination is by per-file locks. Turn it on once per repository:

```bash
sb shared on
```

### The rules

- **Editing a file auto-locks it to you.** sb has no daemon; locks are detected lazily the next time any sb command scans the tree — but deterministically, and attributed correctly (below).
- **A lock lasts until you `sb save`, or one hour passes** (tune with `SB_LOCK_TTL`). On expiry, your edits are **auto-saved as a commit in your name** — never lost — and the lock frees, so an abandoned lock can't block the team.
- **While you hold a lock, only you may save that file.** `sb save` commits *your* locked/changed files and leaves everyone else's in-progress edits untouched, both on disk and in the commit. `sb save --global-force` deliberately sweeps in everyone's edits (journaled as such).
- **A merge that would change a file someone else has locked is refused** — it can't clobber their in-progress work. `sb merge <branch> -i` skips those files: the merge proceeds for everything else, each locked file keeps your current version, and the merge is recorded as **partial** (a single-parent save, not a merge commit), so re-running the merge after the locks release genuinely brings the skipped files in. sb never records ancestry it didn't actually merge.
- **`sb locks` shows who holds what; `sb unlock` releases yours; `sb unlock --force` releases someone else's** — journaled with the prior owner's name.

### Who gets the lock — attribution done honestly

A file on disk carries no note of who edited it, so a lock claimed at scan time must not simply go to whoever ran the scan (Alice edits, Bob runs `sb status` — Bob must **not** become the owner of Alice's edit). The one signal a shared folder provides is the file's **owner uid**: each teammate writes as their own OS account. So sb keeps a uid → identity registry — every sb command anyone runs in shared mode teaches the store which OS account maps to which `sb who` identity — and locks each discovered edit to the account that **owns the file**. Someone who has never run sb still gets correct locks under their system account name. Expired-lock auto-saves are likewise committed under the *real* editor's name.

Where the signal doesn't exist — deletions (nothing left to stat) and uid-squashing network mounts — attribution falls back to the invoking user, as stated, not as a surprise. One editor-dependent nuance: editors that save atomically (write-temp-then-rename — most IDEs) make the editor the file's owner, so attribution is exact; an editor that truncates in place leaves the original creator as owner. New files always attribute correctly. Saving promptly closes the window either way.

### Where shared mode belongs — stated plainly

Shared mode is designed for **one machine with multiple user accounts, or a directly attached shared disk**. The store is SQLite in WAL mode, and SQLite's own documentation is blunt that WAL does not work reliably over NFS or SMB network mounts — file locking on network filesystems is broken in ways no application can paper over. If your "shared drive" is a network mount, treat shared mode as unsupported there; use `.sbox` archives to move work between machines instead. A tool that hides this caveat would be selling you corruption.

---

## 7. Test gates: quality enforcement

Test gates are how sb turns "we should run the tests" into "the tests ran, or it didn't happen."

### How it works

Put executable scripts in these folders — they are ordinary tracked files, so they version, branch, and merge with your code:

```
sb-tests/
  pre-save/       runs before every save (shared-mode saves included)
  pre-merge/      runs before every merge (including fast-forwards)
  pre-publish/    runs before every release
```

Scripts run **sorted by name** (use prefixes: `10-lint.sh`, `20-unit.py`, `30-build.sh`), each inside a **pristine temporary checkout of the exact candidate tree**. That last part is the point:

- A pre-save gate sees your candidate files in a clean directory — nothing ignored or untracked leaks in. In shared mode, it sees the exact merged tree your partial save will produce.
- A pre-merge gate sees **the merged result** — and discovers its scripts *from the merged tree*, so a merge that changes the tests runs the new tests.
- A pre-publish gate sees exactly the tree being released, and the release record carries the content hashes of the scripts that ran.

Each script gets these environment variables and runs with the checkout root as its working directory:

| variable | meaning |
|---|---|
| `SB_STAGE` | `pre-save`, `pre-merge`, or `pre-publish` |
| `SB_BRANCH` | the current branch |
| `SB_COMMIT` | the candidate save hash, or `(worktree)` |
| `SB_REPO` | absolute path to the real repository root |

**Exit 0 passes. Anything else — or exceeding the timeout (default 120s per script, tune with `SB_TEST_TIMEOUT`) — blocks the operation** (exit code `2`) and prints the script's last 15 lines of output. `--no-verify` overrides deliberately, visibly, and *auditable-ly*: the bypass is written into the save's journal entry and shown by `sb journal`.

`.py` scripts run under the same Python as sb; executables run directly; anything else runs under `sh`.

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

Run gates manually anytime: `sb test` (all stages), `sb test pre-merge` (one), `sb test list` (what exists).

### Philosophy

Keep pre-save gates **fast** (seconds: syntax, lint, quick unit tests) so saving stays frictionless. Put slow suites at pre-merge or pre-publish. A gate that takes ten minutes at pre-save will train you to type `--no-verify`, and a gate everyone overrides is worse than no gate.

---

## 8. The secret scanner

The most common irreversible mistake in version control is committing a credential. History is permanent; rotating a leaked key is an incident, not an edit. sb blocks this **at save time**, scanning every file being added or modified for:

- AWS access keys (`AKIA…` / `ASIA…`)
- Private key blocks (`-----BEGIN … PRIVATE KEY-----`, including RSA/EC/OpenSSH/DSA/PGP)
- GitHub tokens (`ghp_…`, `gho_…`, and friends)
- Slack tokens (`xoxb-…` etc.)
- Google API keys (`AIza…`)
- Stripe live keys (`sk_live_…`, `rk_live_…`)
- JWTs
- Generic assignments like `password = "…"` or `api_key: '…'` with long quoted values

Findings block the save (exit `2`) with the file, line number, and pattern name. Your options, in order of preference:

1. **Remove the secret** — load it from the environment or an ignored config file instead.
2. **Ignore the file** — `sb ignore .env`, then save.
3. **Override** — `sb save "msg" --allow-secrets`, when you're certain it's a false positive. The override is journaled.

The scan applies to shared-mode saves identically. Binary files and files over 1 MB are skipped. Honest caveats: pattern matching catches well-known credential *shapes*, not every secret, and only files touched by the current save are scanned. Treat the scanner as a seatbelt, not a substitute for keeping secrets out of tracked files.

---

## 9. Security model and threat model

sb makes exactly three promises. Each is stated with its mechanism, what it defends against, and what it does not. A security feature you can't state precisely is a decoration.

### Promise 1 — Integrity: *what you get back is what you put in*

**Mechanism.** Every object is stored under the SHA-256 hash of its content, and re-hashed on **every read**, not just during `verify`. Every save embeds its tree hash and parent hashes, so each save transitively fixes the exact bytes of every file in it and every save before it. Every operation commits as **one SQLite transaction** (WAL mode, `synchronous=FULL` by default): a save's objects, branch move, and journal entry land together or not at all. Working-folder writes use exclusive randomized temp files, fsync, and atomic rename, through no-follow parent directory descriptors — this applies to checkout, switch, merge, restore, *and* `undo -p`. A crash between the database and the working folder surfaces as ordinary unsaved changes in `status` — never as corruption or a false tamper report.

**Defends against:** disk corruption, torn and partial writes, power loss mid-operation, truncated or bit-flipped objects, malformed objects that hash correctly but don't parse, a crafted tree attempting path traversal on checkout, and symlinks (pre-existing or planted) attempting to redirect any write outside the repository.

**Does not defend against:** loss of the database file itself. Integrity detection is not a backup — see the FAQ.

### Promise 2 — Tamper evidence: *changes made behind sb's back are detectable*

**Mechanism.** The hash-chained journal. Every operation appends an entry whose link is `SHA-256(canonical entry ‖ previous link)`, rooted in a random per-repository ID. `sb verify` recomputes the entire chain and cross-checks every branch tip against the journal's record — flagging tips that were *moved*, branches that were *deleted*, and refs that were *injected* outside sb. Gate bypasses are part of the chained record.

**Defends against:** editing or deleting journal entries; manipulating refs via direct database access in any direction; replacing objects; accidental or casual malicious modification by anything that isn't sb.

**Does not defend against — stated plainly:** an attacker with **write access to the database and knowledge of sb's format** can rewrite the *entire* store into a new, internally consistent history. With no secret material anywhere, internal consistency is recomputable by anyone. This is inherent to keyless designs, and sb closes the gap the honest way: with **anchors** (Section 10). What sb refuses to do is ship the *appearance* of cryptographic authenticity without the key-management reality that makes it meaningful.

### Promise 3 — Leak prevention: *credentials are stopped before they enter permanent history*

**Mechanism.** The commit-time secret scanner (Section 8), on by default, overridable only explicitly, with overrides journaled.

**Defends against:** the accidental commit of recognizable credentials.

**Does not defend against:** unrecognizable secrets, or secrets already present in older saves.

### What sb deliberately does not claim

- **Confidentiality of the store.** The database is created `0600`, and full-disk encryption is the right layer for confidentiality at rest. (`.sbox` archives *are* encrypted — that's transport and cold storage, Section 11.)
- **Authentication.** `author` on a save is attribution for humans. sb says so out loud rather than implying otherwise. Shared mode's uid-based lock attribution improves *accuracy* of attribution; it is still not authentication.
- **Access control.** sb is a local tool; the operating system's file permissions are the access control.

### Why removing cryptography made this design *stronger*

An earlier design signed every commit with Ed25519, falling back to a hand-rolled pure-Python implementation. A senior security review kills that: hand-rolled signature code is an implementation-vulnerability magnet; keys with no management story prove only that *some* key signed something, which is theater; and theater is worse than absence, because users calibrate trust to the claim, not the mechanism. The same reasoning was applied again in 1.2: the embedded encryption module once carried an unused "asymmetric" mode whose construction did not deliver public-key security — it was deleted outright rather than documented around. The current design offers fewer guarantees and delivers all of them.

---

## 10. Anchors: pinning history outside the machine

The one attack a keyless store cannot detect internally is wholesale rewrite (Promise 2's stated limit). Anchors close it with nothing but a hash and a second location.

Every `sb verify` (and `sb info`) prints the current **anchor** — a copy-paste-ready 16-character prefix of the latest journal entry's chain link:

```
  └─── anchor          67b3dea8b260c12a  (save it · check later: sb verify -a <hash>)
```

Copy that value anywhere outside the machine: a note on your phone, a message to a colleague, a printed line in a logbook. Later, paste it straight back:

```bash
sb verify -a 67b3dea8b260c12a
```

Sixteen hex characters is 64 bits — trivially short to jot down, yet computationally infeasible for a forged journal entry to collide with. Any 8–64 hex prefix of a chain link is accepted.

If the anchor is a link in the current chain, everything up to that moment is exactly as it was when you noted it. If it is **not** found, the journal you noted is not the journal on disk: history was replaced wholesale, and no internally consistent rewrite can hide it, because the attacker cannot alter what's written in your notebook.

Anchors also work as **bookmarks**: `sb restore <anchor>` puts the current branch's content back to exactly the state that anchor witnessed — non-destructively, as a new save.

---

## 11. Portable archives (.sbox)

`sb pack` seals the whole repository into one **encrypted, self-describing archive** — a `.sbox` file — safe to email, drop in cloud storage, hand off on a USB stick, or archive for cold storage.

```bash
sb pack -k "my-strong-pass-key"                   # -> <foldername>.sbox
sb pack release.sbox -k "my-strong-pass-key"      # choose the output name
```

Add `-f`/`--files-only` to seal just the current save's files with no history.

To restore — on any machine with sb, fully offline:

```bash
sb unpack my-project.sbox -k "my-strong-pass-key"             # -> ./my-project/
```

For a full-repository archive, the store is restored into a **staging area and verified end-to-end first** — objects re-hashed, journal chain recomputed, refs cross-checked. Only if everything agrees is it installed; a damaged or tampered archive is refused before a single byte lands in the destination, and the output says so: `verified before install ✓`. **All history survives** — every save, every branch, the full journal chain — and the unpack itself is journaled in the installed repository.

Unpacking requires a **fresh or empty** destination; `-i`/`--ignore` merges into a non-empty folder deliberately (redeploys), overwriting matching paths and keeping everything else.

### What's inside a .sbox

Every archive carries an encrypted **manifest** alongside the store:

| Field | Meaning |
|---|---|
| `created` / `created_by` | when the archive was sealed, and by whom |
| `repo_name` | the original folder name (the default unpack destination) |
| `branch` | the branch that was current at pack time |
| `chain_head` | the journal chain head at pack time — usable as an [anchor](#10-anchors-pinning-history-outside-the-machine) |
| `sb_version` / `sbox_version` / `repo_id` | versions and the repository's stable ID |
| `db_sha256` / `db_size` | integrity check for the sealed payload, verified before anything is written |

The manifest lives *inside* the encrypted blob, so an archive reveals nothing — not the author, not the branch, not the file names — to anyone without the pass-key. The only cleartext is the small header (`SBOX`, a format byte, and a random 16-byte per-archive salt), which is cryptographically bound to the ciphertext as authenticated data, so it cannot be altered or swapped without detection.

### The encryption: vox

sb's *integrity* model uses no cryptographic keys — that remains a deliberate, load-bearing property (Section 9). Archive *confidentiality* is different: `pack`/`unpack`/`export -k` use [**vox**](https://jts.gg/vox) (v1.7.3, symmetric core), a small single-file encryption module embedded inside sb and loaded into memory only for the duration of those commands — no separate file, no install step, no network. The unused legacy asymmetric interface was removed from the embedded copy in 1.2.

vox provides a misuse-resistant authenticated cipher (an SIV-style AEAD built on HMAC-SHA512, with PBKDF2-HMAC-SHA512 key stretching at 300,000 iterations). Since sbox format v2, a random per-archive salt is mixed into key derivation, so two archives sealed with the same passphrase use different keys and a password guess cannot be amortized across archives. Two consequences matter in practice:

- **Wrong pass-key or a single altered byte → the archive will not open.** vox verifies authenticity *before* it decrypts. sb adds a second check by re-hashing the recovered payload against `db_sha256` — and for full-repo archives, the staged verification battery on top.
- **Your pass-key is the only thing standing between the archive and its contents.** There is no recovery, no backdoor, and no key file. A weak pass-key is a weak archive.

Archives are written whole and held in memory while sealing/opening — comfortable to a few GB, not a streaming format (Section 20).

---

## 12. The storage format

The entire repository is **one SQLite database**: `.sb/sandbox.db`, in WAL mode, created `0600`, `synchronous=FULL` by default (`sb durability normal` trades the last-commit-on-power-loss guarantee for speed; the change is journaled).

- **Crash safety.** Every sb operation commits as a single ACID transaction: all of it lands, or none of it does. (Precedent: Fossil, the VCS written by SQLite's own author, made the same bet fifteen years ago.)
- **No small-file sprawl.** sb is one file: `cp` is a valid backup, `rsync` sees one changed file, and there is nothing to "pack."
- **Real queries.** Prefix resolution is an indexed `LIKE`; statistics are one `GROUP BY`; the stat cache and locks are tables.
- **Auditable by anything.** The format is inspectable with the world's most widely deployed database tooling — and anything you change with that tooling behind sb's back, `verify` flags.

### Schema

| table | contents |
|---|---|
| `meta` | key/value: `format` version, random `repo_id` (chain root), current `branch`, settings, uid → identity registry |
| `objects` | `hash → kind, size, zlib(data)` — the content-addressed store |
| `refs` | `name → commit hash` — branch tips (empty string = branch with no saves) |
| `journal` | `seq, ts, op, detail(JSON), prev, link` — the append-only hash chain |
| `statcache` | `path → size, mtime, ctime, inode, hash` — change detection without re-reading |
| `locks` | `path → owner, email, since, base` — shared-mode per-file locks |

### Object encodings

An object's hash is `SHA-256("<kind> <length>\0" + data)`. Trees and commits are **canonical JSON** (sorted keys, no whitespace). A tree is `[[mode, kind, hash, name], …]` sorted by name; a commit is `{tree, parents, author, email, time, message}`. Modes are `100644` (file), `100755` (executable), `040000` (directory).

### The stat cache

`status` and `save` detect changes by comparing each file's size, mtime, **ctime, and inode** against the cache; on a full match, the previous hash is reused and the file is never read. mtime alone can be forged or restored (`touch -d`, archive extraction), but ctime is kernel-maintained and the inode changes when an editor replaces a file — so a same-size edit with a restored mtime still misses the cache and gets re-read. Files touched within the last two seconds always bypass the cache; during a save a cached hash is only trusted if the blob actually exists in the store; `--deep` bypasses the cache entirely. A cache miss only costs a re-read — the cache fails toward correctness, never away from it.

### What write paths guarantee

Tree entry names are validated on read (no `/`, `\`, NUL, `.`, `..`, empty, or `.sb`), so a hostile tree cannot write outside the repository. Every worktree write — checkout, switch, merge, restore, `undo -p`, archive extraction — goes through a parent directory opened with no-follow semantics, an exclusively-created randomized temp file, a complete-write loop, fsync, and atomic rename. Archive outputs (`pack`, `export -k`) use the same exclusive randomized temp + rename discipline. Directory pruning never touches `.sb`.

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

`sb ignore <pattern>` appends for you (and journals it). Always ignored regardless of `.sbignore`: `.sb` itself, `*.sbox` archives, `.git`, `node_modules`, `__pycache__`, `*.pyc`, `.DS_Store`. The `.sbignore` file itself is tracked, so ignore rules travel with branches.

Two behaviors worth knowing — the second is a real footgun: ignored files are invisible to `save`/`status` but never deleted by sb; and **ignoring an already-tracked file makes it show as `deleted`**, so the next save removes it from the tree (the file stays on disk, but leaves history going forward). If you mean "stop tracking but keep," that's exactly what happens; if you didn't mean it, check `status` before saving.

---

## 14. Everyday workflows

**Solo project, straight line.** `sb init`, then work/`sb status`/`sb save` in a loop. Add a pre-save syntax gate (`sb test new pre-save 10-syntax`) on day one.

**Safe experiment.** `sb branch spike && sb switch spike`, hack freely with saves as checkpoints. If it works: `sb switch main && sb merge spike`. If it doesn't: switch back and simply never merge.

**"I broke it ten minutes ago."** `sb diff` to see the damage; `sb undo -p <file>` to reclaim one file from the last save; `sb undo` to revert the whole last save (non-destructively).

**"I deleted everything."** Nothing saved is ever lost: `sb save "oops"` (yes — save the wipe), then `sb undo`. The deletion and its reversal both live in history, which is the point.

**"It worked last Tuesday."** Find last Tuesday — an anchor you noted, a save in `sb log`, or a release label — and `sb restore <it>`.

**Small team, one machine or one attached disk.** `sb shared on`. Everyone works in the same folder; editing a file locks it to the *actual editor* (by file ownership); `sb save` commits only your files; `sb locks` shows who holds what; expired locks auto-save in their owner's name. Merges refuse to clobber locked work; `sb merge feat -i` proceeds around it as a recorded partial merge and completes on re-merge.

**Release with a paper trail.** Keep the real suite at `sb-tests/pre-publish/`. Ship with `sb publish v1.4`: sb verifies the entire store, runs the suite against a clean checkout of exactly what's shipping, and journals the record — including the content hashes of the gate scripts that ran. `sb publish -l` is your release history, protected by the chain.

**Deploy to a production filesystem.**

```bash
# on your machine
sb publish v1.4                    # gates + journaled record
sb export v1.4 -k "release-key"    # -> myapp-v1.4.sbox (encrypted, files only)
scp myapp-v1.4.sbox server:

# on the server — first deploy into a fresh folder
sb unpack myapp-v1.4.sbox /srv/www/myapp -k "release-key"

# every deploy after that merges over the previous drop
sb unpack myapp-v1.5.sbox /srv/www/myapp -k "release-key" -i
```

Rolling back is `sb export <older-label>` and the same `-i` drop.

**Weekly trust ritual.** `sb verify`, copy the 16-character anchor next to the date somewhere off-machine. Thirty seconds; afterward, no rewrite of any history before that moment can escape `sb verify -a <hash>`.

---

## 15. sb versus git

| | **sb** | **git** |
|---|---|---|
| Mental model | work → save | work → stage → commit (+ index states) |
| Staging area | none — a save is what you see | the index, with its own command set |
| Detached HEAD | impossible | routine source of confusion |
| Destroying history | no command does it | `reset --hard`, `push -f`, dropped stashes, expired reflog |
| Undo | `sb undo` — a new save, reversible | `revert` vs `reset` vs `restore` vs `checkout` |
| Repository format | one crash-safe SQLite file, ACID everything | thousands of loose files + packfiles + refs + index |
| Operation audit log | hash-chained journal of **every** operation, bypasses included, cross-checked vs refs | reflog: per-machine, expiring, mutable, unchained |
| Tamper evidence | chain + tip cross-check + external anchors | commit DAG only; refs/reflog unprotected |
| Secret prevention | built into every save, overrides journaled | third-party hooks you must install |
| Test enforcement | versioned gates on clean checkouts, on by default | hooks: unversioned, per-clone, easily absent |
| Renames | exact-content detection in status/diff/log | similarity-based detection (incl. edits), rename-aware merges |
| Merge conflicts | auto-merge non-overlap; on conflict, stops cleanly, worktree untouched | conflict markers + in-progress merge state to manage |
| Small-team sharing | shared mode: one repo, per-file locks, honest attribution | clone/push/pull, remotes |
| Remotes / distributed collaboration | not yet (roadmap) | git's core strength |
| Platform | Linux / macOS / WSL | everywhere |
| Ecosystem | one file, zero deps | vast |

The honest summary: **git is a distributed collaboration system that you can also use alone; sb is a personal-and-small-team safety-and-integrity system, designed for the way individuals actually work.** If you need GitHub-style multi-party collaboration today, use git — possibly with sb alongside it (they coexist fine; sb ignores `.git`, add `.sb` to `.gitignore`).

---

## 16. Environment variables

| variable | effect | default |
|---|---|---|
| `SB_NAME` | attribution name for saves (overrides `sb who`) | profile, else OS username |
| `SB_EMAIL` | attribution email | profile, else `<name>@local` |
| `SB_HOME` | folder for the global profile | `~/.config/sandbox` |
| `SB_TEST_TIMEOUT` | seconds allowed per test script | `120` |
| `SB_LOCK_TTL` | seconds a shared-mode lock lives before auto-save + release | `3600` |

Inside test scripts, sb additionally exports `SB_STAGE`, `SB_BRANCH`, `SB_COMMIT`, `SB_REPO` (Section 7).

---

## 17. Exit codes

| code | meaning |
|---|---|
| `0` | success |
| `1` | usage or state error (not a repo, unsaved changes, unknown branch, bad arguments, non-empty unpack destination, corrupt object hit mid-operation, file-system error, …) |
| `2` | a **gate** stopped you: secrets found, test gates failed, merge conflicts, or `verify` found problems |
| `130` | interrupted (Ctrl-C) |

The `1`/`2` split is script-friendly: `2` always means "sb worked correctly and is protecting you," so automation can distinguish "fix your command" from "fix your content."

---

## 18. FAQ

**Where did the signatures go? Is sb less secure now?**
The Ed25519 signing was removed on purpose, and Section 9 explains why in full: keys with no management story prove nothing, and a hand-rolled fallback implementation is a liability. Every property the signatures *actually* delivered — integrity and tamper evidence — is preserved by content re-hashing, the journal chain, the tip cross-check, and anchors. The same standard removed the embedded encryption module's unused asymmetric mode in 1.2.

**Is SHA-256 "cryptography"? I asked for none.**
It's a hash function from Python's standard library used as a content fingerprint — no keys, no signatures, no third-party crypto code. Remove hashing itself and content addressing, integrity checking, and the journal all cease to exist.

**How do I back up a repository?**
Copy `.sb/sandbox.db` (any time sb isn't mid-command; WAL makes even that forgiving), or copy the whole project folder, or `sb pack` for an encrypted single-file backup. After restoring, run `sb verify`.

**Can I have partial commits, like `git add -p`?**
No, by design: a save is exactly your working tree, which is what makes "the tests passed on the save" meaningful. Two unrelated changes belong on two branches, or in two saves in sequence. (Shared mode's "your files only" save is the one principled exception, and it exists to protect *other people's* in-progress work.)

**What about large or binary files?**
They're stored (zlib-compressed) and versioned like anything else; `diff` summarizes them in one line and the secret scanner skips them. Every version of a large file is retained in full — no delta compression yet — so a repo of frequently-changing gigabyte assets will grow accordingly.

**Does rename detection catch a file I moved *and* edited?**
No — detection is exact-content only, on purpose. A moved-and-edited file shows as an add plus a delete. sb reports what it can prove; similarity guessing is on the roadmap, honesty first.

**Symlinks?**
Skipped with a visible note, not silently, and not yet tracked. (Symlinks *pointing into* your repo can't hijack sb's writes either — every write path refuses to follow them.)

**Can two commands run at once?**
Yes, safely. SQLite serializes writers; racing saves are protected by compare-and-swap on the branch tip plus a worktree drift check, so a race ends in one clean success and one loud, safe "run it again" — never corruption. Verified by `sb selftest`.

**Does anything leave my machine?**
No. No network code exists in sb — no telemetry, no phoning home, nothing.

**Can I rename a branch?**
Not built in yet; today it's `sb branch new-name && sb branch old-name -r` (from another branch). Removal is journaled and never deletes saves.

**Can `unpack -i` be undone?**
No — `-i` overwrites files in place, with no per-file backup. That's why the flag exists: without it, unpack refuses any non-empty destination, so overwriting is always a decision you typed.

**Who ends up owning a lock if Bob's `sb status` discovers Alice's edit?**
Alice. Locks are attributed to the file's owner on disk, resolved through the uid → identity registry — not to whoever happened to run sb first. Section 6 has the full story, including the honest edge cases.

---

## 19. Troubleshooting

**`error: not inside a sandbox repository`** — you're outside any folder containing `.sb/sandbox.db`; `cd` in, or `sb init`.

**`error: sb <command>: unrecognized arguments: …` / `missing: …`** — the flag or argument doesn't exist for that command; the correct usage line is printed right below.

**`error: you have unsaved changes`** — `switch`, `merge`, `undo`, `restore`, and `publish` refuse to run over uncommitted work, always. `sb save "wip"` (saves are cheap and undo is free), or `sb undo -p <path>` for changes you want gone.

**`merge blocked — it would change files locked by others`** — shared mode protecting a teammate's in-progress edit. Wait, ask them to save, or `sb merge <branch> -i` to proceed around those files as a recorded partial merge.

**`error: <folder> is not empty — unpack into a fresh folder`** — unpack never writes into a destination that already contains anything. Pick a fresh folder, or add `-i`/`--ignore` to overwrite matching files deliberately.

**`the archive's repository failed verification — nothing was written`** — the store inside the `.sbox` is damaged or was tampered with; unpack refused before touching the destination. Get a good copy of the archive.

**`save blocked — possible secrets detected`** — Section 8. Remove the secret, ignore the file, or `--allow-secrets` if you're sure (journaled).

**`pre-save tests failed — save blocked`** — the failing script's last 15 lines are printed above. Reproduce with `sb test pre-save`. Override once with `--no-verify` (journaled) — then fix the gate.

**`merge stopped — these files conflict`** — the reason is printed per file; your worktree was not touched. Reconcile on one branch, save, merge again.

**`object … does not match its hash` / `verify` reports problems** — real corruption or tampering; sb stopped rather than propagating it. Restore `.sb/sandbox.db` from a backup, then `sb verify`. Undamaged files can be rescued first via `sb undo -p` / `sb export` from saves whose objects are intact.

**`branch '…' changed under this operation … run the command again`** — two sb commands raced; yours lost the compare-and-swap, nothing was changed, and re-running is safe.

**`store error: … database is locked`** — another sb command has the database open for writing. Wait a moment and retry.

**`file system error: …`** — a permission or disk problem outside sb's control, reported cleanly instead of a stack trace.

**A file isn't being saved** — it matches an ignore rule. Check `.sbignore` and the built-in defaults (Section 13); note symlinks are skipped with a printed note.

---

## 20. Known limitations and roadmap

Stated plainly, because a tool that hides its edges isn't trustworthy:

- **POSIX only.** Linux, macOS, WSL. The symlink-safe write machinery relies on directory descriptors Windows doesn't provide; native Windows support would mean weakening guarantees, so it waits until it can be done right.
- **No live remotes yet.** Moving repositories between machines is done with encrypted `.sbox` archives — deliberate and secure, but manual. Journal-first sync is the top roadmap item.
- **Shared mode wants a local disk.** SQLite WAL is not reliable over NFS/SMB network mounts (Section 6). One machine or a directly attached disk: supported. Network mounts: use archives.
- **No symlink tracking** (skipped with a note).
- **Whole-file storage.** zlib-compressed but not delta-compressed; heavy for huge frequently-changing binaries. Archives are also held in memory while sealing/opening — fine to a few GB, not streaming.
- **Rename detection is exact-content only.** Moved-and-edited files show as add + delete; merges are not rename-aware.
- **Conservative merges.** Adjacent-line edits and same-point insertions conflict rather than merge; conflicts are resolved on a branch, not via in-worktree conflict markers (an explicit simplicity trade).
- **No branch rename, no per-save tags** yet.
- **`unpack -i` keeps no backup.**
- **Anchors are manual.** Automatic anchoring is roadmap.
- **Ignoring a tracked file drops it from the next save** (Section 13) — correct by the snapshot model, surprising if unread.

---

*sb — one file, no dependencies, nothing silently destroyed.*
