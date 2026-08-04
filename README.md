# sandbox (sb)

**Version 1.3** · [jts.gg/sandbox](https://jts.gg/sandbox)
**License** · [jts.gg/license](https://jts.gg/license)

Optimized local version control built for Debian Linux. No dependencies beyond Python 3.9

The whole repository is one SQLite database. Snapshots are content-addressed and parent-linked, but there is no staging area and no detached HEAD — a save is your working folder as it stands. Several people can share that folder, so sandbox asks the kernel which account wrote each file and hangs locking and file access off the answer.

---

## Table of contents

1. [Installation](#1-installation)
2. [Design](#2-design)
3. [Quickstart](#3-quickstart)
4. [Core concepts](#4-core-concepts)
5. [Command reference](#5-command-reference)
6. [The watcher](#6-the-watcher)
7. [Accounts and file access](#7-accounts-and-file-access)
8. [Shared editing and locks](#8-shared-editing-and-locks)
9. [Test gates](#9-test-gates)
10. [Secrets and redaction](#10-secrets-and-redaction)
11. [Security model](#11-security-model)
12. [Anchors](#12-anchors)
13. [Portable archives (.sbox)](#13-portable-archives-sbox)
14. [Storage format](#14-storage-format)
15. [Ignoring files](#15-ignoring-files)
16. [Workflows](#16-workflows)
17. [sandbox versus git](#17-sandbox-versus-git)
18. [Environment variables](#18-environment-variables)
19. [Exit codes](#19-exit-codes)
20. [FAQ](#20-faq)
21. [Troubleshooting](#21-troubleshooting)

---

## 1. Installation

Linux, Python 3.9+, standard library only. Three kernel facilities are required and none have portable equivalents: **fanotify** (write attribution), **POSIX ACLs** via the `system.posix_acl_access` xattr (per-account file access), and **directory descriptors with no-follow semantics** (symlink-safe writes). macOS has neither of the first two. WSL2 works where its kernel provides them.

```bash
curl -sL install.jts.gg/sandbox | sudo bash    # system-wide
curl -sL install.jts.gg/sandbox | bash         # ~/.local/bin
```

Manual:

```bash
cp sb.py ~/.local/bin/sb && chmod +x ~/.local/bin/sb
```

Then install the watcher, once per machine:

```bash
sudo sb service -i     # install and start
sb service -s          # confirm
```

`sb init` refuses to run unless the watcher is installed and running. Without it, an in-place write (`>>`, `sed -i`, any editor that truncates) leaves no record of who made it, and sandbox would have to attribute the edit to whoever happens to own the file. Section 6.

Existing repositories register themselves with the service on the next command.

**Upgrading.** Re-run the installer, then `sudo sb service -i` so the unit points at the new binary. A repository written by a newer format is refused with a clear message rather than opened. `sb verify` confirms a store and its history still agree.

---

## 2. Design

**Nothing saved is destroyed.** `undo` writes new history that reverts the old. `switch` refuses to run over unsaved work. A lock revert stores the bytes it displaces. The store is one SQLite database, so every operation is one atomic transaction.

**Simple, not small.** No staging area, no detached HEAD, no rebase. Real branches, three-way merges, similarity-based rename detection, versioned test gates, release records, per-file locks, full-store verification.

**Identity is not configuration.** You are your Linux account. The roster of accounts on a repository grants write access; a kernel-backed service records which account performed each write. None of it can disagree with the machine it runs on.

**Security claims.** Integrity, tamper evidence, leak prevention — each with a stated mechanism and stated limits (Section 11). No keys, no signatures. SHA-256 from the standard library, for content addressing and hash chaining.

---

## 3. Quickstart

```bash
sudo sb service -i               # once per machine

cd my-project
sb init                          # .sb/sandbox.db, branch "main", you as creator

sb status                        # what changed
sb diff                          # line-by-line
sb save "add login form"         # snapshot

sb branch idea                   # creates it AND saves this folder onto it
sb switch idea
sb save "risky refactor"
sb switch main
sb merge idea                    # 3-way; non-overlapping edits merge themselves

sb undo                          # revert the last save, as a new save
sb undo -p src/app.py            # one file back from the last save
sb restore 67b3dea8b260c12a      # any past anchor, save, or release

sb useradd alice                 # grant a linux account write access
sb users                         # roster
sb locks                         # who holds what

sb publish v1.0                  # verify + pre-publish gates + journal the release
sb export v1.0 out/              # that release as plain files
sb export v1.0 -k "release-key"  # or as an encrypted .sbox

sb verify                        # re-hash everything, check the chain
sb journal                       # what sb has done

sb pack -k "pass-key"                        # seal repo + history
sb unpack my-project.sbox -k "pass-key"      # restore elsewhere
```

---

## 4. Core concepts

### Saves

A save snapshots every tracked file with a message, an author, and a timestamp. No staging: what's in the folder, minus ignored files, is what gets committed — which is what makes "the tests passed on this save" mean something.

One refinement makes a shared folder workable: a save commits *your* files and leaves everyone else's in-progress edits alone, on disk and in the commit (Section 8). `--global-force` sweeps in everything and says so in the journal.

Each save records its parent's hash, forming a chain, and a DAG once merged.

### Identity

The author of an operation is the login name of the account running it, from the system password database. Under `sudo`, sandbox resolves the invoking account from `SUDO_UID` rather than reporting root.

A repository also has a **creator** (the uid that ran `sb init`) and a **roster** (uids added with `sb useradd`). Those drive file ownership and access (Section 7).

### Branches

Creating a branch immediately saves the working folder onto it as `Initial branch creation`, so it can be switched to, tested, exported, and merged at once. If the current branch has no saves, it is seeded with the same commit, giving both a shared base.

You are always on exactly one branch. `switch` rewrites the folder to the branch tip and refuses to run over unsaved changes, unless the folder already equals the target tree.

### Locks

Editing a file locks it to you, recording the exact content you're protecting. While held, your version is the file, other accounts can't write it, and only you can commit it. Always on; invisible in a single-account repository. Section 8.

### Object store

Objects are keyed by the SHA-256 of their content, so identical files are stored once.

| kind | contents |
|---|---|
| `blob` | file bytes |
| `chunk` | one ≤1 MiB piece of a large file |
| `chunked` | ordered chunk hashes for one file |
| `tree` | `[[mode, kind, hash, name], …]`, canonical JSON |
| `commit` | `{tree, parents, author, time, message}`, canonical JSON |

Files ≥8 MiB are stored as `chunked` over 1 MiB pieces, so a small edit to a large file stores only the changed chunks and nothing reads the file whole. A `chunked` object addresses to the SHA-256 of the reassembled content, exactly as a blob would, so the split is invisible above the store layer.

Verification happens at both levels: each chunk is re-hashed as it's read, and the reassembled whole is re-hashed against the object's own hash before the last byte is handed over. Per-chunk checks alone wouldn't catch a chunk *list* that was reordered or repointed at other valid chunks. Because callers write through an atomic temp file, a failure at the end means nothing reaches the working folder.

Every object is re-hashed on every read, not only during `verify`.

Symlinks are stored as a blob holding the target path, mode `120000`.

### Journal

An append-only log of every operation, each entry embedding `SHA-256(canonical entry ‖ previous link)` and rooted in a random repository ID. Ops: `init`, `ref` moves (save, merge, undo, restore, branch, autosave), `branch-remove`, `switch`, `merge-open`, `merge-abort`, `publish`, `unlock`, `lock-revert`, `useradd`, `userdel`, `ignore`, `durability`, `pack`, `export`, `unpack`.

So: editing or deleting an entry breaks the chain; moving, deleting, or injecting a branch tip behind sandbox's back is caught by `sb verify` cross-checking refs against the journal; replacing an object is caught by the content re-hash.

Anything unusual about a save is part of the record and shown by `sb journal`: `· no-verify`, `· secrets-override`, `· global-force`, `· redacted`, `· secrets-present`, `· initial-save`, `· seeded`. Skipping a gate is allowed; hiding it is not.

### Renames

Two layers, used by `status`, `diff`, `log`, and merges.

**Exact content** — a deleted path and an added path with identical content pair immediately. Empty files never pair.

**Similarity** — for a file moved *and* edited, whose hash differs by definition. Each candidate is hashed in pieces and pairs sharing ≥50% of them (Jaccard) are reported as renames. Pieces are non-blank lines for text; for binary, boundaries come from a rolling hash over a 48-byte window, so an inserted byte shifts one piece instead of every later boundary. An inverted index from piece hash to candidate means only files that actually share content are compared, so cost tracks real overlap rather than added × deleted.

Merges use the same detection: a rename on one side and an edit under the old name on the other combine. Different renames on both sides conflict.

A detected rename is reported as one pairing, not as an add plus a delete. `sb diff` prints a single `@@ old → new  renamed (content identical)` header for the pair, with no diff body.

---

## 5. Command reference

Positional arguments say what; options say how. Routine options have short and long forms (`-k/--key`, `-f/--files-only`, `-i/--ignore`, `-n/--limit`, `-r/--remove`, `-a/--anchor`, `-l/--list`, `-p/--path`, `-s/--status`). Safety overrides (`--allow-secrets`, `--no-verify`, `--global-force`, `--force`) have none — bypassing a gate or taking someone's lock gets typed out. Subactions are words: `sb test list`, `sb publish list`.

Bad arguments produce one line of explanation, that command's usage line, and a pointer to `sb help`. A flag that belongs to a different command is named as such.

`status`, `save`, `undo`, `restore`, `branch`, `switch`, `merge`, `publish`, `locks`, and `unlock` synchronize locks before their own work — enforce, expire, acquire, re-apply permissions. That's how a tool with no per-repository daemon keeps a shared folder consistent.

### `sb init`

Creates `.sb/sandbox.db` (mode `0600`), branch `main`, a journal seeded with a random repository ID, and records you as creator. Registers the repository with the watcher. Fails if a repository exists here, or if the watcher isn't installed and running.

### `sb status [--deep]`

Branch, latest save, active locks (`their version wins` for other people's), files that differ from history only by redaction, and changes since the last save: `renamed old → new`, `new`, `modified`, `deleted`. Files under someone else's lock are marked `(theirs)` and excluded from what your next save would commit. `--deep` skips the stat cache and re-hashes everything.

### `sb save "<message>" [--allow-secrets] [--no-verify] [--global-force]`

1. Sync locks: revert other people's locked files, expire abandoned locks, take locks on what you edited.
2. Select your changed files, excluding every file another account holds.
3. Redact recognized credentials in the blobs about to be committed; the files on disk are untouched. `--allow-secrets` commits verbatim. A file holding a credential that isn't clean UTF-8 can't be rewritten faithfully and blocks the save.
4. Run pre-save gates against a clean checkout of the candidate tree.
5. Re-check that the worktree didn't move during scanning and testing.
6. Store blobs, build the tree, write the commit, move the tip (compare-and-swap), release your locks, restore file access, journal — one transaction.

With a conflicted merge open, `sb save` finishes it as a two-parent merge commit, and is refused while any conflicted file still holds an `<<<<<<< ours` marker.

`--global-force` commits the whole worktree and releases every lock, journaled as such.

### `sb log [-n <count>]`

Newest first: hash, date, author, message, `(merge)`, and a per-save change summary (`+2 new · ~1 modified · old.txt → new.txt`).

### `sb diff [<path>]`

Unified diff against the last save. Renames collapse to one header line. Files under another account's lock are labelled. Binaries get a one-line size summary.

### `sb undo [-p <path>]`

Reverts the latest save as a *new* save; the undone save stays in the log, and running it again redoes. Requires a clean tree, other people's locks excepted.

`-p <path>` restores just that path from the last save and releases your lock on it, without creating a save. Refused if someone else holds it.

### `sb restore <anchor | save | release | branch>`

Returns the branch to a past state as a new save; `sb undo` takes you straight back. Requires a clean tree (same exception). Accepts an anchor (8–64 hex of a chain link), a save-hash prefix (4+), a release label, or a branch name. Ambiguous targets are rejected with the list of matches.

### `sb branch [<name>] [-r] [--allow-secrets]`

No argument lists branches and tips. A name creates the branch and saves the folder onto it. `-r` deletes a branch pointer — never the current one, never the last — leaving its saves in the store and journal.

The initial save follows the normal rules: credentials redacted, and files under other accounts' locks taken from the last save rather than from disk.

### `sb switch <branch>`

Rewrites the folder to the branch tip. Refuses over unsaved changes unless the folder already matches. Locked files are left alone; emptied directories are pruned. An unknown name is an error, not a new branch.

### `sb merge <branch> [--no-verify] [-i]` · `sb merge --abort`

Three-way merge using the lowest common ancestor (correct after prior merges; criss-cross histories resolve deterministically by time then hash).

- Current branch is an ancestor → fast-forward, still gated by pre-merge tests.
- Changed on one side → that side wins.
- Present on one side and absent from the base → kept. Deletion requires the base to have had it.
- Rename-aware: a rename on one side follows an edit made under the old name on the other. Renamed-vs-deleted, and two different renames, conflict.
- Changed on both sides → line-level three-way merge. Non-overlapping edits combine, including on adjacent lines; CRLF and missing-final-newline states are preserved. Overlaps, differing insertions at one point, binaries, differing exec bits, and two changed symlink targets conflict.

Pre-merge gates run against the merged tree and discover their scripts from it.

On conflict the merge lands in the worktree with `<<<<<<< ours` / `=======` / `>>>>>>> theirs` markers and stays open — `switch`, `branch`, `restore`, `undo`, `publish`, and another `merge` are blocked. Resolve and `sb save "<message>"`, or `sb merge --abort` to put the folder back. Binary and symlink conflicts have nowhere to put markers, so they keep your version and are listed.

A merge that would change a file someone else holds is refused. `-i` skips those files: everything else merges, each skipped file keeps your version and its lock, and the result is recorded as a **partial merge** — single-parent, not a merge commit — so re-running after the locks release brings in what was skipped.

### `sb test [<stage>]` · `sb test new <stage> <name>` · `sb test list` · `sb test guide`

Run gates manually (all, or `pre-save` / `pre-merge` / `pre-publish`), scaffold a script, list what's discovered, or print the walkthrough. A manual run tests the current worktree. Exit `0` or `2`.

### `sb publish [<label>]` · `sb publish list` · `-l` · `--no-verify`

Records the current save as a release behind two gates: full store verification, then pre-publish tests on a clean checkout of HEAD. Requires a clean worktree and no open merge. Journals what, from which branch, by whom, when, and the content hashes of the scripts that ran, then prints the anchor. `-l` lists releases and reports whether the chain still verifies.

A release is a record; `sb export` produces the files.

### `sb export <version> [<destination>] [-k <passkey>]`

Materializes a release label, branch, or save prefix as plain files — exec bits preserved, no `.sb`. Destination defaults to `<repo>-<version>/` and must be empty. Every blob is re-hash-verified on the way out, and a plain folder export writes nothing to your repository.

`-k <passkey>` produces an encrypted files-only `.sbox` instead, carrying label, commit, and sealed-by metadata, and journals the export. Drop it with `sb unpack <file> <path> -k <passkey>`, adding `-i` to redeploy.

### `sb verify [-a <hash>]`

Re-hashes every object (including history from removed branches and orphans from interrupted operations), validates tree entry names, recomputes the chain, and cross-checks every branch tip against the journal. Malformed objects and unexpected refs are reported as findings, not crashes. `-a` additionally confirms a noted anchor.

```
checked 18 objects across 5 save(s)
  ├─── content hashes  all valid ✓
  ├─── journal chain   11 entries linked ✓
  ├─── branch tips     match the journal ✓
  └─── anchor          8977ecba8bd79985  (save it · check later: sb verify -a <hash>)
history is intact ✓ — store, journal and refs all agree
```

### `sb journal [-n <count>]`

Every operation with its detail: ref moves with old → new hashes and any bypass or redaction markers, releases, switches, branch removals, opened and aborted merges, roster changes, lock releases (`· forced` when forced), lock reverts with the hashes of the bytes kept, ignore rules, durability changes, and pack/export/unpack. Each line ends with that entry's 16-character chain link, usable as an anchor. Re-verifies the chain at the end.

### `sb info`

Store path and size, branch, object counts, journal length, current anchor, active locks, and the account you're acting as.

### `sb service -i | -s | -k`

- `-i` — install and start. Needs root. Writes and enables `sandbox-watch.service` under systemd, otherwise starts a detached process logging to `/var/lib/sandbox/watch.log`. Stops any existing watcher first, so it doubles as restart.
- `-s` — installed, running, event count, seconds since the last heartbeat, registered repositories, pid.
- `-k` — stop, retrying until the lock is free so no straggler survives.

### `sb useradd <linux-user>` · `sb userdel <linux-user>` · `sb users`

`useradd` adds an existing system account to the roster and re-applies access; they can then edit any unlocked file. `userdel` removes it and re-applies access; the creator can't be removed, and any locks they still hold are reported with the `sb unlock --force` needed to free them. `users` lists the roster with `creator · owns unlocked files`, `you`, and each account's lock count. Both changes are journaled.

### `sb durability [full|normal]`

`full` (default): the newest committed transaction survives power loss. `normal`: faster, still crash-safe, may lose the most recent commit. Journaled.

### `sb locks`

Path, holder, how long held, minutes to expiry, and a short hash of the protected content (`· file removed` if they deleted it).

### `sb unlock [<path>...] [--force]`

Releases your locks and restores each file's previous permission bits. `--force` releases other people's and, where sb has the privilege, takes ownership of the file — without that, the next scan reads the old owner and hands the lock straight back. Journaled with the paths, prior owners, and any ownership taken. Content is never changed.

### `sb salvage <hash> [<path>]`

Writes stored content back to a file — the other side of a lock revert, whose journal entry gives you the hash. 4–64 hex, unambiguous; destination defaults to `salvaged-<hash>` and is never overwritten.

### `sb ignore <pattern>`

Appends to `.sbignore` and journals it.

### `sb pack [<output>] -k <passkey> [-f]`

Seals the repository into one encrypted `.sbox` (Section 13). Defaults to `<foldername>.sbox`; never overwrites. Written through an exclusively-created temp file and renamed, so a planted symlink can't redirect it. Warns about unsaved changes, since pack seals saved history. `-f` seals only the current save's files.

### `sb unpack <file.sbox> [<destination>] -k <passkey> [-f] [-i]`

A wrong pass-key or altered archive fails cleanly, writing nothing. A full-repository archive is staged and verified — objects re-hashed, chain recomputed, refs cross-checked — before anything is installed.

```
unpacked my-project · 3 file(s)
  ├─── sealed by  jordan  · 2026-07-14 08:35
  ├─── branch     main · anchor bd40a7878f681649
  └─── verified before install ✓ — store, journal and refs all agree
```

Destination must be fresh or empty; `-i` merges into a non-empty one, overwriting matching paths and keeping the rest, with no per-file backup. `-f` writes only the files, no `.sb` — files-only archives unpack this way automatically and journal nothing, having no store to journal into. A full unpack journals itself in the repository it installs.

### `sb version`

Prints `sb 1.3 · jts.gg/sandbox`. `-V` works too.

---

## 6. The watcher

Everything in a shared folder rests on one question: **which account wrote these bytes?**

The filesystem can't answer it. It records who *owns* a file, never who wrote to it, and an in-place write leaves ownership untouched. Attributing by ownership credits whoever created the file — usually wrong, and wrong in the direction that takes someone's work and files it under another name.

### What it is

One process per machine, installed with `sudo sb service -i`. It opens a single **fanotify** descriptor and places a *mount mark* on each mount holding a registered repository; mount marks are recursive and cover directories created later, so coverage is complete rather than best-effort. Every `FAN_CLOSE_WRITE` arrives with the writing pid, and `/proc/<pid>` turns that into an account. Where the kernel supports it, sandbox requests pidfd reporting so a short-lived process can't exit before it's identified.

Both the real uid and the **loginuid** are recorded. loginuid is set by PAM at login and can't be changed without `CAP_AUDIT_CONTROL`, so unlike `$SUDO_UID` it survives `sudo` and `su`.

fanotify is reached through `ctypes` against libc — no package, no auditd, no dependency.

### What it writes

Only its own store. Repository databases are opened **read-only**; a root-owned writer inside a user's SQLite file is how you get root-owned WAL sidecars and two writers on one database.

```
/var/lib/sandbox/
  events.db      event store (0644, root-owned, world-readable)
  repos.d/       one file per registered repository (1777, sticky)
  watch.lock     singleton lock
  watch.pid      current pid
  watch.log      output when running without systemd
```

`repos.d` is world-writable with the sticky bit, like `/tmp`: anyone may register a repository, nobody may remove another's. `sb init` isn't a root command, so registration can't go through the root-owned database. An entry only takes effect if the path actually holds a repository, and at most 512 are watched.

Events are pruned on a rolling basis: writes outside any repository after five minutes, everything else after 30 days. Writes inside `.sb` are ignored outright.

### Coverage

A watcher that silently missed events would be worse than none. Each run writes a **coverage window**: start time, a heartbeat every 5 seconds, and a clean-shutdown flag. Windows carry fractional seconds, so an edit made right after a clean stop falls outside rather than tying with it.

To attribute a change, sandbox looks for an event that could explain *this* change:

1. Match on **(device, inode)**. The inode identifies these bytes whether they were written in place or renamed in from a temp file — what most IDEs do, and what a name-keyed watch would miss.
2. Otherwise match on path.
3. Either way, only events at or after the file's mtime (minus one second for whole-second rounding) count. An event predating the change can't have produced it.

No match but the mtime falls inside a window → fall back to the file's owner. Mtime outside every window → say so and stop:

```
  ├─── notes.txt changed while nothing was watching
  └─── the watcher was not running, so sb cannot say who wrote these.
       they are left unlocked and unattributed — sudo sb service -s
```

That message is never suppressed.

Only one watcher may run, enforced by an exclusive advisory lock rather than a pid file: the kernel drops it however the holder dies, so a killed watcher leaves no ghost and two starts can't race. Duplicates would each open a coverage window, so stopping "the" watcher would leave others still claiming everything was observed.

### Enforcement

The watcher is also the only thing on the machine that can hand a file between accounts, so it applies the access rules in Section 7 — on every observed write inside a repository, and every few seconds for any repository whose store changed. `sb` runs as whoever typed the command and usually can't `chown`; without this, a lock holder could end up unable to write the file they just locked.

---

## 7. Accounts and file access

A repository has a **creator** (ran `sb init`) and a **roster** (`sb useradd`). Membership grants write access; a lock narrows it to one person.

```bash
sb users
sb useradd alice        # must already exist as a system account
sb userdel alice        # the creator cannot be removed
```

The rule:

- **Unlocked** → owned by the creator, writable by every roster member.
- **Locked** → owned by the holder, writable only by the holder.

Directories are creator-owned at `0755` with `rwx` for each member. That isn't cosmetic: replacing a file needs write permission on its *containing directory*, so write-temp-then-rename editors can't save at all without it — and that rename is exactly when the kernel records the writer. `.sb` is `0770` with member `rwx`; the store and its `-wal`/`-shm` sidecars are `0660` with member `rw`, since every member must be able to write the store and SQLite creates sidecars beside it.

Mode bits can't express "these accounts may write this file," so sandbox writes a POSIX ACL directly as the `system.posix_acl_access` xattr — owner `rw(x)`, each named account `rw(x)`, group and other read-only, plus a mask. The on-disk format is small and stable, so this needs no `acl` package and no `setfacl`. On a filesystem without ACL support the `setxattr` fails and the mode bits still apply, leaving owner-writable files.

`sb` applies what it can when taking or releasing a lock, on `useradd` and `userdel`, and after a save releases locks. `chown` is privileged, so the watcher does that half, reading the creator, roster, locks, and committed file list straight from the head trees. With the watcher running, access converges within seconds of any change; with it stopped, ownership drifts.

---

## 8. Shared editing and locks

A team can work in one folder with one database, no clone/push/pull. Not a mode, can't be turned off, and invisible in a single-account repository.

- **Editing a file locks it to you**, claimed the next time any command scans the tree, attributed to the account the watcher recorded as the writer.
- **A held file is read-only to everyone else and owned by you.** Taking a lock drops group and other write bits, hands ownership to the holder, and narrows the ACL — so another account's editor refuses to save over it rather than discovering the problem later. Release restores the exact previous permission bits.
- **If a foreign write lands anyway** — shared login, writable parent, root — the next command puts your version back, stores the rejected bytes, and names them in the journal for `sb salvage`. A second writer's copy never becomes the version of record, and nothing typed is destroyed.
- **Only you can save it.** Everyone else's `sb save` skips your locked files. `--global-force` is the exception, and the journal says so.
- **Your own later edits move the lock forward**, recording the new content and restarting the clock.
- **A lock ends** on `sb save`, when the file matches its saved state, or after an hour idle (`SB_LOCK_TTL`).
- **Expiry loses nothing.** Abandoned edits are auto-saved in the *owner's* name, then auto-reverted in history and on disk, so the tree returns to what the owner found. The auto-save hash is printed and journaled; `sb restore <hash>` brings it back.
- **Merges never clobber locked work** (Section 5). Switches, restores, undos, and checkouts leave locked files alone, and the unsaved-changes check doesn't count them as your work.

Because expiry auto-saves are preservation commits, they store bytes verbatim rather than redacting: redacting and then reverting the disk would destroy the only copy of a live credential. Recognizable secrets in one are reported loudly and flagged `secrets-present`.

### Who gets the lock

1. **The recorded writer**, matched by inode and time window (Section 6). Right even for an in-place edit that left ownership untouched — if Alice edits and Bob runs `sb status`, the lock is Alice's.
2. **The file's owner**, when nothing matched but the watcher was demonstrably running.
3. **Nothing.** Changed while no watcher ran → left unlocked and unattributed, and said out loud. No guessing.

Deletions have nothing to stat and produce no close-write event, so they fall back to the account running the command.

### Lifecycle inside a command

1. **Enforce** — compare each locked file against the content it protects. Holder changed it: protect the new content, reset the clock. Someone else did: store their bytes, journal, revert. Content now equal to the last save: release.
2. **Expire** — auto-save and auto-revert anything past the TTL, grouped by owner.
3. **Acquire** — lock changed-but-unlocked files to whoever wrote them.
4. **Re-apply** — re-assert lock ownership and permissions.

### Scope

One machine with several accounts, or a directly attached shared disk. SQLite's WAL mode is documented as unreliable over NFS and SMB, and attribution fails from the other end: uid-squashing mounts destroy the ownership signal and fanotify marks a local mount. Move work between machines with `.sbox` archives instead.

---

## 9. Test gates

Executable scripts in these folders are ordinary tracked files, so they version, branch, and merge with the code:

```
sb-tests/
  pre-save/       before every save
  pre-merge/      before every merge, fast-forwards included
  pre-publish/    before every release
```

They run sorted by name (`10-lint.sh`, `20-unit.py`) inside a pristine temporary checkout of the exact candidate tree:

- **pre-save** sees the tree your save will produce — redactions applied, other accounts' locked files excluded, nothing ignored or untracked leaking in.
- **pre-merge** sees the merged result, and discovers its scripts from it, so a merge that changes the tests runs the new ones.
- **pre-publish** sees exactly what's shipping; the release record carries the content hashes of the scripts that ran.

The checkout root is the working directory, with:

| variable | value |
|---|---|
| `SB_STAGE` | `pre-save`, `pre-merge`, `pre-publish` |
| `SB_BRANCH` | current branch |
| `SB_COMMIT` | candidate save hash, or `(worktree)` |
| `SB_REPO` | absolute path to the real repository root |

Exit 0 passes. Anything else, or exceeding `SB_TEST_TIMEOUT` (120s per script), blocks with exit `2` and prints the script's last 15 lines. `--no-verify` overrides and lands in the journal entry.

`.py` runs under sandbox's own Python, executables run directly, anything else runs under `sh`.

```bash
sb test new pre-save 10-syntax      # scaffolds sb-tests/pre-save/10-syntax.sh
```

```sh
#!/bin/sh
set -eu
python3 -m py_compile $(find . -name '*.py' -not -path './sb-tests/*')
```

Keep pre-save gates in the seconds — syntax, lint, fast unit tests — and put slow suites at pre-merge or pre-publish. A ten-minute pre-save gate teaches everyone to type `--no-verify`, and a gate everyone overrides is worse than no gate.

---

## 10. Secrets and redaction

Every added or modified file is scanned at save time for AWS keys (`AKIA…`/`ASIA…`), private key blocks, GitHub tokens (`ghp_…`), Slack tokens (`xoxb-…`), Google API keys (`AIza…`), Stripe live keys (`sk_live_…`), JWTs, and generic assignments like `password = "…"` with long quoted values.

A match is replaced with `<REDACTED>` in the committed blob. **The file on disk is never touched** — the code you're running still has its key, and the save still happens. What was redacted is printed and journaled per file. Private keys are redacted as a whole block, `-----BEGIN` through the matching `-----END`, because the material is the base64 body below the flagged line.

Two exceptions: `--allow-secrets` commits verbatim (journaled `secrets-override`), and a file that isn't clean UTF-8 can't be rewritten without mangling its bytes, so it blocks the save with exit `2`. Expiry auto-saves preserve bytes verbatim (Section 8).

A redacted file differs from its saved form permanently and by design, so sandbox treats that difference as expected:

- `sb status` lists it under *redacted in history*, not `modified`.
- It never counts as a dirty tree, so it can't block `switch`, `merge`, `restore`, `undo`, or `publish`, and holds no lock.
- Lock expiry skips it — reverting would delete the only live copy.
- Checkouts won't overwrite it when the target is what it redacts to.

When the target genuinely differs, that's the content you asked for, so sandbox writes it — after printing which files hold a credential history never stored.

Redaction is a seatbelt, not a policy. The durable fix is an environment variable or an ignored config file (`sb ignore .env`). Binaries and files over 64 MiB are skipped; text is scanned a window at a time; only files in the current save are scanned; pattern matching catches known credential shapes, not every secret.

---

## 11. Security model

### Integrity

Objects are keyed and re-hashed by SHA-256 on every read. Each save embeds its tree and parent hashes, transitively fixing every byte before it. Operations commit as one SQLite transaction (WAL, `synchronous=FULL`). Worktree writes use exclusive randomized temp files, fsync, and atomic rename through no-follow parent descriptors — checkout, switch, merge, restore, `undo -p`, lock reverts, `salvage`. A crash between database and folder surfaces as ordinary unsaved changes.

Covers: corruption, torn and partial writes, power loss, truncated or bit-flipped objects, objects that hash correctly but don't parse, trees attempting path traversal, symlinks attempting to redirect a write.

Doesn't cover: loss of the database file. Detection is not backup.

### Tamper evidence

The hash-chained journal, plus `sb verify` recomputing it and cross-checking every branch tip, catches edited or deleted entries, refs moved or injected via direct SQL, and replaced objects.

Limit: anyone with write access and knowledge of the format can rewrite the whole store into a consistent history. With no secret material, consistency is recomputable by anyone — inherent to keyless designs. Anchors (Section 12) close it, since a value recorded off-machine can't be reproduced by any rewrite. What sandbox won't do is ship the appearance of cryptographic authenticity without key management that would make it mean something.

### Leak prevention

Save-time redaction, on by default, overridable only explicitly, with both the redaction and the override journaled. Unredactable files block instead.

Limit: unrecognizable secrets, secrets already in older saves, and secrets in binary or oversized files, which are skipped.

### Attribution is not authentication

It's a kernel-recorded fact about which local account performed a write, and the loginuid survives `sudo` and `su`. It doesn't prove who was at the keyboard, and root can both write files and alter the event store. It buys accuracy in a cooperative team, not resistance to a hostile administrator.

The event store is world-readable by design (unprivileged `sb` consults it) and root-writable only. It records paths, inodes, uids, pids, and process names for writes on watched mounts, including outside repositories for five minutes. On a shared machine that's a deliberate, mild observability trade.

### Locks are not access control

Permission bits and ACLs stop the ordinary case and keep a second writer's copy from becoming the version of record. They don't stop root, a shared login, or anything bypassing sandbox. `sb unlock --force` is available to every member, and journaled.

### Not claimed

Confidentiality of the store — it's created `0600` and relaxed by the watcher to `0660` (`.sb` at `0770`, plus ACL entries for members) so every member can write it; full-disk encryption is the right layer. Authentication. Access control beyond what the local filesystem enforces.

---

## 12. Anchors

The one thing a keyless store can't detect internally is a wholesale rewrite. An anchor closes it with a hash kept somewhere else.

`sb verify` and `sb info` print the current anchor — 16 hex characters of the latest chain link — and every `sb journal` line and `sb publish` prints the link for its own entry.

```
  └─── anchor          67b3dea8b260c12a  (save it · check later: sb verify -a <hash>)
```

Copy it off the machine. Later:

```bash
sb verify -a 67b3dea8b260c12a
```

64 bits: short enough to write down, far too large to forge a collision against. Any 8–64 hex prefix works, and a pasted ellipsis is forgiven. Found in the chain means everything up to that moment is as you witnessed it. Not found means the journal on disk isn't the journal you saw.

Anchors double as bookmarks: `sb restore <anchor>` returns the branch to the state that anchor witnessed. If the branch had no saves then, sandbox says so and lists the branches that did.

---

## 13. Portable archives (.sbox)

```bash
sb pack -k "pass-key"                       # -> <foldername>.sbox
sb pack release.sbox -k "pass-key"
sb pack release.sbox                        # prompts instead
sb unpack my-project.sbox -k "pass-key"     # -> ./my-project/
```

For `pack` and `unpack` the key comes from `-k`, else `SB_PASSKEY`, else a prompt (confirmed when sealing). `-k` exposes it to shell history and process listings; the other two don't.

A full-repository archive is staged and verified before install, so a damaged or tampered one is refused before anything reaches the destination. All history survives, and the unpack is journaled in the installed repository. `-f` seals or writes only the current save's files.

An unpacked repository keeps the creator and roster uids from its store, which may mean different people on a different machine — check `sb users` after restoring.

### Manifest

| field | meaning |
|---|---|
| `created` / `created_by` | when it was sealed, and by which account |
| `repo_name` | original folder name, the default unpack destination |
| `branch` | branch at pack time |
| `chain_head` | journal head at pack time; usable as an anchor |
| `label` / `commit` | for `sb export -k`: which version the files came from |
| `sb_version` / `sbox_version` / `repo_id` | versions and the repository's stable ID |
| `db_sha256` / `db_size` | integrity check for the payload, verified before any write |

The manifest sits inside the encrypted blob, so an archive reveals nothing — author, branch, filenames — without the key. The only cleartext is a small header (`SBOX`, a format byte, a random 16-byte salt), bound to the ciphertext as authenticated data so it can't be swapped undetected.

### Encryption

Nothing in save, merge, verify, the journal, or anchors depends on a secret. Archive confidentiality is separate, handled by [vox](https://jts.gg/vox) (v1.7.3, symmetric core), a single-file module embedded in sandbox and loaded only while `pack`, `unpack`, or `export -k` runs. No separate file, no install, no network.

vox is a misuse-resistant AEAD: SIV-style over HMAC-SHA512, with PBKDF2-HMAC-SHA512 at 300,000 iterations. A random per-archive salt goes into key derivation, so the same passphrase yields different keys and a guess can't be amortized across archives. One format is read and written (v2); anything else is refused by name.

Consequences: a wrong key or a single altered byte means the archive won't open — authenticity is verified before decryption, the payload is re-hashed against `db_sha256`, and full-repo archives then go through the staged verification. And the key is the only thing between the archive and its contents; there's no recovery and no key file.

Sealing and opening are streamed — the body is hashed and encrypted a chunk at a time, and the tag is verified before any plaintext is written — so a multi-gigabyte repository never has to fit in memory.

---

## 14. Storage format

One SQLite database, `.sb/sandbox.db`, WAL mode, created `0600` and relaxed by the watcher to `0660`, `synchronous=FULL` by default.

- Every operation is one ACID transaction.
- One file: `cp` is a valid backup, `rsync` sees one change.
- Prefix resolution is an indexed `LIKE`; the stat cache and locks are ordinary tables.
- Inspectable with standard tooling — and anything changed that way is flagged by `verify`.

| table | contents |
|---|---|
| `meta` | `format`, random `repo_id` (chain root), current `branch`, `creator_uid`, `roster`, `durability`, open-merge state |
| `objects` | `hash → kind, size, zlib(data)` |
| `refs` | `name → commit hash` (empty string = branch with no saves) |
| `journal` | `seq, ts, op, detail(JSON), prev, link` |
| `statcache` | `path → size, mtime, ctime, inode, hash` |
| `locks` | `path → owner, since, base, held, mode, uid, perm` |

In `locks`: `uid` is the account the lock belongs to and the only field consulted when deciding whether it's yours; `held` is the hash of the protected content, or a marker if the holder deleted the file; `perm` is the permission bits to restore on release.

The watcher's `events`, `coverage`, and `repos` tables live in its own store (Section 6). No repository database contains watcher data.

**Object hashes** are `SHA-256("<kind> <length>\0" + data)`. Trees and commits are canonical JSON — sorted keys, no whitespace. Modes: `100644`, `100755`, `040000`, `120000`.

**Stat cache.** Change detection compares size, mtime, ctime, and inode; a full match reuses the previous hash without reading the file. mtime alone can be restored from userspace (`touch -d`, archive extraction), but ctime is kernel-maintained and the inode changes when an editor replaces a file, so a same-size edit with a restored mtime still misses. Files whose mtime *or* ctime is under two seconds old always bypass; during a save a cached hash is trusted only if the blob exists; `--deep` bypasses entirely. A miss costs a re-read — the cache fails toward correctness.

**Write paths.** Tree entry names are validated on read (no `/`, `\`, NUL, `.`, `..`, empty, or `.sb`), so a hostile tree can't write outside the repository. Every worktree write and archive output goes through a no-follow parent descriptor, an exclusively-created randomized temp file, a complete-write loop, fsync, and atomic rename. Directory pruning never touches `.sb`.

---

## 15. Ignoring files

`.sbignore` in the root, one glob per line, `#` for comments. A pattern matches the full relative path, that path as a directory prefix, or any single component.

```
*.log
build
.env
node_modules
data/*.tmp
```

`sb ignore <pattern>` appends and journals. Always ignored: `.sb`, `*.sbox`, `.git`, `.svn`, `node_modules`, `__pycache__`, `*.pyc`, `*.egg-info`, `.venv`, `venv`, `.DS_Store`, and editor scratch files (`*.swp`, `*.swo`, `*~`, `.#*`, `#*#`, `4913`, `.goutputstream-*`, `*.tmp`, `.~lock.*`). That last group matters: they appear and vanish mid-save, and tracking them would lock files that were never work.

`.sbignore` is itself tracked, so rules travel with branches.

Ignored files are invisible to `save` and `status` but never deleted. A rule only decides what gets *picked up* — a file already in the last save stays tracked even if a later rule matches it, so adding `*.log` won't silently drop a `keep.log` you committed. To stop tracking something, delete it.

---

## 16. Workflows

**Solo.** `sudo sb service -i` once, then `sb init` and work / `sb status` / `sb save`. Add a pre-save syntax gate on day one.

**Experiment.** `sb branch spike && sb switch spike` — it already holds your folder as its first save. Works out: `sb switch main && sb merge spike`. Doesn't: switch back and never merge.

**Broke it ten minutes ago.** `sb diff`, then `sb undo -p <file>` for one file or `sb undo` for the last save.

**Deleted everything.** `sb save "oops"`, then `sb undo`. Both live in history.

**Worked last Tuesday.** Find it — a noted anchor, a save in `sb log`, a release label — and `sb restore <it>`.

**Adding someone.** `sb useradd alice`. She can write everything unlocked, and each file becomes hers the moment she edits it until she saves. `sb userdel alice` when she leaves; `sb unlock <path> --force` for anything she left held.

**Small team, one machine.** Everyone in the same folder. Locks land on the actual editor; your locked files stay put and others' edits to them are reverted and recoverable with `sb salvage`; `sb save` commits only your files; abandoned locks auto-save in their owner's name after an hour. `sb merge feat -i` proceeds around locked work and completes on re-merge.

**My edit got reverted.** It belonged to someone else's lock. `sb journal` has the `lock-revert` entry with the hash; `sb salvage <hash> mine.txt`.

**sb can't tell who changed this.** The watcher was down. `sb service -s`, then `sudo sb service -i`. The change is still there and still saveable.

**Release.** Real suite in `sb-tests/pre-publish/`, then `sb publish v1.4`. `sb publish -l` is the history.

**Deploy.**

```bash
sb publish v1.4
sb export v1.4 -k "release-key"
scp myapp-v1.4.sbox server:

sb unpack myapp-v1.4.sbox /srv/www/myapp -k "release-key"        # first drop
sb unpack myapp-v1.5.sbox /srv/www/myapp -k "release-key" -i     # every one after
```

Rolling back is `sb export <older-label>` and the same `-i` drop.

**Trust check.** `sb verify` weekly; keep the anchor with the date somewhere off-machine.

---

## 17. sandbox versus git

| | **sandbox** | **git** |
|---|---|---|
| Model | work → save | work → stage → commit |
| Staging | none | the index |
| Detached HEAD | impossible | routine confusion |
| Destroying history | no command does it | `reset --hard`, `push -f`, expired reflog |
| Undo | `sb undo`, a new save | `revert` vs `reset` vs `restore` vs `checkout` |
| New branch | born with a save | a pointer; content later |
| Identity | your Linux account | `user.name`, set to anything |
| Who edited a file | kernel-recorded, or explicitly unknown | not tracked |
| Format | one crash-safe SQLite file | loose files + packfiles + refs + index |
| Audit log | hash-chained journal, cross-checked vs refs | reflog: local, expiring, mutable |
| Tamper evidence | chain + tip cross-check + anchors | commit DAG only |
| Secrets | redacted at save time by default | third-party hooks |
| Tests | versioned gates on clean checkouts, on by default | hooks: unversioned, per-clone |
| Renames | similarity detection, rename-aware merges | similarity detection, rename-aware merges |
| Sharing | one repo, per-file locks backed by ownership and ACLs | clone/push/pull |
| Distributed collaboration | no | git's core strength |
| Platform | Linux | everywhere |

git is a distributed collaboration system you can also use alone; sandbox is a safety and integrity system for individuals and small teams on a shared machine. They coexist — sandbox ignores `.git`, so add `.sb` to `.gitignore`.

---

## 18. Environment variables

| variable | effect | default |
|---|---|---|
| `SB_PASSKEY` | pass-key for `pack` and `unpack` when `-k` is absent, and for `export -k ''` | prompt |
| `SB_TEST_TIMEOUT` | seconds per test script | `120` |
| `SB_LOCK_TTL` | seconds a lock survives idle before auto-save and release | `3600` |

`SUDO_UID` and `SUDO_USER` are read when running under `sudo` so an elevated command is attributed to the invoking account. They're advisory; the watcher records the kernel's `loginuid`, which can't be forged this way.

Test scripts additionally get `SB_STAGE`, `SB_BRANCH`, `SB_COMMIT`, `SB_REPO` (Section 9).

---

## 19. Exit codes

| code | meaning |
|---|---|
| `0` | success |
| `1` | usage or state error — not a repo, watcher missing at `init`, unsaved changes, unknown branch, bad arguments, non-empty unpack destination, corrupt object, filesystem error |
| `2` | a gate stopped you — unredactable secrets, failed tests, merge conflicts, `verify` findings |
| `130` | interrupted |

`2` means sandbox worked correctly and blocked something on purpose, so automation can tell "fix your command" from "fix your content."

---

## 20. FAQ

**Why does `sb init` need a service installed?**
Without it, sandbox can't tell who wrote a file and would have to guess from ownership — wrong for every in-place edit. One command, once per machine.

**What does the watcher see?**
Completed writes on mounts holding registered repositories: path, inode, uid, loginuid, pid, process name. Never file contents. Writes outside repositories are dropped after five minutes; writes inside `.sb` are ignored.

**macOS?**
Not for creating repositories: `sb init` needs the watcher, the watcher needs fanotify, and the access model needs POSIX ACLs. WSL2 works where its kernel has both.

**What breaks if I stop the watcher?**
Nothing about saving, branching, or merging. New edits stop being attributable — changes outside a coverage window are left unlocked and reported rather than guessed at — and access rules stop being re-applied, so ownership drifts.

**Why wasn't my secret blocked?**
It was redacted instead: `<REDACTED>` in the committed blob, your file untouched, the files listed in the journal. Only a file that can't be rewritten faithfully blocks.

**Someone has a file locked and my edit keeps reverting. Where did it go?**
Into the store. The `lock-revert` journal entry has the hash; `sb salvage <hash> mine.txt` writes it back out.

**My editor says permission denied.**
That's the lock at the filesystem level — a locked file is owned by its holder with a narrowed ACL. `sb locks` shows who and until when.

**Can I turn locking off?**
No. In a single-account repository it does nothing visible: you're always your own holder.

**Why does `sb branch` create a save?**
So a branch is never an empty name — it can be switched to, tested, exported, and merged immediately, with no "save something first" step before a merge.

**I merged a branch without my file and the file survived.**
Intended. A file absent from the base was only ever added on one side, so it's kept. Removal requires the base to have had it and one side to have deleted it.

**Does rename detection catch a file I moved and edited?**
Yes, if the two versions still share half their content — exact hash first, then piece-hash similarity. The pair reports as one rename, so `sb diff` shows a header line rather than the edit.

**Is SHA-256 cryptography? I asked for none.**
It's a hash from the standard library used as a content fingerprint. No keys, no signatures, no third-party crypto.

**How do I back up?**
Copy `.sb/sandbox.db`, copy the folder, or `sb pack` for an encrypted single file. Run `sb verify` after restoring.

**Partial commits, like `git add -p`?**
No. A save is exactly your working tree, which is what makes "the tests passed on this save" true. Unrelated changes belong in two saves or two branches. The your-files-only rule is the one exception, and exists to protect other people's work.

**Large or binary files?**
Versioned like anything else, zlib-compressed; `diff` summarizes them in a line and redaction skips them. Files ≥8 MiB are chunked, so a small edit stores only changed chunks and nothing is held in memory whole.

**Symlinks?**
Stored as content — the target path — and recreated as real links by checkout, switch, merge, export, and archives. Never followed on write: every path opens parents no-follow, so a link can't redirect a write out of the repository. Two changed targets conflict, since merging a path line-by-line could produce one that points nowhere.

**Two commands at once?**
Fine. SQLite serializes writers, and racing saves are caught by a compare-and-swap on the tip plus a worktree drift check. One succeeds, one says to run it again.

**Does anything leave my machine?**
No network code in sb.

**Rename a branch?**
Not built in. `sb branch new-name && sb branch old-name -r` from another branch; removal never deletes saves.

**Can `unpack -i` be undone?**
No — it overwrites in place with no per-file backup. That's why it's a flag.

**If Bob's `sb status` discovers Alice's edit, who gets the lock?**
Alice. The watcher recorded her account performing the write. Bob running a command isn't evidence of anything.

---

## 21. Troubleshooting

**`the write-attribution watcher is not installed`** — `sudo sb service -i`, then retry. If it's installed but stopped, the same command restarts it.

**`not inside a sandbox repository`** — `cd` into one, or `sb init`.

**`the write-attribution watcher is not running`** (warning) — everything works, but new edits can't be attributed. `sudo sb service -i`.

**`<file> changed while nothing was watching`** — the watcher was down for that edit, so sandbox won't name an author. The file is unlocked and unattributed; the change is still saveable.

**`unrecognized arguments:` / `missing:`** — the usage line for that command is printed below, and a flag belonging elsewhere is named.

**`no such system account: <name>`** — `sb useradd` only takes accounts that already exist. Create the user first.

**`the creator cannot be removed`** — the creator owns every unlocked file. To hand a project over, `sb pack` it and have the new owner unpack under their own account.

**`you have unsaved changes`** — `sb save "wip"` (saves are cheap, undo is free) or `sb undo -p <path>`. Other people's locked files and redaction-only differences never trigger this.

**`reverted N file(s) to their lock holder's version`** — you edited files someone else holds. Yours are stored; the message and journal give the hashes for `sb salvage`.

**`nothing of yours to save`** — everything you changed is locked by someone else. Wait, or ask them to save.

**`merge blocked — it would change files locked by others`** — wait, or `sb merge <branch> -i` to merge around them as a partial merge.

**`save blocked — secrets in files that cannot be safely redacted (not clean UTF-8)`** — remove the secret, `sb ignore` the file, or `--allow-secrets`.

**`secrets redacted in the save (working files untouched)`** — not an error. History holds `<REDACTED>`; your files are unchanged.

**`<folder> is not empty — unpack into a fresh folder`** — pick a fresh one, or `-i` to overwrite matching files deliberately.

**`the archive's repository failed verification — nothing was written`** — the archive is damaged or tampered with. Get a good copy.

**`pre-save tests failed — save blocked`** — the failing script's last 15 lines are above. Reproduce with `sb test pre-save`.

**`merge of <branch>: N file(s) need you`** — resolve the markers and `sb save "<message>"`, or `sb merge --abort`. Binary and symlink conflicts keep your version instead of being marked.

**`conflict markers are still in <file>`** — remove the `<<<<<<<` / `=======` / `>>>>>>>` lines and the side you don't want, then save.

**`a merge of '<branch>' is still open`** — finish with `sb save` or drop with `sb merge --abort`.

**`object … does not match its hash`** — real corruption or tampering; sandbox stopped rather than propagating it. Restore the database from a backup, then `sb verify`. Intact files can be rescued first with `sb undo -p` or `sb export`.

**`branch '…' changed under this operation`** — two commands raced and yours lost the compare-and-swap. Nothing changed; run it again.

**`store error: … database is locked`** — another command holds the write lock. Retry.

**`file system error:`** — permission or disk problem outside sandbox. On a permission error, check `sb users`, `sb locks`, and that the watcher is running.

**A file isn't being saved** — it matches an ignore rule and wasn't already tracked, someone else holds it (`(theirs)` in `sb status`), or it differs from history only by redaction.

---

*sb — one file, no dependencies, nothing silently destroyed.*
