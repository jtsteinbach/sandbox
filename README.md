# sandbox (sb)

**Version 1.3** · [jts.gg/sandbox](https://jts.gg/sandbox)
**License** · [jts.gg/license](https://jts.gg/license)

Safe version control in one file. One command vocabulary you can learn in five minutes. No dependencies beyond Python 3.9+, no cryptography libraries, and no command that destroys saved history.

sandbox is not a git clone and does not use git's repository format. It keeps the two ideas git got right (content addressing and a Merkle DAG of snapshots) and drops the staging area, detached HEADs, destructive commands, and the loose-file repository layout.

It also does one thing no other version-control tool does: it asks the kernel who wrote each file. There is no configured identity, no name, no email — you are the Linux account you are logged in as, and a small system service records which account performed every write. Everything about shared editing, locks, and file access is built on that one fact.

---

## Table of contents

1. [Installation](#1-installation)
2. [Why sandbox exists](#2-why-sandbox-exists)
3. [Quickstart](#3-quickstart)
4. [Core concepts](#4-core-concepts)
5. [Command reference](#5-command-reference)
6. [The write-attribution watcher](#6-the-write-attribution-watcher)
7. [Accounts and file access](#7-accounts-and-file-access)
8. [Shared editing and locks](#8-shared-editing-and-locks)
9. [Test gates](#9-test-gates)
10. [Secrets and redaction](#10-secrets-and-redaction)
11. [Security model](#11-security-model)
12. [Anchors](#12-anchors)
13. [Portable archives (.sbox)](#13-portable-archives-sbox)
14. [The storage format](#14-the-storage-format)
15. [Ignoring files](#15-ignoring-files)
16. [Everyday workflows](#16-everyday-workflows)
17. [sandbox versus git](#17-sandbox-versus-git)
18. [Environment variables](#18-environment-variables)
19. [Exit codes](#19-exit-codes)
20. [FAQ](#20-faq)
21. [Troubleshooting](#21-troubleshooting)

---

## 1. Installation

Requirements: Python 3.9+ (standard library only) on Linux.

sandbox 1.3 depends on three Linux-specific kernel facilities, and none of them have portable equivalents:

- **fanotify**, for the write-attribution watcher (Section 6). This is what makes "who edited this file" a fact rather than a guess.
- **POSIX ACLs**, written directly as the `system.posix_acl_access` extended attribute, for per-account file access (Section 7). Works on ext4, xfs, btrfs, and anything else mounted with ACL support.
- **POSIX directory descriptors** with no-follow semantics, for symlink-safe atomic writes.

macOS has neither of the first two, so `sb init` will not proceed there. WSL2 works where its kernel provides fanotify and ACLs.

System-wide (requires sudo):

```bash
curl -sL install.jts.gg/sandbox | sudo bash
```

For your user only:

```bash
curl -sL install.jts.gg/sandbox | bash
```

Manual install, if you prefer to inspect first:

```bash
mkdir -p ~/.local/bin
cp sb.py ~/.local/bin/sb
chmod +x ~/.local/bin/sb
```

If `~/.local/bin` is not on your PATH, add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile.

### Then install the watcher, once per machine

```bash
sudo sb service -i     # installs and starts the write-attribution watcher
sb service -s          # confirm: "watcher running"
sb help                # the command menu
```

`sb init` refuses to create a repository if the watcher is not installed and running, and says so with the command to fix it. This is deliberate: without the watcher, an in-place edit (`>>`, `sed -i`, `nano`, any editor that truncates rather than renames) leaves no record at all of who made it, and sandbox would have to attribute the change to whoever happens to own the file. Section 6 explains what the watcher does and what it never does.

Existing repositories pick the watcher up automatically — any sandbox command registers its repository with the service if it isn't registered already.

### Upgrading

Re-run the install command, then `sudo sb service -i` again so the service points at the new binary. sandbox refuses to open repositories created by a newer format than it understands, so upgrades are safe and downgrades fail with a clear message instead of corrupting anything.

Repositories from earlier versions are migrated in place the first time 1.3 opens them: the stat cache gains its ctime and inode columns (and is cleared, so the first scan re-reads), and the lock table gains the columns the content-lock model needs (`held`, `mode`, `uid`, `perm`). A lock recorded before the upgrade has no protected content, so it adopts whatever is on disk the first time 1.3 sees it.

`sb verify` re-hashes every object, recomputes the journal chain, and cross-checks every branch tip against it, so you can confirm at any time that an installed copy and its history still agree.

---

## 2. Why sandbox exists

Version control solves a real problem: change things without fear, and know exactly what happened later. Git solves it too, but behind an interface where `checkout` means four different things, a mistyped `reset --hard` erases an afternoon, and the repository itself is thousands of loose files that a power cut can leave half-written.

sandbox is built on four decisions.

**First, safety is structural.** No sandbox command discards saved history. `undo` creates new history that reverts the old. `switch` refuses to run over unsaved work. A lock revert stores the bytes it displaces. The store is one SQLite database, so every operation is a single atomic transaction; a crash mid-save leaves you exactly where you were.

**Second, simple doesn't mean toy.** There is no staging area (a save snapshots everything that isn't ignored), no detached HEAD, and no rebase. There are real branches, three-way merges with automatic conflict-free merging, similarity-based rename detection, versioned test gates, release records, per-file content locks for teams, and a full-store integrity verifier.

**Third, identity is not configuration.** Older versions had `sb who`, a name, an email, and a profile file — all of which could say anything, and none of which the filesystem agreed with. That is gone. You are your Linux account. The account that runs `sb` is who you are, the roster of accounts added to a repository is what grants write access, and a kernel-backed service records which account actually performed each write. Nothing here can drift out of step with the machine it runs on, which is the entire point.

**Fourth, security claims are kept narrow and true.** sandbox promises integrity, tamper evidence, and leak prevention. Section 11 states how each works, what it defends against, and what it doesn't. There are no keys and no signatures; everything rests on SHA-256 from the standard library, used for content addressing and hash chaining.

---

## 3. Quickstart

```bash
sudo sb service -i               # once per machine: the write-attribution watcher

cd my-project
sb init                          # creates .sb/sandbox.db, branch "main", you as creator

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

# let someone else in
sb useradd alice                 # a real linux account; grants write access
sb users                         # who is in this repository
sb locks                         # who is holding what right now

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

Locks (Section 8) need no setup: they are always on, and editing a file is what takes one.

---

## 4. Core concepts

### Saves

A save is a complete snapshot of every tracked file, with a message, an author (your account name), and a timestamp. There is no staging area and no partial commit: what you see in your working folder, minus ignored files, is what gets saved. This makes "the tests passed on this save" mean something, because the save is exactly the tree that was tested.

One refinement, which is what makes a shared folder workable: a save commits *your* files and leaves everyone else's in-progress edits exactly as they are, on disk and in the commit (Section 8). `sb save --global-force` sweeps in everyone's edits and says so in the journal.

Every save records the hash of its parent, so saves form a chain, and with merges a DAG. Changing any byte of any past save would change its hash and break every link after it.

### Identity

There is no identity to configure. The author of an operation is the login name of the account running it, read straight from the system password database. Under `sudo`, sandbox resolves the invoking account from `SUDO_UID` rather than reporting root, so `sudo sb export` is still attributed to you.

A repository additionally has a **creator** (the uid that ran `sb init`) and a **roster** (the uids added with `sb useradd`). Those two facts drive file ownership and access (Section 7).

### Branches

A branch is a named pointer to a save, and it is never just a name: creating one (`sb branch idea`) immediately saves the working folder onto it with the message `Initial branch creation`. A new branch therefore has content from the moment it exists and can be switched to, tested, exported, and merged straight away.

If the branch you are standing on has never been saved, it is seeded with that same first commit, so both branches share one common base and an immediate merge is meaningful rather than an error.

You are always on exactly one branch; there is no detached-HEAD state. Switching rewrites the working folder to match the branch's latest save, and refuses to run if you have unsaved changes — except when the folder already equals the target branch's tree, which is what lets `sb branch x` be followed straight away by `sb switch x`.

### Locks

Editing a file locks it to you. A lock records the exact content you are protecting: while you hold it, your version is the file, other people's edits to it are put back, and only you can commit it. A lock also moves the file's ownership and access to you, so other accounts cannot write it in the first place. Locks are always on, need no configuration, and cost nothing in a repository with one user. Section 8.

### The object store

Content lives in a content-addressed store: the key for each object is the SHA-256 hash of its content, so identical files are stored once regardless of how many saves contain them.

| kind | contents |
|---|---|
| `blob` | raw file bytes (files below the chunk threshold) |
| `chunk` | one piece (up to 1 MiB) of a large file |
| `chunked` | the ordered list of chunk hashes making up one large file |
| `tree` | a directory listing: `[[mode, kind, hash, name], ...]` as canonical JSON |
| `commit` | `{tree, parents, author, time, message}` as canonical JSON |

A file at or above 8 MiB is stored as a `chunked` object listing 1 MiB `chunk` hashes rather than a single blob, so a small edit to a large file only stores the chunks that changed, and no read has to hold the whole file at once. A `chunked` object addresses to the SHA-256 of the reassembled content, exactly as a plain blob would, so content addressing, `sb verify`, and every consumer are unchanged — the chunking is invisible above the store layer.

Chunking is verified at two levels, and both are necessary. Each chunk is re-hashed as it is read, and the reassembled whole is re-hashed against the object's own hash before the last byte is handed over. Per-chunk checks alone would not catch a chunk *list* that was reordered or repointed at other valid chunks — every piece would verify while the file came out wrong. Because callers write through an atomic temp file, a failure at the end means nothing reaches the working folder. `sb verify` streams objects the same way, so checking a repository never costs more memory than one chunk. Symlinks are stored as a blob holding the target path, under mode `120000`.

Every object is re-hashed on every read, not just during `verify`. A damaged blob or chunk raises an error the moment anything touches it.

### The journal

The journal is an append-only log inside the store recording every operation: saves, merges, undos, restores, branch creation and removal, switches, releases, lock releases, lock reverts, expiry auto-saves, opened and aborted merges, account additions and removals, ignore rules, durability changes, and pack/export/unpack events. Each entry embeds the SHA-256 link of the previous entry, forming a hash chain rooted in a random repository ID chosen at `init`.

The consequences:

- Deleting or editing a journal entry breaks the chain at that point.
- Moving, deleting, or injecting a branch tip behind sandbox's back (direct SQL included) is caught by `sb verify`, which cross-checks refs against the journal.
- Replacing an object is caught by the content re-hash.

Anything unusual about a save is part of the record, and `sb journal` shows it inline: `· no-verify`, `· secrets-override`, `· global-force`, `· redacted`, `· secrets-present`, `· initial-save`, `· seeded`. Skipping a gate is allowed; hiding it is not.

### Renames

`status`, `diff`, `log`, and merges detect renames in two layers.

First, **exact content**: a deleted path and an added path with byte-identical content pair up immediately. Empty files never pair.

Second, **content similarity**, for the case exact matching can never catch — a file that was moved *and* edited, whose hash is different by definition. sandbox hashes each candidate in pieces and compares the sets of piece hashes; a pair sharing at least half its pieces (Jaccard ≥ 0.5) is reported as a rename. For text, the pieces are non-blank lines. For binary content, boundaries are chosen by a rolling hash over a 48-byte window rather than at fixed offsets, so inserting or deleting bytes shifts only the piece it landed in and every other boundary stays put — fixed-size blocks do not have that property, since one inserted byte moves every later boundary. An inverted index from piece hash to candidate means only files that actually share content are ever compared, so the work is proportional to real overlap rather than to added × deleted, and no pair budget is needed.

Merges use the same detection: if one side renames a file and the other edits it under the old name, the edit follows to the new name. A file renamed differently on the two sides conflicts.

A detected rename is reported as one pairing rather than as a separate add and delete. `sb status` shows `renamed old → new`, `sb log` folds it into the change summary, and `sb diff` prints a single `@@ old → new  renamed (content identical)` header for the pair without a diff body.

### Test gates

Executable scripts under `sb-tests/pre-save/`, `sb-tests/pre-merge/`, and `sb-tests/pre-publish/` run before the corresponding operation and block it on failure. They run in a pristine temporary checkout of the exact candidate tree, never your working folder. Section 9.

---

## 5. Command reference

The grammar is consistent across commands:

- Positional arguments say what: a message, a path, a branch, a version, an account.
- Options say how, and each routine option has a short and long form: `-k`/`--key`, `-f`/`--files-only`, `-i`/`--ignore`, `-n`/`--limit`, `-r`/`--remove`, `-a`/`--anchor`, `-l`/`--list`, `-p`/`--path`, `-s`/`--status`. Options may appear anywhere on the line.
- The safety overrides (`--allow-secrets`, `--no-verify`, `--global-force`, `--force`) deliberately have no short form. Bypassing a gate or taking someone's lock is typed out in full. `-i` is not in this class: skipping locked files in a merge, or unpacking into a non-empty folder, is routine once you've asked for it explicitly.
- Pass-keys are always `-k <passkey>` across `pack`, `unpack`, and `export`. For `pack` and `unpack` the key may instead come from `SB_PASSKEY` or an interactive prompt; for `export`, `-k` is what selects the encrypted form, and its value may be left empty to fall back to the variable or the prompt.
- Subactions are words: `sb test list`, `sb test new`, `sb publish list` (`-l` also works).
- Mistakes get a one-line explanation, the correct usage line for that command, and a pointer to `sb help`, not a parser dump. An option that exists but belongs to a different command is named as such rather than left looking removed.

`<angle brackets>` are required, `[square brackets]` optional. Commands work from anywhere inside the repository. Colors appear only when output is a terminal.

`status`, `save`, `undo`, `restore`, `branch`, `switch`, `merge`, `publish`, `locks`, and `unlock` each synchronize locks before doing their own work — enforce, expire, acquire, re-apply permissions — which is how a tool with no per-repository daemon keeps a shared folder consistent.

### `sb init`

Creates the repository: `.sb/sandbox.db`, branch `main`, a journal seeded with a random repository ID, and you recorded as the creator. The database is created with `0600` permissions, and the repository registers itself with the watcher.

Fails if a repository already exists here, or if the write-attribution watcher is not installed (`sudo sb service -i`) or not running (`sb service -s`).

### `sb status [--deep]`

Current branch, latest save, active locks (who holds what, and `their version wins` for other people's), files that differ from history only because their secrets are redacted, and every change relative to the last save: `renamed old → new`, `new`, `modified`, `deleted`. Files that belong to someone else's lock are marked `(theirs)` and are not counted in the changes your next save would commit. `--deep` bypasses the stat cache and re-hashes every file.

### `sb save "<message>" [--allow-secrets] [--no-verify] [--global-force]`

Snapshots your changed files as a new save. The message is required. In order:

1. Synchronize locks: put other people's locked files back to their versions, expire abandoned locks, take locks on the files you edited, and re-assert lock permissions.
2. Work out your file set: the files you changed, excluding every file another person holds a lock on.
3. Redaction pass: recognized credentials in your added or modified files are replaced with `<REDACTED>` in the blob about to be committed (Section 10). The file on disk is never rewritten. `--allow-secrets` commits them verbatim instead. A file that contains a credential but is not clean UTF-8 cannot be rewritten faithfully, so it blocks the save rather than being mangled.
4. Pre-save test gates in a clean checkout of the candidate tree (Section 9). Failures block; `--no-verify` overrides.
5. Re-check that the worktree didn't change while being scanned and tested; refuse rather than commit an untested state.
6. Store blobs, build the tree, write the commit, move the branch tip (compare-and-swap against the tip it started from), release your locks, restore normal file access, and journal it — all in one transaction. Bypasses and redactions are journaled with it.

If a conflicted merge is open, `sb save` is what finishes it: the result becomes a real two-parent merge commit, and the save is refused while any conflicted file still holds an `<<<<<<< ours` marker.

`--global-force` snapshots the entire worktree, everyone's edits included, and releases every lock. It is journaled as `global-force`.

### `sb log [-n <count>]`

Save history, newest first: hash, date, author, message, a `(merge)` marker, and a change summary per save (`+2 new · ~1 modified · old.txt → new.txt`). `-n 5` limits output.

### `sb diff [<path>]`

Unified diff between the working folder and the last save. Renames show as one `@@ old → new` line. Files under another person's lock are labelled before their diff. Binary files show as a one-line size summary. With `<path>`, limits output to that file or folder.

### `sb undo [-p <path>]`

Reverts the latest save by creating a new save whose content equals the previous one. The undone save stays in the log and journal; running `sb undo` again redoes. Requires a clean working tree — other people's locked files are exempt from that check and are never written over.

With `-p <path>`, brings just that file or folder back from the last save, overwriting the working copy and releasing your lock on it. No new save is created. Refused if someone else holds a lock on any matched path. These writes go through the same symlink-safe atomic machinery as checkout, so a symlink planted at the path or in a parent cannot redirect the write outside the repository.

### `sb restore <anchor | save | release-label | branch>`

Returns the current branch to any past state, as a new save. Nothing is rewound or deleted; `sb undo` afterward takes you straight back. Requires a clean working tree (again, other people's locked files excepted).

Targets: an anchor (8–64 hex characters of a journal chain link, resolving to the current branch's tip as the journal recorded it at that moment), a save-hash prefix from `sb log` (4+ characters), a release label (`sb restore rel-3`), or a branch name. Ambiguous targets are rejected with a list of what matched. If the target looks like a path, the error suggests `sb undo -p` instead.

### `sb branch [<name>] [-r] [--allow-secrets]`

No argument: list branches with their tips. With a name: create it **and immediately save the working folder onto it** as `Initial branch creation`. With `-r`: delete the named branch's pointer (never the current branch, never the last one). Its saves stay in the store and journal, and `verify` keeps checking them.

The initial save behaves like any other save: credentials are redacted (`--allow-secrets` commits them verbatim), and files another person holds a lock on are taken from the last save rather than from disk, because their in-progress work is theirs to commit. If the current branch has no saves yet, it is seeded with the same commit.

### `sb switch <branch>`

Rewrites the working folder to the branch's latest save and updates the branch pointer. Refuses with unsaved changes, unless the folder already matches the target exactly. Files locked by other people are left alone. Writes are atomic and symlink-safe; directories emptied by the switch are pruned. There is no detached mode — an unknown name is an error, not a new branch.

### `sb merge <branch> [--no-verify] [-i]` / `sb merge --abort`

Three-way merge into the current branch, using the best common ancestor as the base (a true lowest-common-ancestor search, correct after prior merges; criss-cross histories resolve deterministically).

- If the current branch is an ancestor of the target, the tip fast-forwards (still gated by pre-merge tests).
- Files changed on one side take that side.
- A file that exists on only one side, and never existed in the base, is kept. Deletion happens only when the base had the file and one side removed it.
- Merge is rename-aware, using the similarity detection described in Section 4: if one side renames a file and the other edits it under the old name, the edit follows to the new name. A file renamed on one side and deleted on the other conflicts, as does a file renamed differently on both.
- Files changed on both sides get a line-level three-way merge. Non-overlapping edits combine (including edits on adjacent lines), and CRLF files and files without a trailing newline merge with their line endings and final-newline state preserved. Genuine overlaps, differing same-point insertions, binaries, differing executable bits, and symlinks whose targets both changed all conflict.

Pre-merge gates run against the merged tree itself before it is committed, and discover their scripts from that merged tree.

On conflict the merge is applied to the worktree with `<<<<<<< ours` / `=======` / `>>>>>>> theirs` markers written into the conflicting text files, and the merge stays open: `sb switch`, `sb branch`, `sb restore`, `sb undo`, `sb publish`, and another `sb merge` are blocked until it's resolved. Edit the marked files and run `sb save "<message>"` to finish — the save is refused while any conflicted file still holds an `<<<<<<< ours` marker — or `sb merge --abort` to put the folder back exactly as it was. Binary and symlink conflicts have no markers to place, so the file is kept at your version and listed for you to reconcile by hand.

A merge that would change a file someone else has locked is refused. With `-i` (`--ignore`), those files are skipped: the merge proceeds for everything else, each skipped file keeps your current version and its lock, and the result is recorded as a **partial merge** — a single-parent save, not a merge commit — so re-running the merge after the locks release brings in what was skipped. sandbox does not record ancestry it didn't actually merge.

### `sb test [<stage>]` / `sb test new <stage> <name>` / `sb test list` / `sb test guide`

Run gates manually (all stages, or one of `pre-save`, `pre-merge`, `pre-publish`), scaffold a new script, list discovered tests, or print the built-in walkthrough. A manual run tests your current worktree. Exits `0` if everything passed, `2` otherwise.

### `sb publish [<label>]` / `sb publish list` / `-l` / `--no-verify`

Records the current save as a release, behind two gates: full store verification (sandbox refuses to publish from a damaged store), then pre-publish tests on a clean checkout of the exact HEAD tree. Requires a clean worktree and no open merge. Passing both writes a `publish` entry into the journal: what, from which branch, by whom, when, and the content hashes of the gate scripts that ran. The anchor for that entry is printed. `sb publish -l` lists releases and reports whether the chain protecting them still verifies.

A release is a record. To get its files back out, use `sb export`.

### `sb export <version> [<destination>] [-k <passkey>]`

Materializes any version (release label, branch name, or save-hash prefix) as plain files, with executable bits preserved and no `.sb` directory. The destination defaults to `<repo>-<version>/` and must be empty. Every blob is re-hash-verified on the way out, and a plain folder export writes nothing at all to your repository.

With `-k <passkey>`, produces an encrypted files-only `.sbox` artifact instead, carrying the label, commit, and sealed-by metadata, and journals the export. Ship it and drop it with `sb unpack <file.sbox> /path -k <passkey>` (`-i` to redeploy over a previous drop).

### `sb verify [-a <hash>]`

Re-hashes every object in the store (including history from removed branches and orphans from interrupted operations), validates every tree entry name, recomputes the entire journal chain, and cross-checks every branch tip against the journal, catching refs that were moved, deleted, or injected outside sb. Malformed objects, unreadable journal rows, and unexpected refs are reported as findings, not crashes. `-a <hash>` additionally confirms a previously noted anchor (Section 12).

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

The operation log, with detail for every operation type: ref moves with old → new hashes and any bypass, redaction, or initial-save markers; releases; switches; branch removals; account additions and removals; opened and aborted merges; lock releases (with `· forced` when forced); lock reverts (with the paths put back and the hashes of the bytes kept); ignore rules; durability changes; and pack/export/unpack events. Each line ends with that entry's 16-character chain link, which is a usable anchor. Ends by re-verifying the chain.

### `sb info`

One-screen overview: store location and size, current branch, object counts, journal length, current anchor, active lock count, and the account you are acting as.

### `sb service -i | -s | -k`

Manages the machine-wide write-attribution watcher (Section 6).

- `-i` / `--install` — install and start it. Needs root. Writes a systemd unit (`sandbox-watch.service`) where systemd is available and enables it; otherwise starts a detached background process, logging to `/var/lib/sandbox/watch.log`. Safe to re-run: it stops any existing watcher first, so this is also how you restart one.
- `-s` / `--status` — installed? running? event count, seconds since the last heartbeat, registered repositories, pid. Warns plainly when it is installed but stopped.
- `-k` / `--kill` — stop it, and keep stopping until the lock is free, so no straggler survives.

### `sb useradd <linux-user>`

Adds a real Linux account to this repository's roster and re-applies access across the repository. They may then edit any unlocked file; editing one locks it to them until they save. The account must already exist on the system — sandbox does not create users. Journaled.

### `sb userdel <linux-user>`

Removes an account from the roster and re-applies access, so they can no longer write the repository's files. The creator cannot be removed. If they still hold locks, the count and paths are reported, along with the `sb unlock <path> --force` you would use to release them. Journaled.

### `sb users`

Every account in the repository, with `creator · owns unlocked files` on the creator, `you` on yourself, and the number of locks each is currently holding.

### `sb durability [full|normal]`

Shows or sets crash durability. `full` (default): the newest committed transaction survives power loss. `normal`: faster, still crash-safe, but may lose the most recent commit on power loss. Journaled.

### `sb locks`

Every active lock: the path, who holds it, how long they have held it, how many minutes until it expires, and a short hash of the content being protected (`· file removed` when the holder deleted it).

### `sb unlock [<path>...] [--force]`

Releases your locks (all, or the named paths), restoring each file's previous permission bits. `--force` releases locks held by others, and hands the file's ownership to you where sb has the privilege to do so — otherwise the next scan would read the old owner and hand the lock straight back, making the force do nothing. The journal records the paths, the prior owners, that it was forced, and any ownership taken. Releasing a lock does not change file content — it only stops sandbox putting that file back.

### `sb salvage <hash> [<path>]`

Writes any stored content back out to a file. This is the other side of a lock revert: when your edit to someone else's locked file is put back, the bytes you wrote are stored, their hash is printed and journaled, and this command brings them back. The hash is 4–64 hex characters (sb prints 10) and must be unambiguous; the destination defaults to `salvaged-<hash>` and is never overwritten if it exists.

### `sb ignore <pattern>`

Appends a pattern to `.sbignore` (Section 15). Journaled.

### `sb pack [<output>] -k <passkey> [-f]`

Seals the whole repository into one encrypted `.sbox` (Section 13). Output defaults to `<foldername>.sbox`; an existing file is never overwritten. The archive is written through an exclusively-created randomized temp file and atomically renamed, so a pre-planted symlink can't redirect it. Warns about unsaved changes, since pack seals saved history. Journaled.

`-f` seals only the current save's files, no history.

### `sb unpack <file.sbox> [<destination>] -k <passkey> [-f] [-i]`

Restores an archive. A wrong pass-key or altered archive fails cleanly and writes nothing. For a full-repository archive, the store is restored into a private staging area and fully verified first (objects re-hashed, chain recomputed, refs cross-checked); only if everything agrees is it installed and checked out. A damaged archive is refused before anything lands in the destination:

```
unpacked my-project · 3 file(s)
  ├─── sealed by  jordan  · 2026-07-14 08:35
  ├─── branch     main · anchor bd40a7878f681649
  └─── verified before install ✓ — store, journal and refs all agree
```

The destination must be fresh or empty. `-i` (`--ignore`) merges into a non-empty destination instead: matching paths are overwritten with the archive's version, everything else is kept. This is the redeploy flag. There is no per-file backup.

`-f` writes only the native files with no `.sb` directory. Files-only archives unpack this way automatically; since no store is installed, there is nothing to journal. A full-repository unpack journals itself in the repository it installs.

### `sb version`

`sb version` (also `-V`) prints `sb 1.3 · jts.gg/sandbox`.

---

## 6. The write-attribution watcher

Everything sandbox does in a shared folder rests on one question: **which account wrote these bytes?**

The filesystem cannot answer it. It records who *owns* a file, never who *wrote* to it. An in-place write — `>>`, `sed -i`, `nano`, any editor that truncates rather than replacing — changes the content and leaves ownership exactly as it was. Attributing that edit by ownership credits whoever created the file, which in a shared repository is usually the wrong person, and wrong in the one direction that matters: it takes someone's work and files it under someone else's name.

So sandbox asks the kernel instead.

### What it is

One process for the whole machine, installed with `sudo sb service -i`. It opens a single **fanotify** notification descriptor and places a *mount mark* on each mount holding a registered repository. A mount mark is recursive and covers directories created after the watch started, so coverage of a repository is complete rather than best-effort. Every completed write (`FAN_CLOSE_WRITE`) arrives with the pid that made it, and `/proc/<pid>` turns that pid into an account. Where the kernel supports it, sandbox asks for pidfd reporting, which pins the writing process so a short-lived one cannot exit before it is identified.

Both the real uid and the **loginuid** are recorded. loginuid is set by PAM at login and cannot be changed without `CAP_AUDIT_CONTROL`, so unlike `$SUDO_UID` it survives `sudo` and `su` and cannot be forged by the person running the command.

fanotify is reached through `ctypes` against libc. There is no package to install, no auditd, and no dependency added to the one-file promise.

### What it writes, and what it never writes

The watcher writes **only to its own store**, `/var/lib/sandbox/events.db`. It opens repository databases **read-only** and never for writing. A root-owned writer inside a user's SQLite file is how you get root-owned WAL sidecars and two writers on one database, so it simply doesn't happen.

```
/var/lib/sandbox/
  events.db      the event store (0644, root-owned, world-readable)
  repos.d/       one small file per registered repository (1777, sticky)
  watch.lock     the singleton lock
  watch.pid      the current pid
  watch.log      output, when running without systemd
```

`repos.d` is world-writable with the sticky bit, exactly like `/tmp`: anyone may register their repository, nobody may remove someone else's. That is deliberate — `sb init` is not a root command, so registration cannot go through the root-owned database. A registry entry only takes effect if the path really contains a repository, so a stray or mischievous entry cannot make the watcher mark an unrelated mount. Up to 512 repositories are watched.

The event store is pruned on a rolling basis: writes outside any repository are dropped after five minutes, and everything else after 30 days.

### Coverage: knowing when you don't know

A watcher that silently missed events would be worse than none, because it would let sandbox go back to guessing without saying so. Each run of the watcher writes a **coverage window**: when it started, a heartbeat every 5 seconds, and a flag set when it shuts down cleanly. Windows are stored with fractional seconds, so an edit made immediately after a clean stop falls outside the window rather than sharing its timestamp with it.

When sandbox needs to attribute a change, it looks for an event that could actually explain *this* change:

1. Match on **(device, inode)** first. The inode identifies these exact bytes whether they were written in place or renamed into position from a temp file elsewhere — which is what most IDEs do, and what a name-keyed watch would miss entirely.
2. Fall back to matching on path.
3. In both cases, only consider events at or after the file's mtime (minus one second, for whole-second rounding). An event that predates the change cannot be the thing that produced it, and crediting it would hand a later edit to an earlier writer.

If nothing matches but the file's mtime falls inside a coverage window, sandbox falls back to the file's owner. If the mtime falls **outside** every window, sandbox says so and does nothing else:

```
  ├─── notes.txt changed while nothing was watching
  └─── the watcher was not running, so sb cannot say who wrote these.
       they are left unlocked and unattributed — sudo sb service -s
```

That message is never suppressed, not even by the quiet mode other commands use.

### Managing it

```bash
sudo sb service -i     # install + start (also the way to restart)
sb service -s          # status, heartbeat, registered repositories, pid
sudo sb service -k     # stop
```

With systemd, `-i` writes `/etc/systemd/system/sandbox-watch.service`, enables it, and restarts it; the unit runs at `Nice=5` with idle I/O scheduling and restarts automatically. Without systemd, `-i` spawns a detached background process instead, so the tool still works rather than quietly having no attribution.

Only one watcher may run at a time, enforced by an exclusive advisory lock rather than a pid file. The kernel drops that lock however the holder dies, so a killed watcher never leaves a ghost behind, and two simultaneous starts cannot race. This matters more than it looks: duplicate watchers would each open their own coverage window, so stopping "the" watcher would leave others still reporting that everything is observed.

### The privileged half of access enforcement

The watcher is also the only thing on the machine that can hand a file from one account to another, so it applies the access rules described in Section 7. It re-applies them when it sees a write inside a repository, and every few seconds for any repository whose store has changed. `sb` itself runs as whoever typed the command and usually cannot `chown`, which is why this job belongs here: without it, a lock holder could end up unable to write the file they had just locked.

---

## 7. Accounts and file access

A repository has a **creator** — the account that ran `sb init` — and a **roster** of additional Linux accounts added with `sb useradd`. Membership is what grants write access to the repository's files. A lock narrows that access to one person for as long as it lasts.

```bash
sb users                 # who is in this repository
sb useradd alice         # grant access (must be a real system account)
sb userdel alice         # revoke it; the creator cannot be removed
```

### The rule, in two lines

- **Unlocked file** → owned by the creator; every roster member may write it.
- **Locked file** → owned by the holder; only the holder may write it.

Directories follow from that: owned by the creator, mode `0755`, with every roster member granted `rwx`. This is not decoration — replacing a file requires write permission on its *containing directory*, not on the file, so an editor that saves by write-temp-then-rename cannot save at all unless roster members can write the directory. That same rename is also the moment the kernel records who wrote the new file.

The `.sb` directory is mode `0770` with roster members granted `rwx`, and the store and its `-wal`/`-shm` sidecars are `0660` with roster members granted `rw`. Every member has to be able to write the store or only the creator could ever save, and SQLite needs to create its sidecars beside the database, so the directory must be writable too.

### How "these specific accounts may write this file" is expressed

Mode bits cannot say that. A POSIX ACL can, so sandbox writes one — directly, as the `system.posix_acl_access` extended attribute. The on-disk ACL format is small and stable, so this needs no `acl` package and no `setfacl` binary, and the no-dependencies promise survives. The entries are: owner `rw(x)`, each named account `rw(x)`, group and other read-only, plus a mask.

On a filesystem without ACL support the `setxattr` simply fails and the mode bits still apply — you get owner-writable files rather than per-account access.

### Who actually applies it

`sb` applies what it can whenever it changes something relevant: taking or releasing a lock, `useradd`, `userdel`, and after a save releases locks. But `chown` is privileged, so an ordinary `sb` run cannot hand a file to another account. The watcher, running as root, does that (Section 6): it reads the repository's creator, roster, locks, and committed file list — straight from the head trees, not from the stat cache, which can be empty after a migration — and re-asserts ownership, mode, and ACLs across the repository whenever the store changes or a write is observed inside it.

The practical consequence: with the watcher running, access rules converge within a few seconds of any change. With the watcher stopped, ownership drifts and sandbox will tell you it cannot attribute changes.

---

## 8. Shared editing and locks

A whole team can work in one repository — one folder, one database — without clone/push/pull. This is not a mode and cannot be turned off. A repository with one user simply never sees a foreign lock, and everything below collapses to ordinary solo behavior.

### The rules

- **Editing a file locks it to you.** There is no per-repository daemon; locks are claimed the next time any sandbox command scans the tree, and attributed to the account the watcher recorded as the writer.
- **A held file is read-only to everyone else, and owned by you.** Taking a lock drops the file's group and other write bits, hands ownership to the holder, and narrows the ACL to the holder alone, so another person's editor refuses to save over it in the first place — the lock is enforced by the filesystem, not only discovered afterward. Releasing the lock — by save, `sb unlock`, expiry, or `sb undo -p` — restores the exact permission bits the file had before and returns it to the shared rule.
- **If a foreign write lands anyway, your bytes win.** A shared login, a writable parent directory, or root can get past the permission bits. When that happens the next sandbox command puts your version back on disk, stores the rejected bytes, and names them in the journal, so `sb salvage <hash> [<path>]` writes them out again: the revert refuses to let a second writer's copy become the version of record, and destroys nothing.
- **Only you can save it.** Everyone else's `sb save` skips your locked files entirely, whatever is on disk. `sb save --global-force` is the deliberate exception, and the journal says so.
- **Your own later edits move the lock forward.** Each command that notices you changed the file again records the new content as the protected one and restarts the expiry clock.
- **A lock ends** when you `sb save` it, when the file matches its saved state (nothing left to protect), or after an hour of inactivity (`SB_LOCK_TTL`, default 3600 seconds).
- **Expiry loses nothing.** The abandoned edits are auto-saved as a commit in the *owner's* name, then auto-reverted — in history and on disk — so the shared tree returns to the state the owner found it in. The auto-save hash is printed and journaled: `sb restore <hash>` brings the work back.
- **Merges never clobber locked work.** A merge that would change a file someone else has locked is refused; `sb merge <branch> -i` proceeds around it as a recorded partial merge. Switches, restores, undos, and checkouts all leave locked files untouched, and the unsaved-changes check doesn't count them as your work.
- **`sb locks`** shows who holds what and what content they are protecting. `sb unlock` releases yours; `sb unlock --force` releases someone else's, journaled with the prior owner's name.

Because expiry auto-saves are *preservation* commits, they store the bytes verbatim rather than redacting them: redacting and then reverting the disk would destroy the only copy of a live credential. If such a snapshot contains recognizable secrets, sandbox says so loudly and flags `secrets-present` in the journal (Section 10).

### Who gets the lock

This is where the watcher earns its place. When a scan finds a changed file, sandbox asks the event store which account wrote it (Section 6). That is a fact recorded by the kernel, and it is right even for an in-place edit that left ownership untouched. If Alice edits and Bob runs `sb status`, the lock is created in Alice's name.

The order of preference is:

1. **The recorded writer**, matched by inode and time window.
2. **The file's owner uid**, when no event matched but the watcher was demonstrably running — the change was observed, and ownership is the best available signal for it.
3. **Nothing.** If the change happened while no watcher was running, the file is left unlocked and unattributed, and sandbox says so. It does not guess.

Deletions have nothing left to stat and produce no close-write event, so they fall back to the account running the command.

### What the lock lifecycle looks like inside a command

Every command that touches state runs the same sequence before doing its own work:

1. **Enforce** — for each lock, compare disk against the content it protects. Unchanged: nothing to do. Changed by the holder: protect the new content and reset the clock. Changed by anyone else: store the rejected bytes, journal them, and put the holder's version back. A lock whose content now equals the last save is released, having nothing left to protect.
2. **Expire** — auto-save and auto-revert anything idle past the TTL, grouped by owner and attributed to them.
3. **Acquire** — lock every changed-but-unlocked file to whoever wrote it.
4. **Re-apply** — re-assert lock permissions and ownership, since any command may have rewritten a locked file.

### Where shared editing belongs

It is designed for one machine with multiple user accounts, or a directly attached shared disk. The store is SQLite in WAL mode, and SQLite's documentation is explicit that WAL does not work reliably over NFS or SMB mounts, because file locking on network filesystems is broken in ways no application can compensate for. Attribution has the same constraint from the other end: a uid-squashing network mount destroys the ownership signal, and fanotify marks a local mount. If your shared drive is a network mount, treat multi-user use as unsupported there and move work with `.sbox` archives instead.

---

## 9. Test gates

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

Run gates manually anytime: `sb test` (all stages), `sb test pre-merge` (one), `sb test list`, `sb test guide`.

### Keeping gates usable

Keep pre-save gates fast (seconds: syntax, lint, quick unit tests) so saving stays frictionless, and put slow suites at pre-merge or pre-publish. A gate that takes ten minutes at pre-save trains everyone to type `--no-verify`, and a gate everyone overrides is worse than no gate.

---

## 10. Secrets and redaction

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

Expired-lock auto-saves are the deliberate exception described in Section 8: they preserve bytes verbatim, warn loudly, and flag `secrets-present` in the journal.

### Living with a redacted file

A redacted file differs from its saved form permanently and by design — disk holds the real credential, history holds `<REDACTED>`. sandbox treats that difference as expected rather than as unsaved work:

- `sb status` lists it under *redacted in history*, not as `modified`.
- It never counts as a dirty tree, so it can't block `switch`, `merge`, `restore`, `undo`, or `publish`, and it holds no lock.
- Lock expiry skips it, because reverting it would delete the only live copy of the credential.
- Checkouts won't overwrite it. If the target version is what your file redacts to, the file is left exactly as it is, so a branch switch can't replace your live credential with `<REDACTED>`.

The one case where the working copy does get replaced is a target that genuinely differs (a branch where that file has different content). That's the content you asked for, so sandbox writes it — but it prints a warning first naming the files whose credential exists only on disk, since history has no copy to give back.

Redaction is a seatbelt, not a substitute for keeping secrets out of tracked files. The durable fix is to move the credential to an environment variable or an ignored config file (`sb ignore .env`) so it stops recurring. Binary files and files over 64 MiB are skipped, text is scanned a window at a time so a large file costs a window rather than its own size in memory, pattern matching catches known credential shapes rather than every secret, and only files touched by the current save are scanned.

---

## 11. Security model

sandbox makes three promises. Each comes with its mechanism, what it defends against, and what it doesn't.

### Promise 1 — Integrity: what you get back is what you put in

Mechanism: every object is stored under the SHA-256 of its content and re-hashed on every read. Every save embeds its tree hash and parent hashes, so each save transitively fixes the exact bytes of every file in it and every save before it. Every operation commits as one SQLite transaction (WAL mode, `synchronous=FULL` by default). Working-folder writes use exclusive randomized temp files, fsync, and atomic rename through no-follow parent directory descriptors; this applies to checkout, switch, merge, restore, `undo -p`, lock reverts, and `salvage`. A crash between the database and the working folder surfaces as ordinary unsaved changes in `status`.

Defends against: disk corruption, torn and partial writes, power loss mid-operation, truncated or bit-flipped objects, malformed objects that hash correctly but don't parse, crafted trees attempting path traversal on checkout, and symlinks attempting to redirect any write outside the repository.

Does not defend against: loss of the database file itself. Integrity detection is not a backup.

### Promise 2 — Tamper evidence: changes made behind sandbox's back are detectable

Mechanism: the hash-chained journal. Every operation appends an entry whose link is `SHA-256(canonical entry ‖ previous link)`, rooted in a random per-repository ID. `sb verify` recomputes the chain and cross-checks every branch tip against the journal's record, flagging tips that were moved, branches that were deleted, and refs that were injected. Gate bypasses, redactions, forced unlocks, and roster changes are all part of the chained record.

Defends against: editing or deleting journal entries; manipulating refs via direct database access; replacing objects; modification by anything that isn't sb.

Does not defend against: an attacker with write access to the database and knowledge of sandbox's format can rewrite the entire store into a new, internally consistent history. With no secret material anywhere, internal consistency is recomputable by anyone; this is inherent to keyless designs. Anchors (Section 12) close the gap: a chain-head value recorded outside the machine cannot be reproduced by any rewrite. What sandbox will not do is ship the appearance of cryptographic authenticity (signatures, badges) without the key management that would make it meaningful.

### Promise 3 — Leak prevention: credentials don't enter permanent history in the clear

Mechanism: the save-time redaction pass (Section 10), on by default, overridable only explicitly, both the redaction and the override journaled. Files that cannot be redacted faithfully block the save instead.

Defends against: accidental commits of recognizable credentials.

Does not defend against: unrecognizable secrets, secrets already in older saves, or secrets in binary and oversized files, which are skipped.

### What attribution is and is not

Attribution in 1.3 is considerably stronger than a configured name — it is a kernel-recorded fact about which account performed a write, and the loginuid it records survives `sudo` and `su`. It is still **not authentication**. It says which local account wrote a file. It does not prove who was sitting at that account, and anyone with root can both write files and alter the event store. What it delivers is accuracy in a cooperative team, not resistance to a hostile administrator.

The watcher's own store is world-readable by design (so unprivileged `sb` can consult it) and root-writable only. It records paths, inodes, uids, pids, and process names for writes on watched mounts — including writes outside repositories, which are kept for five minutes and then dropped. On a shared machine, treat that as a mild and deliberate observability trade: the machine keeps a short record of who wrote what, where sandbox is watching.

### What locks are and are not

Locks are a coordination mechanism with filesystem teeth, not access control in the security sense. Permission bits and ACLs stop the ordinary case — another member's editor refuses the write. They stop a second writer's version from becoming the version of record, on disk and in history, and they make sure nothing typed is destroyed on the way. They do not stop root, a shared login, or anyone bypassing sandbox entirely. `sb unlock --force` is available to every member, and journaled.

### What sandbox does not claim

- Confidentiality of the store. The database is created `0600`, and the watcher relaxes it to `0660` (with `.sb` at `0770`, plus ACL entries for any additional roster members) so that every member can write it; full-disk encryption is the right layer for confidentiality at rest. (`.sbox` archives are encrypted; that covers transport and cold storage.)
- Authentication, as above.
- Access control beyond what the local filesystem enforces. sandbox is a local tool.

### Why the cryptography was removed

An earlier design signed every commit with Ed25519, with a hand-rolled pure-Python fallback. That fails security review on three grounds: hand-rolled signature code is where implementation vulnerabilities live; keys with no management story (generated silently, stored in a dotfile, never rotated or bound to anything a verifier could trust) prove only that some key signed something; and a claim users trust more than its mechanism deserves is worse than no claim. The same standard was applied to the embedded encryption module, which carried an unused "asymmetric" mode whose construction did not deliver public-key security; it was deleted rather than documented around. Every property the removed code was supposed to provide is covered by the re-hashing, the chain, the ref cross-check, and anchors.

---

## 12. Anchors

The one attack a keyless store cannot detect internally is a wholesale rewrite (Promise 2's stated limit). Anchors close it with a hash and a second location.

`sb verify` and `sb info` print the current anchor, a 16-character prefix of the latest journal entry's chain link (and every `sb journal` line and every `sb publish` prints the link for that entry, so any of them can be noted):

```
  └─── anchor          67b3dea8b260c12a  (save it · check later: sb verify -a <hash>)
```

Copy it anywhere off the machine: a note on your phone, a message to a colleague, a line in a logbook. Later:

```bash
sb verify -a 67b3dea8b260c12a
```

Sixteen hex characters is 64 bits: short enough to jot down, far too large for a forged journal to collide with. Any 8–64 hex prefix of a chain link is accepted, and a pasted trailing ellipsis is forgiven.

If the anchor is a link in the current chain, everything up to that moment is exactly as it was when you noted it. If it isn't found, the journal on disk is not the journal you witnessed: history was replaced, and the rewrite can't touch what's in your notebook.

Anchors also work as bookmarks: `sb restore <anchor>` returns the current branch's content to the state that anchor witnessed, as a new save. If that branch had no saves at the anchor's moment, sandbox says so and lists the branches that did.

---

## 13. Portable archives (.sbox)

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

An unpacked repository keeps the creator and roster uids recorded in its store. On a different machine those uids may map to different people or to nobody, so check `sb users` after restoring and adjust with `sb useradd` / `sb userdel`.

### What's inside a .sbox

Every archive carries an encrypted manifest alongside the store:

| Field | Meaning |
|---|---|
| `created` / `created_by` | when the archive was sealed, and the account name that sealed it |
| `repo_name` | the original folder name (the default unpack destination) |
| `branch` | the branch current at pack time |
| `chain_head` | the journal chain head at pack time; usable as an [anchor](#12-anchors) |
| `label` / `commit` | for `sb export -k`: which version the files came from |
| `sb_version` / `sbox_version` / `repo_id` | versions and the repository's stable ID |
| `db_sha256` / `db_size` | integrity check for the sealed payload, verified before anything is written |

The manifest lives inside the encrypted blob, so an archive reveals nothing (author, branch, file names) without the pass-key. The only cleartext is the small header (`SBOX`, a format byte, and a random 16-byte per-archive salt), and it is bound to the ciphertext as authenticated data, so it can't be altered or swapped undetected.

### The encryption: vox

sandbox's integrity model uses no cryptographic keys (Section 11); nothing in save, merge, verify, the journal, or anchors depends on a secret. Archive confidentiality is a separate concern, handled by [vox](https://jts.gg/vox) (v1.7.3, symmetric core), a small single-file encryption module embedded inside sandbox and loaded into memory only while `pack`, `unpack`, or `export -k` runs. No separate file, no install step, no network. The unused legacy asymmetric interface is not present in the embedded copy.

vox is a misuse-resistant authenticated cipher: an SIV-style AEAD built on HMAC-SHA512, with PBKDF2-HMAC-SHA512 key stretching at 300,000 iterations. A random per-archive salt is mixed into key derivation, so two archives sealed with the same passphrase use different keys and a password guess can't be amortized across archives. sandbox reads and writes one archive format (v2); anything else is refused by name rather than guessed at. Two practical consequences:

- A wrong pass-key or a single altered byte means the archive will not open. vox verifies authenticity before decrypting, and sandbox re-hashes the recovered payload against `db_sha256`; full-repo archives additionally go through the staged verification battery.
- The pass-key is the only thing standing between the archive and its contents. There is no recovery and no key file. A weak pass-key is a weak archive.

Archives are sealed and opened as a stream — the body is built into a temp file, hashed and encrypted a chunk at a time, and on unpack the tag is verified before any plaintext is written — so a multi-gigabyte repository never has to fit in memory. Large tracked files are chunked in the store (Section 14), so the archive references chunks rather than re-embedding whole files.

---

## 14. The storage format

The entire repository is one SQLite database: `.sb/sandbox.db`, in WAL mode, created `0600` and relaxed by the watcher to `0660` so every roster member can write it, with `synchronous=FULL` by default (`sb durability normal` trades the newest-commit-on-power-loss guarantee for speed; the change is journaled).

- Crash safety: every operation commits as a single ACID transaction. (Fossil, the VCS written by SQLite's author, made the same bet fifteen years ago.)
- No small-file sprawl: the repo is one file, so `cp` is a valid backup and `rsync` sees one changed file.
- Real queries: prefix resolution is an indexed `LIKE`; the stat cache and locks are ordinary tables.
- Inspectable with standard database tooling, and anything changed with that tooling behind sandbox's back is flagged by `verify`.

### Schema

| table | contents |
|---|---|
| `meta` | key/value: `format` version, random `repo_id` (chain root), current `branch`, `creator_uid`, `roster`, `durability`, open-merge state |
| `objects` | `hash → kind, size, zlib(data)`: the content-addressed store |
| `refs` | `name → commit hash`: branch tips (empty string = branch with no saves) |
| `journal` | `seq, ts, op, detail(JSON), prev, link`: the append-only hash chain |
| `statcache` | `path → size, mtime, ctime, inode, hash`: change detection without re-reading |
| `locks` | `path → owner, since, base, held, mode, uid, perm`: per-file content locks |

In `locks`, `owner` is the account name shown in output, `uid` is the account the lock belongs to (the only field consulted when deciding whether a lock is yours), `held` is the hash of the content the holder is protecting (or a `deleted` marker when they removed the file), `mode` is the file mode to restore it with, and `perm` is the permission bits the file had before it was locked. A lock recorded by an older version has an empty `held` and adopts whatever is on disk the first time 1.3 sees it.

The watcher keeps a separate, root-owned store at `/var/lib/sandbox/events.db` with `events`, `coverage`, and `repos` tables (Section 6). No repository database ever contains watcher data, and the watcher never opens a repository for writing.

### Object encodings

An object's hash is `SHA-256("<kind> <length>\0" + data)`. Trees and commits are canonical JSON (sorted keys, no whitespace). A tree is `[[mode, kind, hash, name], …]` sorted by name; a commit is `{tree, parents, author, time, message}`, where `author` is the account name. Modes are `100644` (file), `100755` (executable), `040000` (directory), `120000` (symlink).

### The stat cache

`status` and `save` detect changes by comparing each file's size, mtime, ctime, and inode against the cache; on a full match the previous hash is reused and the file isn't read. mtime alone can be restored from userspace (`touch -d`, archive extraction), but ctime is kernel-maintained and the inode changes when an editor replaces a file, so a same-size edit with a restored mtime still misses the cache and gets re-read. Files whose mtime *or* ctime is under two seconds old always bypass the cache; during a save a cached hash is only trusted if the blob exists in the store; `--deep` bypasses the cache entirely. A miss only costs a re-read; the cache fails toward correctness.

### What write paths guarantee

Tree entry names are validated on read (no `/`, `\`, NUL, `.`, `..`, empty, or `.sb`), so a hostile tree can't write outside the repository. Every worktree write (checkout, switch, merge, restore, `undo -p`, lock reverts, archive extraction) goes through a parent directory opened with no-follow semantics, an exclusively-created randomized temp file, a complete-write loop, fsync, and atomic rename. Archive outputs (`pack`, `export -k`) use the same discipline. Directory pruning never touches `.sb`.

---

## 15. Ignoring files

`.sbignore` in the repository root holds one glob pattern per line; `#` starts a comment. A pattern matches the full relative path, that path as a directory prefix, or any single path component:

```
# .sbignore
*.log
build
.env
node_modules
data/*.tmp
```

`sb ignore <pattern>` appends for you and journals it. Always ignored regardless of `.sbignore`: `.sb` itself, `*.sbox` archives, `.git`, `.svn`, `node_modules`, `__pycache__`, `*.pyc`, `*.egg-info`, `.venv`, `venv`, `.DS_Store`, and a list of editor scratch files (`*.swp`, `*.swo`, `*~`, `.#*`, `#*#`, `4913`, `.goutputstream-*`, `*.tmp`, `.~lock.*`). That last group matters more than it looks: those files appear and vanish mid-save, and tracking them would lock files that were never real work and leave dead locks behind.

`.sbignore` itself is tracked, so ignore rules travel with branches.

One behavior to know: ignored files are invisible to `save`/`status` but never deleted by sandbox. An ignore rule only decides what gets *picked up* — a file already in the last save stays tracked even if a later rule matches it, so adding `*.log` will not silently drop a `keep.log` you already committed. To stop tracking a file, delete it (the deletion is saved like any other change) rather than relying on an ignore rule to do it.

---

## 16. Everyday workflows

**Solo project, straight line.** `sudo sb service -i` once on the machine, then `sb init` and work / `sb status` / `sb save` in a loop. Add a pre-save syntax gate (`sb test new pre-save 10-syntax`) on day one.

**Safe experiment.** `sb branch spike && sb switch spike` — the branch already holds your folder as its first save, so there is nothing to set up. Hack with saves as checkpoints. If it works: `sb switch main && sb merge spike`. If it doesn't: switch back and never merge.

**"I broke it ten minutes ago."** `sb diff` to see the damage; `sb undo -p <file>` to reclaim one file from the last save; `sb undo` to revert the whole last save.

**"I deleted everything."** Nothing saved is ever lost: `sb save "oops"` (yes, save the wipe), then `sb undo`. Both the deletion and its reversal live in history.

**"It worked last Tuesday."** Find last Tuesday (an anchor you noted, a save in `sb log`, or a release label) and `sb restore <it>`.

**Bringing someone onto a project.** `sb useradd alice`. She now owns nothing and can write everything unlocked; the moment she edits a file it becomes hers until she saves. `sb users` shows the roster, `sb locks` shows who is holding what. If she leaves, `sb userdel alice` — and if she left locks behind, `sb unlock <path> --force` releases them, journaled with her name on the record.

**Small team, one machine or one attached disk.** Everyone works in the same folder. The watcher records who wrote each file, so locks land on the actual editor; your version of a locked file stays put and anyone else's edit to it is put back (recoverable with `sb salvage`); `sb save` commits only your files; abandoned locks auto-save in their owner's name after an hour and free themselves. Merges refuse to clobber locked work; `sb merge feat -i` proceeds around it as a recorded partial merge and completes on re-merge.

**"My edit got reverted."** The file belongs to someone else's lock. `sb journal` shows the `lock-revert` entry with the hash of what you wrote; `sb salvage <hash> mine.txt` gets it back, and you can hand it to the lock holder or apply it after they save.

**"sb says it can't tell who changed this."** The watcher was stopped when the edit happened. `sb service -s` to confirm, `sudo sb service -i` to bring it back. The change is still there and still saveable — sandbox just refuses to invent an author for it.

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

## 17. sandbox versus git

| | **sandbox** | **git** |
|---|---|---|
| Mental model | work → save | work → stage → commit (+ index states) |
| Staging area | none; a save is what you see | the index, with its own command set |
| Detached HEAD | impossible | routine source of confusion |
| Destroying history | no command does it | `reset --hard`, `push -f`, dropped stashes, expired reflog |
| Undo | `sb undo`, a new save, reversible | `revert` vs `reset` vs `restore` vs `checkout` |
| New branch | born with a save of your folder | a pointer; content comes later |
| Identity | your Linux account; nothing to configure | `user.name` / `user.email`, set to anything |
| Who edited a file | kernel-recorded per write, or explicitly reported as unknown | not tracked at all |
| Repository format | one crash-safe SQLite file | thousands of loose files + packfiles + refs + index |
| Operation audit log | hash-chained journal of every operation, bypasses included, cross-checked vs refs | reflog: per-machine, expiring, mutable, unchained |
| Tamper evidence | chain + tip cross-check + external anchors | commit DAG only; refs/reflog unprotected |
| Secret prevention | redacted at save time by default, journaled | third-party hooks you must install |
| Test enforcement | versioned gates on clean checkouts, on by default | hooks: unversioned, per-clone, easily absent |
| Renames | similarity-based detection in status/diff/log, rename-aware merges | similarity-based detection, rename-aware merges |
| Merge conflicts | auto-merge non-overlap; conflict markers in the worktree, `sb save` finishes or `sb merge --abort` drops it | conflict markers + in-progress merge state |
| Small-team sharing | one repo, per-file content locks backed by ownership and ACLs, always on | clone/push/pull, remotes |
| Remotes / distributed collaboration | not yet (roadmap) | git's core strength |
| Platform | Linux (fanotify + POSIX ACLs) | everywhere |
| Ecosystem | one file, zero deps | vast |

Summary: git is a distributed collaboration system you can also use alone; sandbox is a safety-and-integrity system for individuals and small teams sharing a machine. If you need GitHub-style multi-party collaboration today, use git, possibly with sandbox alongside it (they coexist; sandbox ignores `.git`, add `.sb` to `.gitignore`).

---

## 18. Environment variables

| variable | effect | default |
|---|---|---|
| `SB_PASSKEY` | pass-key for `pack` and `unpack` when `-k` is absent, and for `export -k ''` | prompt |
| `SB_TEST_TIMEOUT` | seconds allowed per test script | `120` |
| `SB_LOCK_TTL` | seconds a lock survives without activity before auto-save + release | `3600` |

sandbox also reads `SUDO_UID` and `SUDO_USER` when running under `sudo`, so an elevated command is still attributed to the invoking account rather than to root. There is nothing to set — `sudo` provides them. They are advisory only: the watcher records the kernel's `loginuid`, which cannot be forged this way.

`SB_NAME`, `SB_EMAIL`, and `SB_HOME` no longer exist. Identity comes from the OS account, and there is no profile file to locate.

Inside test scripts, sandbox exports `SB_STAGE`, `SB_BRANCH`, `SB_COMMIT`, and `SB_REPO` (Section 9).

---

## 19. Exit codes

| code | meaning |
|---|---|
| `0` | success |
| `1` | usage or state error (not a repo, watcher missing at `sb init`, unsaved changes, unknown branch, bad arguments, non-empty unpack destination, corrupt object hit mid-operation, filesystem error, …) |
| `2` | a gate stopped you: unredactable secrets, failed test gates, merge conflicts, or `verify` found problems |
| `130` | interrupted (Ctrl-C) |

The split is script-friendly: `2` means sandbox worked correctly and blocked something on purpose, so automation can tell "fix your command" from "fix your content."

---

## 20. FAQ

**Where did `sb who` go?**
Removed. There is no configured identity in 1.3 — no name, no email, no profile file. You are the Linux account running the command, and saves are attributed to that account's name. `sb info` shows the account you are acting as, and `sb users` shows everyone with access to the repository.

**Why does `sb init` refuse to run until I install a service?**
Because without it, sandbox cannot tell who wrote a file, and would have to guess from ownership — which is wrong for every in-place edit. Section 6 explains the mechanism. It is one command, once per machine: `sudo sb service -i`.

**What does the watcher actually see?**
Completed writes on the mounts where registered repositories live: path, inode, uid, loginuid, pid, and process name. Writes outside any repository are recorded briefly and dropped after five minutes; writes inside `.sb` are ignored entirely. It never reads file *contents*, and it never writes to a repository database.

**Can I use sandbox on macOS?**
Not for creating repositories. `sb init` needs the watcher, and the watcher needs fanotify, which is Linux-only. The ACL-based access model is likewise Linux. WSL2 works where its kernel provides both.

**What happens if I stop the watcher?**
Existing repositories keep working — you can save, branch, merge, everything. What changes is that new edits can't be attributed. sandbox prints a warning that the watcher is stopped, and any change made outside a coverage window is left unlocked and reported as unattributed rather than assigned to a guess. Access rules also stop being re-applied, so file ownership drifts until it comes back.

**Why didn't my secret block the save?**
Because 1.3 redacts instead of blocking. The credential is replaced with `<REDACTED>` in the committed blob, your file on disk is untouched, and the journal records which files were redacted. Only a file that can't be rewritten faithfully (not clean UTF-8) still blocks. Section 10.

**Someone else has a file locked and my edit keeps reverting. Where did my work go?**
Into the object store. The revert is journaled as `lock-revert` with the hash of exactly what you wrote; `sb salvage <hash> [<path>]` writes it back out to any filename you like. Nothing you typed is destroyed — sandbox just refuses to let a second writer's copy become the version of record.

**Why couldn't I even save the file? My editor said permission denied.**
That's the lock doing its job at the filesystem level. A locked file is owned by its holder with a narrowed ACL, so other accounts can't write it in the first place. `sb locks` tells you who holds it and when it expires.

**Can I turn locking off?**
No. Shared operation is structural in 1.3, not a setting. In a repository with one account it never does anything visible: you are always your own lock holder, and your own edits move your locks forward.

**Why does `sb branch` create a save?**
So a branch is never an empty name. It can be switched to, tested, exported, and merged the moment it exists, and there is no "save something first" step before a merge. If the branch you were on had no saves, it is seeded with the same commit so the two share a base.

**I merged a branch that doesn't have my file, and the file survived. Bug?**
Intended. In a three-way merge a file that only your side has never existed in the base, which reads as "only we changed it," so it is kept. Deleting it would mean inventing a deletion the other branch never made. A file is removed only when it existed in the base and the other side deleted it.

**Does rename detection catch a file I moved and edited?**
Yes, as of 1.3, provided the two versions still share at least half their content. Detection runs exact-hash first and then falls back to piece-hash similarity (Section 4). The pair is reported as one rename, so `sb diff` shows a single header line for it rather than a diff of the edit.

**Where did the signatures go? Is sandbox less secure now?**
The Ed25519 signing was removed deliberately; Section 11 has the full reasoning. Keys with no management story prove nothing, and a hand-rolled fallback implementation is a liability. The properties the signatures actually provided (integrity, tamper evidence) are covered by content re-hashing, the journal chain, the ref cross-check, and anchors — and attribution is now stronger than a signature over a self-declared identity ever was.

**Is SHA-256 "cryptography"? I asked for none.**
It's a hash function from Python's standard library used as a content fingerprint. No keys, no signatures, no third-party crypto code. Content addressing, integrity checking, and the journal are all built on it.

**How do I back up a repository?**
Copy `.sb/sandbox.db` (any time sandbox isn't mid-command; WAL makes even that forgiving), copy the whole project folder, or `sb pack` for an encrypted single-file backup. After restoring, run `sb verify`.

**Can I have partial commits, like `git add -p`?**
No, by design: a save is exactly your working tree, which is what makes "the tests passed on this save" meaningful. Two unrelated changes belong on two branches or in two saves. The your-files-only save is the one exception, and it exists to protect other people's in-progress work.

**What about large or binary files?**
Stored (zlib-compressed) and versioned like anything else; `diff` summarizes them in one line and the redaction pass skips binaries. A file at or above 8 MiB is split into content-addressed 1 MiB chunks (Section 14), so editing a few bytes of a large file stores only the changed chunks rather than a whole new copy, and the file is hashed, stored, checked out, and archived a chunk at a time without ever being held in memory whole.

**Symlinks?**
Tracked as content — the link's target path is stored, and checkout, switch, merge, export, and archives all recreate it as a real symlink. A symlink is never *followed* on write: every write path opens parents with no-follow semantics, so a link (even one pointing outside the repo, or planted at a target name) is stored and restored as a link and can't redirect a write out of the repository. Two sides changing a link's target is a conflict, since merging a path line-by-line could produce a target that points nowhere.

**Can two commands run at once?**
Yes. SQLite serializes writers, and racing saves are protected by a compare-and-swap on the branch tip plus a worktree drift check. A race ends with one clean success and one "run it again" error, never corruption.

**Does anything leave my machine?**
No. There is no network code in sb.

**Can I rename a branch?**
Not built in yet. Today: `sb branch new-name && sb branch old-name -r` (from another branch). Removal is journaled and never deletes saves.

**Can `unpack -i` be undone?**
No. `-i` overwrites files in place with no per-file backup. That's why it's a flag: without it, unpack refuses any non-empty destination, so overwriting only happens when you asked for it.

**Who ends up owning a lock if Bob's `sb status` discovers Alice's edit?**
Alice. The watcher recorded that her account performed the write, matched by inode and time window. Bob running a command is not evidence of anything, and sandbox doesn't treat it as such.

---

## 21. Troubleshooting

**`error: the write-attribution watcher is not installed`** — run `sudo sb service -i` once on this machine, then `sb init` again. If it's installed but stopped, the same command restarts it; `sb service -s` shows the current state.

**`error: not inside a sandbox repository`** — you're outside any folder containing `.sb/sandbox.db`. `cd` in, or `sb init`.

**`the write-attribution watcher is not running`** (as a warning during a command) — everything still works, but new edits can't be attributed. `sudo sb service -i`.

**`<file> changed while nothing was watching`** — the watcher was down when that edit happened, so sandbox won't guess who made it. The file is left unlocked and unattributed. Restart the watcher; save the change as normal.

**`error: sb <command>: unrecognized arguments: …` / `missing: …`** — the flag or argument doesn't exist for that command; the correct usage line is printed below the error, and if the flag belongs to a different command, that's named too.

**`error: 'who' is not an sb command`** — `sb who` was removed in 1.3. Identity is your Linux account; `sb info` shows what you're being attributed as, and `sb users` shows who's in the repository.

**`error: no such system account: <name>`** — `sb useradd` only accepts accounts that already exist on the machine. Create the user first (`sudo useradd …`), then add them.

**`error: the creator cannot be removed`** — the creator owns every unlocked file in the repository. To hand a project over, `sb pack` it and have the new owner unpack it under their own account.

**`error: you have unsaved changes`** — `switch`, `merge`, `undo`, `restore`, and `publish` refuse to run over uncommitted work. `sb save "wip"` (saves are cheap, undo is free), or `sb undo -p <path>` for changes you want gone. Other people's locked files never trigger this, and neither do files that differ only by a redacted secret.

**`reverted N file(s) to their lock holder's version`** — you edited files someone else holds locks on. Their versions are back on disk; yours are stored, and the message and journal give you the hashes for `sb salvage`.

**`nothing of yours to save` / `N file(s) belong to other people's locks`** — everything you changed is locked by someone else, so there was nothing for your save to commit. Wait for them to save, or ask them to.

**`merge blocked — it would change files locked by others`** — protecting a teammate's in-progress edit. Wait, ask them to save, or `sb merge <branch> -i` to proceed around those files as a recorded partial merge.

**`save blocked — secrets in files that cannot be safely redacted (not clean UTF-8)`** — the file holds a credential but isn't text sandbox can rewrite without corrupting it. Remove the secret, `sb ignore` the file, or `--allow-secrets` (journaled). Section 10.

**`secrets redacted in the save (working files untouched)`** — not an error. The save happened; history holds `<REDACTED>` and your files are unchanged. Move the credential to an environment variable or an ignored file so it stops recurring.

**`error: <folder> is not empty — unpack into a fresh folder`** — unpack never writes into a destination that already contains anything. Pick a fresh folder, or add `-i` to overwrite matching files on purpose.

**`the archive's repository failed verification — nothing was written`** — the store inside the `.sbox` is damaged or was tampered with; unpack refused before touching the destination. Get a good copy of the archive.

**`pre-save tests failed — save blocked`** — the failing script's last 15 lines are printed above the error. Reproduce with `sb test pre-save`. Override once with `--no-verify` (journaled), then fix the gate.

**`merge of <branch>: N file(s) need you`** — the merge is applied to your worktree and left open, with `<<<<<<< ours` markers in the conflicting text files (binary and symlink conflicts keep your version instead, since there's nothing to mark up). Edit them, then `sb save "<message>"` to finish — it becomes a real two-parent merge commit and is refused while any marker remains. `sb merge --abort` puts the folder back exactly as it was. While the merge is open, `switch`, `branch`, `restore`, `undo`, `publish`, and another `merge` are blocked.

**`conflict markers are still in <file>`** — a marker line survived your edit. Remove the `<<<<<<<` / `=======` / `>>>>>>>` lines along with whichever side you don't want, then save again.

**`a merge of '<branch>' is still open`** — finish it with `sb save "<message>"` or drop it with `sb merge --abort` before running the command you tried.

**`object … does not match its hash` / `verify` reports problems** — real corruption or tampering; sandbox stopped rather than propagating it. Restore `.sb/sandbox.db` from a backup, then `sb verify`. Undamaged files can be rescued first via `sb undo -p` or `sb export` from saves whose objects are intact.

**`branch '…' changed under this operation … run the command again`** — two sandbox commands raced; yours lost the compare-and-swap and nothing was changed. Re-run it.

**`store error: … database is locked`** — another sandbox command has the database open for writing. Wait a moment and retry.

**`file system error: …`** — a permission or disk problem outside sandbox's control, reported cleanly. If it's a permission error on a repository file, check `sb users` (are you actually a member?) and `sb locks` (does someone else hold it?), and confirm the watcher is running so access rules are being applied.

**A file isn't being saved** — it matches an ignore rule (and wasn't already tracked), or someone else holds a lock on it (`sb status` marks those `(theirs)`), or it differs from history only by a redacted secret. Check `.sbignore` and the built-in defaults (Section 15).

---

*sb — one file, no dependencies, nothing silently destroyed.*
