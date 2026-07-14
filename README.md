# sandbox (sb)

**Version 1.0** · [jts.gg/sandbox](https://jts.gg/sandbox)

**Safe, honest version control for humans.** One file. One command vocabulary you can learn in five minutes. Zero dependencies beyond Python 3.9+. Zero cryptography libraries. Nothing is ever silently destroyed.

sb is not a git clone and does not use git's repository format. It keeps the two ideas git got right — content addressing and a Merkle DAG of snapshots — and replaces everything that makes git hostile to daily use: the staging area, detached HEADs, destructive commands, a repository made of thousands of fragile loose files, and error messages written for git's own developers.

```
$ sb init
$ sb save "first version of the landing page"
saved 8163c18fe1 on main · 12 file(s)
  └─── "first version of the landing page"
```

That is the entire mental model: **you work, you save.** Everything else — branches, merges, quality gates, deployment records, tamper detection — builds on those two verbs.

---

## Table of contents

1. [Why sb exists](#1-why-sb-exists)
2. [Installation](#2-installation)
3. [Five-minute quickstart](#3-five-minute-quickstart)
4. [Core concepts](#4-core-concepts)
5. [Command reference](#5-command-reference)
6. [Test gates: quality enforcement](#6-test-gates-quality-enforcement)
7. [The secret scanner](#7-the-secret-scanner)
8. [Security model and threat model](#8-security-model-and-threat-model)
9. [Anchors: pinning history outside the machine](#9-anchors-pinning-history-outside-the-machine)
10. [Portable archives (.sbox)](#10-portable-archives-sbox)
11. [The storage format](#11-the-storage-format)
12. [Ignoring files](#12-ignoring-files)
13. [Everyday workflows](#13-everyday-workflows)
14. [sb versus git](#14-sb-versus-git)
15. [Environment variables](#15-environment-variables)
16. [Exit codes](#16-exit-codes)
17. [FAQ](#17-faq)
18. [Troubleshooting](#18-troubleshooting)
19. [Known limitations and roadmap](#19-known-limitations-and-roadmap)

---

## 1. Why sb exists

Version control solves a real problem: *"I want to change things without fear, and know exactly what happened later."* Git solves that problem too — wrapped in an interface where `checkout` means four different things, where a mistyped `reset --hard` erases an afternoon of work, and where the repository itself is a directory of thousands of loose files that a power cut can leave half-written.

sb starts from three convictions.

**Safety should be structural, not disciplinary.** In sb there is no command that discards saved history. `undo` creates *new* history that reverts the old. `switch` refuses to run over unsaved work. The store is one SQLite database, so every operation is a single atomic transaction — a crash mid-save leaves you exactly where you were, never in a torn in-between state.

**A tool can be simple without being a toy.** sb has no staging area (a save snapshots everything that isn't ignored), no detached HEAD (you are always on a branch), and no rebase (history is append-only). Yet it has real branches, real three-way merges with automatic conflict-free merging, versioned test gates that block bad saves, deployment records, and a full-store integrity verifier.

**Security features should be honest.** sb makes exactly three security promises — integrity, tamper evidence, and leak prevention — and Section 8 states precisely how each is achieved, what it defends against, and what it deliberately does not. There are no keys to lose, no signatures to misunderstand, and no cryptography library in the dependency tree. Everything rests on one primitive: SHA-256 from Python's standard library, used for content addressing and hash chaining.

---

## 2. Installation

sb is a single file with no dependencies. Requirements: Python 3.9 or newer (its bundled `sqlite3` and `hashlib` modules are all sb uses).

```bash
# put it somewhere on your PATH
mkdir -p ~/.local/bin
cp sb.py ~/.local/bin/sb
chmod +x ~/.local/bin/sb

# confirm
sb help
```

If `~/.local/bin` is not on your PATH, add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile. On Windows, run it as `python sb.py <command>` or create a small `sb.bat` wrapper.

To upgrade, replace the file. sb refuses to open repositories created by a *newer* format than it understands, so upgrades are always safe and downgrades fail loudly rather than corrupting anything.

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
sb restore src/app.py            # bring one file back from the last save

# trust, but verify
sb verify                        # re-hash every object, check the journal chain
sb journal                       # the tamper-evident log of everything sb ever did

# move it somewhere safe
sb pack "a-strong-pass-key"      # seal the whole repo into an encrypted .sbox
sb unpack my-project.sbox "a-strong-pass-key"   # restore it on another machine
```

That's 90% of daily use. The remaining 10% — test gates, deployments, anchors — is below.

---

## 4. Core concepts

### Saves

A **save** is a complete snapshot of every tracked file, with a message, an author, and a timestamp. There is no staging area and no partial commit: what you see in your working folder (minus ignored files) is what gets saved. This is deliberate — the staging area is the single largest source of git confusion, and "the snapshot is exactly what I'm looking at" is a property you can reason about.

Every save records the hash of its parent save, so saves form a chain (and, with merges, a directed acyclic graph). Changing any byte of any past save would change its hash and break every link after it — history is tamper-evident by construction.

### Branches

A **branch** is a named pointer to a save. Creating one is instant and costs nothing (`sb branch idea`). You are *always* on exactly one branch; sb has no detached-HEAD state, which eliminates the entire "you are in 'detached HEAD' state" class of confusion. Switching branches rewrites your working folder to match that branch's latest save — and refuses to run if you have unsaved changes.

### The object store

Every piece of content lives in a **content-addressed object store**: the key for each object is the SHA-256 hash of its content. Identical files are stored once no matter how many saves contain them. Three kinds of object exist:

| kind | contents |
|---|---|
| `blob` | raw file bytes |
| `tree` | a directory listing: `[[mode, kind, hash, name], ...]` as canonical JSON |
| `commit` | `{tree, parents, author, email, time, message}` as canonical JSON |

Every object is **re-hashed on every read**. Silent corruption cannot flow into your working folder: a damaged blob raises an error the moment anything touches it, and `sb verify` finds it proactively.

### The journal

The **journal** is sb's signature idea: an append-only log inside the store where every operation that changes anything — every save, merge, undo, branch creation, switch, and deploy — is recorded. Each entry embeds the SHA-256 link of the previous entry, forming a **hash chain** rooted in a random repository ID chosen at `init`.

This makes the repository's *operational history* tamper-evident, not just its content:

- Delete or edit a journal entry → the chain breaks at that point.
- Move a branch tip behind sb's back (even with direct SQL against the database) → `sb verify` catches it, because branch tips are cross-checked against what the journal last recorded.
- Corrupt an object → the content re-hash catches it.

`sb journal` shows the log; `sb verify` proves it. See Section 8 for exact guarantees and Section 9 for anchoring the chain outside the machine entirely.

### Test gates

Executable scripts in `sb-tests/pre-save/`, `sb-tests/pre-merge/`, and `sb-tests/pre-deploy/` run automatically before the corresponding operation and **block it on failure**. They always run in a pristine temporary checkout of the exact candidate tree — never your dirty working folder — so "passes the gate" means "the actual thing being saved passes," with no untracked-file contamination. Section 6 covers them fully.

---

## 5. Command reference

Conventions: `<angle brackets>` are required, `[square brackets]` optional. All commands work from anywhere inside the repository. Colors appear only when output is a terminal, so piping to files or scripts is always clean.

### `sb init`

Creates a repository in the current folder: the `.sb/` directory containing `sandbox.db`, with a single branch `main` and a journal seeded with a random repository ID. The database file is created with `0600` permissions (private to your user) by default. Fails if a repository already exists here. If it finds an old loose-file-format `.sb/` (from a pre-1.0 sb), it explains rather than guessing.

### `sb status`

Shows the current branch, the latest save, and every file that is `new`, `modified`, or `deleted` relative to that save. Fast even on large trees thanks to the stat cache (Section 11): unchanged files are detected by size + modification time without being re-read.

### `sb save "message" [--allow-secrets] [--no-verify]`

Snapshots every tracked file as a new save on the current branch. The message is **required** — history without messages is archaeology. In order, `save`:

1. Diffs the working tree against the last save; if nothing changed, no save is created.
2. Runs the **secret scanner** over every file being added or modified (Section 7). Findings block the save; `--allow-secrets` overrides deliberately.
3. Runs **pre-save test gates** in a clean checkout (Section 6). Failures block the save; `--no-verify` overrides.
4. Stores the blobs, builds the tree, writes the commit, moves the branch tip, and journals the whole thing — as one atomic transaction.

### `sb log [-n N]`

Save history for the current branch, newest first: hash, date, author, message, and a `(merge)` marker for merge saves. `-n 5` limits to five entries.

### `sb diff [path]`

Unified, colorized diff between the working folder and the last save. With `path`, limits output to that file or everything under that folder. Binary files show as changed in `status` but are not rendered as text diffs.

### `sb undo`

Reverts the effect of the latest save **by creating a new save** whose content equals the previous one. History is never rewritten and nothing is deleted — the "undone" save remains fully in the log and journal. Running `sb undo` again redoes (it reverts the revert). Requires a clean working tree, so it can never eat uncommitted work.

### `sb restore <path>`

Copies a file — or, if `path` is a folder, everything under it — out of the last save into your working folder, overwriting the working copy, with its saved permissions. This is the "I mangled this file, give me back the good one" command.

### `sb branch [name]`

With no argument, lists branches, marking the current one and showing each tip. With a name, creates a branch pointing at the current save. Branch names must be single path components (no `/`, no leading `-`).

### `sb switch <branch>`

Moves to a branch: rewrites the working folder to match its latest save and updates the current-branch pointer. **Refuses to run with unsaved changes** — save first, or `restore` the files you want to drop. File writes during switch are atomic (write-to-temp, then rename), and directories emptied by the switch are pruned.

### `sb merge <branch> [--no-verify]`

Three-way merge of `<branch>` into the current branch, using their common ancestor as the base:

- If the current branch is an ancestor of the target, this is a **fast-forward**: the tip simply moves (still gated by pre-merge tests).
- Files changed on only one side take that side automatically.
- Files changed on **both** sides get a **line-level three-way merge**: non-overlapping edits combine automatically (`(N file(s) auto-merged)` in the output). Overlapping edits are conflicts.
- On conflict, the merge **stops before touching anything**: your working folder is left exactly as it was, and sb lists each conflicting file with the reason (`2 overlapping change(s)`, `binary file changed on both branches`, `changed vs deleted`). Reconcile the file on one branch — copy the other side's version over, or combine them by hand — save, and merge again. sb never leaves you in a half-merged state with special in-progress rules to remember.

The merge algorithm is deliberately conservative: edits that touch adjacent lines, or insertions at the same point, are reported as conflicts rather than guessed. A false conflict costs you a minute; a false merge costs you a bug.

Before the merged result is committed, **pre-merge test gates** run against the *merged tree itself* — the thing that will actually exist afterward — so "both branches passed their tests" can never smuggle in a combination that doesn't.

### `sb test [stage]` / `sb test new <stage> <name>` / `sb test list`

Run gates manually against your current working tree (all stages, or one of `pre-save`, `pre-merge`, `pre-deploy`), scaffold a new test script from a template, or list discovered tests. Section 6 has the full guide.

### `sb deploy [label] [--list] [--no-verify]`

Marks the current save as deployed, behind two gates:

1. **Full store verification** — the entire `sb verify` battery. sb refuses to deploy from a damaged or tampered store.
2. **Pre-deploy tests** on a clean checkout of the exact tree being deployed.

Passing both writes a `deploy` entry into the hash-chained journal: what was deployed, from which branch, by whom, when. `sb deploy --list` shows all records and reports whether the chain that protects them still verifies. A deploy record is not a signature (Section 8), but falsifying one after the fact requires rewriting the journal chain — which `verify` and any noted anchor will expose.

### `sb verify [--anchor HEX]`

The "is everything intact?" button. It:

1. Walks every save reachable from every branch and **re-hashes every commit, tree, and blob**.
2. Validates every tree entry name (defense against crafted trees that try to escape the repository on checkout).
3. Recomputes the **entire journal hash chain** from the repository ID to the head.
4. Cross-checks every **branch tip against the journal's last record** of it — a ref moved outside sb is caught here.
5. With `--anchor HEX`, additionally confirms that a previously noted chain link is still part of history (Section 9).

Prints a category-by-category report and the current **chain head** — write that value down somewhere else and it becomes an anchor. Exits `0` when everything agrees, `2` with a precise list of problems otherwise.

```
checked 18 objects across 5 save(s)
  ├─── content hashes  all valid ✓
  ├─── journal chain   11 entries linked ✓
  ├─── branch tips     match the journal ✓
  └─── chain head      8977ecba8bd79985…  (write it down — it anchors today's history)
history is intact ✓ — store, journal and refs all agree
```

### `sb journal [-n N]`

The append-only operation log: every save, merge, undo, branch, switch, and deploy, each with its timestamp, detail, and chain link. Ends by re-verifying the chain and telling you so. This is the answer to "what actually happened in this repository?" — including things `log` doesn't show, like switches and deploys.

### `sb info`

One-screen repository overview: store location and size, current branch, object counts, journal length, chain head, and how your saves will be attributed.

### `sb who [name] [email]`

Shows — or, with arguments, sets — how saves are attributed, stored in `~/.config/sandbox/profile.json` (override location with `SB_HOME`; override values per-command with `SB_NAME` / `SB_EMAIL`). This is **attribution for humans reading history, not authentication** — sb is explicit about that distinction (Section 8).

### `sb ignore <pattern>`

Appends a pattern to `.sbignore` (Section 12).

### `sb pack <PASS-KEY> [out.sbox]`

Seals the entire repository into a single encrypted `.sbox` archive (Section 10). The output defaults to `<foldername>.sbox`; a `.sbox` suffix is added if you omit it, and an existing file is never overwritten. Warns if you have unsaved changes, since pack seals *saved* history — save first to include them. Requires network access to fetch the encryption module.

### `sb unpack <path.sbox> <PASS-KEY> [dest]`

Restores a `.sbox` archive into a fresh folder (default: the original repository name), recreating the store with private permissions and checking out its files (Section 10). Refuses to overwrite an existing repository. A wrong pass-key or an altered archive fails cleanly and writes nothing.

### `sb version`

`sb version` (also `-V` / `--version`) prints the version and author: `sb 1.0 · jts.gg/sandbox`.

---

## 6. Test gates: quality enforcement

Test gates are how sb turns "we should run the tests" into "the tests ran, or it didn't happen."

### How it works

Put executable scripts in these folders — they are ordinary tracked files, so they version, branch, and merge with your code:

```
sb-tests/
  pre-save/      runs before every save
  pre-merge/     runs before every merge (including fast-forwards)
  pre-deploy/    runs before every deployment
```

Scripts run **sorted by name** (use prefixes: `10-lint.sh`, `20-unit.py`, `30-build.sh`), each inside a **pristine temporary checkout of the exact candidate tree**. That last part is the point:

- A pre-save gate sees your working files, but in a clean directory — nothing ignored or untracked leaks in, so "works on my machine because of a stray local file" is caught.
- A pre-merge gate sees **the merged result** — and discovers its scripts *from the merged tree*, so a merge that changes the tests runs the new tests.
- A pre-deploy gate sees exactly the tree being deployed.

Each script gets these environment variables and runs with the checkout root as its working directory:

| variable | meaning |
|---|---|
| `SB_STAGE` | `pre-save`, `pre-merge`, or `pre-deploy` |
| `SB_BRANCH` | the current branch |
| `SB_COMMIT` | the candidate save hash, or `(worktree)` |
| `SB_REPO` | absolute path to the real repository root |

**Exit 0 passes. Anything else — or exceeding the timeout (default 120s per script, tune with `SB_TEST_TIMEOUT`) — blocks the operation** and prints the script's last 15 lines of output so you can see why. `--no-verify` on `save`, `merge`, or `deploy` overrides deliberately and visibly.

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

And a pre-deploy gate:

```sh
#!/bin/sh
set -eu
python3 -m pytest -q tests/
```

Run gates manually anytime: `sb test` (all stages), `sb test pre-merge` (one), `sb test list` (what exists).

### Philosophy

Keep pre-save gates **fast** (seconds: syntax, lint, quick unit tests) so saving stays frictionless. Put slow suites at pre-merge or pre-deploy. A gate that takes ten minutes at pre-save will train you to type `--no-verify`, and a gate everyone overrides is worse than no gate.

---

## 7. The secret scanner

The most common irreversible mistake in version control is committing a credential. History is permanent and (once shared) distributed; rotating a leaked key is an incident, not an edit. sb blocks this **at save time**, scanning every file being added or modified for:

- AWS access keys (`AKIA…` / `ASIA…`)
- Private key blocks (`-----BEGIN … PRIVATE KEY-----`, including RSA/EC/OpenSSH/DSA/PGP)
- GitHub tokens (`ghp_…`, `gho_…`, and friends)
- Slack tokens (`xoxb-…` etc.)
- Google API keys (`AIza…`)
- Stripe live keys (`sk_live_…`, `rk_live_…`)
- JWTs
- Generic assignments like `password = "…"` or `api_key: '…'` with long quoted values

Findings block the save with the file, line number, and pattern name. Your options, in order of preference:

1. **Remove the secret** — load it from the environment or an ignored config file instead.
2. **Ignore the file** — `sb ignore .env`, then save.
3. **Override** — `sb save "msg" --allow-secrets`, when you're certain it's a false positive (a documentation example, a test fixture).

Binary files and files over 1 MB are skipped (scanning them produces noise, not safety). Honest caveats: pattern matching catches well-known credential *shapes*, not every secret — a password with no recognizable structure passes — and only files touched by the current save are scanned. Treat the scanner as a seatbelt, not a substitute for keeping secrets out of tracked files in the first place.

---

## 8. Security model and threat model

sb makes exactly three promises. Each is stated with its mechanism, what it defends against, and what it does not. A security feature you can't state precisely is a decoration.

### Promise 1 — Integrity: *what you get back is what you put in*

**Mechanism.** Every object is stored under the SHA-256 hash of its content, and re-hashed on **every read**, not just during `verify`. Every save embeds its tree hash and parent hashes, so each save transitively fixes the exact bytes of every file in it and every save before it. All writes are atomic SQLite transactions (WAL mode); working-folder writes during switch/merge use write-to-temp-then-rename.

**Defends against:** disk corruption, torn writes, power loss mid-operation, truncated or bit-flipped objects, a crafted tree object attempting path traversal on checkout (entry names are validated to be single, safe path components).

**Does not defend against:** loss of the database file itself. Integrity detection is not a backup — see the FAQ on backups.

### Promise 2 — Tamper evidence: *changes made behind sb's back are detectable*

**Mechanism.** The hash-chained journal. Every operation appends an entry whose link is `SHA-256(canonical entry ‖ previous link)`, rooted in a random per-repository ID. `sb verify` recomputes the entire chain and cross-checks every branch tip against the journal's final record of it.

**Defends against:** editing or deleting journal entries (chain breaks at the exact entry); moving branch tips via direct database manipulation (tip/journal mismatch); replacing objects (content re-hash); accidental or casual malicious modification by anything that isn't sb.

**Does not defend against — stated plainly:** an attacker with **write access to the database and knowledge of sb's format** can rewrite the *entire* store — all objects, the whole journal chain, every ref — into a new, internally consistent history. With no secret material anywhere (no keys), internal consistency is recomputable by anyone. This is inherent to keyless designs, and sb closes the gap the honest way: with **anchors** (Section 9). A chain-head link recorded *outside* the machine cannot be reproduced by any rewrite, so wholesale replacement becomes detectable too. What sb refuses to do is ship the *appearance* of cryptographic authenticity (signatures, "verified" badges) without the key-management reality that makes it meaningful.

### Promise 3 — Leak prevention: *credentials are stopped before they enter permanent history*

**Mechanism.** The commit-time secret scanner (Section 7), on by default, overridable only explicitly.

**Defends against:** the accidental commit of recognizable credentials — the most common and most costly VCS security failure in practice.

**Does not defend against:** unrecognizable secrets, or secrets already present in older saves.

### What sb deliberately does not claim

- **Confidentiality.** The store is not encrypted. The database is created `0600` (private to your user), and full-disk or per-directory encryption is the right layer for confidentiality. Encrypting the store inside sb would mean key management — the exact complexity this design rejects.
- **Authentication.** `author` on a save is attribution for humans, like a name on a document. sb says so out loud (`sb info`: *"attribution only — sb uses no keys or signatures"*) rather than implying otherwise.
- **Access control.** sb is a local tool; the operating system's file permissions are the access control.

### Why removing cryptography made this design *stronger*

The previous design signed every commit with Ed25519, falling back to a hand-rolled pure-Python implementation when the `cryptography` package was missing. A senior security review kills that with three observations. First, hand-rolled signature code is the classic implementation-vulnerability magnet — non-constant-time, unaudited, and carrying all the risk of real crypto with none of the assurance. Second, the keys had no management story: generated silently, stored in a home-directory file, never rotated, never distributed, never bound to anything a verifier could trust — so a "valid signature" proved only that *some* key signed it, which is theater. Third, theater is worse than absence, because users calibrate their trust to the claim, not the mechanism. The current design offers fewer guarantees and delivers all of them.

---

## 9. Anchors: pinning history outside the machine

The one attack a keyless store cannot detect internally is wholesale rewrite (Promise 2's stated limit). Anchors close it with nothing but a hash and a second location.

Every `sb verify` (and `sb info`) prints the current **chain head** — the link of the latest journal entry:

```
  └─── chain head      67b3dea8b260c12a0b292f1bd30cb0f1…  (write it down — it anchors today's history)
```

Record that value anywhere outside the machine: a note on your phone, a message to a colleague, a printed line in a logbook, a weekly entry in a password manager. Later:

```bash
sb verify --anchor 67b3dea8b260c12a0b292f1bd30cb0f1...
```

If the anchor is a link in the current chain, everything up to that moment is exactly as it was when you noted it — every operation, every save those operations recorded. If it is **not** found, the journal you noted is not the journal on disk: history was replaced wholesale, and no internally consistent rewrite can hide it, because the attacker cannot alter what's written in your notebook.

This is the same trust move that transparency logs and blockchain checkpoints make — externalize one small value, protect an unbounded history — implemented with a single SHA-256 and no infrastructure. Anchor as often as your paranoia requires; each anchor protects everything before it.

---

## 10. Portable archives (.sbox)

A sandbox repository is a single file (`.sb/sandbox.db`), which already makes it easy to move. But moving a raw database means moving your entire history *in the clear*. `sb pack` solves the last mile: it seals the whole repository into one **encrypted, self-describing archive** — a `.sbox` file — that is safe to email, drop in cloud storage, hand off on a USB stick, or archive for cold storage.

```bash
sb pack "my-strong-pass-key"              # -> <foldername>.sbox
sb pack "my-strong-pass-key" release.sbox # choose the output name
```

```
packed my-project.sbox · 45,576 bytes
  ├─── branch   main · chain head bd40a7878f681649…
  ├─── sealed   Jordan <jt@noct.gg>  2026-07-14 08:35
  └─── encrypted with vox (jts.gg/vox) · unpack: sb unpack my-project.sbox <PASS-KEY>
```

To restore it — on any machine with sb and network access — give the file and the same pass-key:

```bash
sb unpack my-project.sbox "my-strong-pass-key"            # -> ./my-project/
sb unpack my-project.sbox "my-strong-pass-key" dest-dir   # choose the folder
```

```
unpacked my-project · 3 file(s)
  ├─── sealed by  Jordan <jt@noct.gg>  · 2026-07-14 08:35
  ├─── branch     main · chain head bd40a7878f681649…
  └─── verify it: cd my-project && sb verify
```

Unpacking recreates `.sb/sandbox.db` in a **fresh** folder (it refuses to overwrite an existing repository), restores it with private `0600` permissions, and checks out the current branch's files into your working folder. **All history survives** — every save, every branch, the full journal chain — so the very first thing worth doing is `sb verify`, which will confirm the store, chain, and refs all still agree.

### What's inside a .sbox

Every archive carries an encrypted **manifest** alongside the store, written from your `sb who` identity and read back on unpack:

| Field | Meaning |
|---|---|
| `created` / `created_by` | when the archive was sealed, and the name/email of who sealed it |
| `repo_name` | the original folder name (the default unpack destination) |
| `branch` | the branch that was current at pack time |
| `chain_head` | the journal chain head at pack time — cross-check it against the source repo, or use it as an [anchor](#9-anchors-pinning-history-outside-the-machine) |
| `sb_version` / `repo_id` | the sb version that wrote it and the repository's stable ID |
| `db_sha256` / `db_size` | integrity check for the sealed store, verified before anything is written on unpack |

Because the manifest lives *inside* the encrypted blob, an archive reveals nothing — not the author, not the branch, not the file names — to anyone without the pass-key. The only cleartext is a 5-byte header (`SBOX` + a format byte), which is also cryptographically bound to the ciphertext so it cannot be swapped without detection.

### The encryption: vox

sb carries no cryptography of its own — that is a deliberate, load-bearing property of its security model (Section 8). So `pack` and `unpack` don't bundle a cipher; instead, at the moment you run them, sb fetches [**vox**](https://jts.gg/vox) — a small, single-file symmetric-encryption module — directly from its public source over HTTPS using only Python's standard library, and loads it in memory for that one operation. Nothing is written to disk, and sb's own footprint stays crypto-free and dependency-free.

vox provides a misuse-resistant authenticated cipher (an SIV-style AEAD built on HMAC-SHA512, with PBKDF2-HMAC-SHA512 key stretching). Two consequences matter in practice:

- **Wrong pass-key or a single altered byte → the archive will not open.** vox verifies authenticity *before* it decrypts, so a corrupted or tampered `.sbox` fails loudly rather than yielding garbage. sb adds a second belt-and-suspenders check by re-hashing the recovered store against `db_sha256`.
- **Your pass-key is the only thing standing between the archive and its contents.** There is no recovery, no backdoor, and no key file. Choose a strong, high-entropy pass-key, and store it separately from the archive. A weak pass-key is a weak archive.

> **A note on trust.** `pack`/`unpack` require network access to fetch vox, and you are trusting the code at its published URL at run time. That is the right trade for keeping sb itself crypto-free, but if you need fully offline or fully pinned operation, that is a conscious choice to make in your own environment. Everything else in sb — save, merge, verify, the journal, anchors — works with no network at all.

---

## 11. The storage format

The entire repository is **one SQLite database**: `.sb/sandbox.db`, in WAL mode, created `0600`. This is a deliberate rejection of git's loose-object layout, for reasons that compound:

- **Crash safety.** Every sb operation — blobs, tree, commit, ref move, journal entry — is a single ACID transaction. Git's loose files and separately-written refs can tear under power loss; SQLite's journal cannot. (Precedent: Fossil, the VCS written by SQLite's own author, made the same bet fifteen years ago.)
- **No small-file sprawl.** A large git repo holds hundreds of thousands of tiny objects, punishing filesystems and backup tools. sb is one file: `cp` is a valid backup, `rsync` sees one changed file, and there is nothing to "pack."
- **Real queries.** Prefix resolution is an indexed `LIKE`, statistics are one `GROUP BY`, and the stat cache is a table — no ad-hoc index file with its own format and lock protocol.
- **Auditable by anything.** The format is inspectable with the world's most widely deployed database tooling, not a bespoke binary format.

### Schema

| table | contents |
|---|---|
| `meta` | key/value: `format` version, random `repo_id` (chain root), current `branch`, `created` |
| `objects` | `hash → kind, size, zlib(data)` — the content-addressed store |
| `refs` | `name → commit hash` — branch tips (empty string = branch with no saves) |
| `journal` | `seq, ts, op, detail(JSON), prev, link` — the append-only hash chain |
| `statcache` | `path → size, mtime_ns, hash` — change detection without re-reading |

### Object encodings

An object's hash is `SHA-256("<kind> <length>\0" + data)`. Trees and commits are **canonical JSON** (sorted keys, no whitespace) — deterministic to produce and trivially parseable, with none of the whitespace-sensitive text-format parsing that plagues git internals. A tree is `[[mode, kind, hash, name], …]` sorted by name; a commit is `{tree, parents, author, email, time, message}`. Modes are `100644` (file), `100755` (executable), `040000` (directory).

### The stat cache

`status` and `save` detect changes by comparing each file's size and nanosecond mtime against the cache; on a match, the previous hash is reused and the file is never read. Two safety valves keep this honest: files modified within the last two seconds are always re-read (defeating the classic "same-size edit within mtime granularity" race), and during a saving pass a cached hash is only trusted if the blob actually exists in the store. On a ~400-file tree, warm `status` completes in under 100 ms.

### What checkout guarantees

Tree entry names are validated on read (no `/`, `\`, NUL, `.`, `..`, empty, or `.sb`), so a hostile tree object cannot write outside the repository. Files are written to a temp name and atomically renamed into place. Directory pruning after deletions compares resolved paths — it can never touch `.sb` itself.

---

## 12. Ignoring files

`.sbignore` in the repository root holds one glob pattern per line; `#` starts a comment. A pattern matches the full relative path, that path as a directory prefix, or any single path component:

```
# .sbignore
*.log
build
.env
node_modules
data/*.tmp
```

`sb ignore <pattern>` appends for you. Always ignored regardless of `.sbignore`: `.sb` itself, `.git`, `node_modules`, `__pycache__`, `*.pyc`, `.DS_Store`. The `.sbignore` file itself is tracked, so ignore rules travel with branches like any other file.

Two behaviors worth knowing: ignoring a pattern does not remove already-saved files from history (delete them and save), and ignored files are invisible to `save`/`status` but never deleted by sb.

---

## 13. Everyday workflows

**Solo project, straight line.** `sb init`, then work/`sb status`/`sb save` in a loop. Add a pre-save syntax gate (`sb test new pre-save 10-syntax`) on day one; it costs a second per save and eliminates "committed broken code" forever.

**Safe experiment.** `sb branch spike && sb switch spike`, hack freely with saves as checkpoints. If it works: `sb switch main && sb merge spike`. If it doesn't: `sb switch main` and simply never merge — the branch stays as a record, costing nothing.

**"I broke it ten minutes ago."** `sb diff` to see the damage; `sb restore <file>` to reclaim one file from the last save; `sb undo` to revert the whole last save (non-destructively — you can `sb undo` again to change your mind).

**Release with a paper trail.** Keep the real test suite at `sb-tests/pre-deploy/`. Ship with `sb deploy v1.4`: sb verifies the entire store, runs the suite against a clean checkout of exactly what's shipping, and journals the record. `sb deploy --list` is your release history, protected by the chain.

**Weekly trust ritual.** `sb verify`, note the chain head next to the date somewhere off-machine. Thirty seconds; afterward, no rewrite of any history before that moment can escape `sb verify --anchor`.

**Two branches touched the same file.** Merge anyway — if the edits don't overlap line-wise, sb combines them automatically and tells you (`1 file(s) auto-merged`). If they truly overlap, sb stops *before touching anything*, names the files and reasons, and your working folder is untouched. Reconcile on one branch, save, merge again.

---

## 14. sb versus git

| | **sb** | **git** |
|---|---|---|
| Mental model | work → save | work → stage → commit (+ index states) |
| Staging area | none — a save is what you see | the index, with its own command set |
| Detached HEAD | impossible | routine source of confusion |
| Destroying history | no command does it | `reset --hard`, `push -f`, dropped stashes, expired reflog |
| Undo | `sb undo` — a new save, reversible | `revert` vs `reset` vs `restore` vs `checkout` |
| Repository format | one crash-safe SQLite file, ACID everything | thousands of loose files + packfiles + refs + index |
| Operation audit log | hash-chained journal, cross-checked vs refs | reflog: per-machine, expiring, mutable, unchained |
| Tamper evidence | chain + tip cross-check + external anchors | commit DAG only; refs/reflog unprotected |
| Secret prevention | built into every save | third-party hooks you must install |
| Test enforcement | versioned gates on clean checkouts, on by default | hooks: unversioned, per-clone, easily absent |
| Merge conflicts | auto-merge non-overlap; on conflict, stops cleanly, worktree untouched | conflict markers + in-progress merge state to manage |
| Remotes / collaboration | not yet (roadmap) | git's core strength |
| Ecosystem | one file, zero deps | vast |

The honest summary: **git is a distributed collaboration system that you can also use alone; sb is a personal safety-and-integrity system, designed for the way individuals and small teams actually work.** If you need GitHub-style multi-party collaboration today, use git — possibly with sb alongside it (they coexist fine; sb ignores `.git`, add `.sb` to `.gitignore`).

---

## 15. Environment variables

| variable | effect | default |
|---|---|---|
| `SB_NAME` | attribution name for saves (overrides `sb who`) | profile, else OS username |
| `SB_EMAIL` | attribution email | profile, else `<name>@local` |
| `SB_HOME` | folder for the global profile | `~/.config/sandbox` |
| `SB_TEST_TIMEOUT` | seconds allowed per test script | `120` |

Inside test scripts, sb additionally exports `SB_STAGE`, `SB_BRANCH`, `SB_COMMIT`, `SB_REPO` (Section 6).

---

## 16. Exit codes

| code | meaning |
|---|---|
| `0` | success |
| `1` | usage or state error (not a repo, unsaved changes, unknown branch, corrupt object hit mid-operation, …) |
| `2` | a **gate** stopped you: secrets found, tests failed, merge conflicts, or `verify` found problems |
| `130` | interrupted (Ctrl-C) |

The `1`/`2` split is script-friendly: `2` always means "sb worked correctly and is protecting you," so automation can distinguish "fix your command" from "fix your content."

---

## 17. FAQ

**Where did the signatures go? Is sb less secure now?**
The Ed25519 signing was removed on purpose, and Section 8 explains why in full: keys with no management story prove nothing, and a hand-rolled fallback implementation is a liability, not a feature. Every property the signatures *actually* delivered in practice — integrity and tamper evidence — is preserved by content re-hashing, the journal chain, the tip cross-check, and anchors, with zero crypto dependencies and zero key files to lose or leak.

**Is SHA-256 "cryptography"? I asked for none.**
It's a hash function from Python's standard library (`hashlib`) used as a content fingerprint — no keys, no encryption, no signatures, no third-party crypto code. Remove hashing itself and content addressing, integrity checking, and the journal all cease to exist; it is the single primitive the entire design stands on.

**How do I back up a repository?**
Copy `.sb/sandbox.db` (the safest moment is any time sb isn't mid-command, and SQLite's WAL makes even that forgiving), or copy the whole project folder. One file — every backup tool handles it trivially. After restoring, run `sb verify`.

**Can I have partial commits, like `git add -p`?**
No, by design: a save is exactly your working tree, which is what makes "the tests passed on the save" meaningful. If you have two unrelated changes, the sb-native move is to do them on two branches, or save them in sequence.

**What about large or binary files?**
They're stored (zlib-compressed) and versioned like anything else; `diff` won't render them and the secret scanner skips them. Every version of a large file is retained in full, so a repo full of frequently-changing gigabyte assets will grow accordingly — delta compression is on the roadmap.

**Symlinks?**
Skipped with a visible note, not silently, and not yet tracked. On the roadmap.

**Can two commands run at once?**
SQLite serializes writers, so concurrent sb commands won't corrupt anything; a second writer may see a "database is locked" store error and simply retries. sb is a single-user-at-a-time tool by design.

**Does anything leave my machine?**
No. No network code exists in sb — no telemetry, no phoning home, nothing.

**Can I rename a branch or delete one?**
Not yet — both are journal-visible operations on the roadmap. Today a branch you're done with simply sits at its last save, costing one row.

---

## 18. Troubleshooting

**`error: not inside a sandbox repository`** — you're outside any folder containing `.sb/sandbox.db`; `cd` in, or `sb init`.

**`error: you have unsaved changes`** — `switch`, `merge`, and `undo` refuse to run over uncommitted work, always. `sb save "wip"` (saves are cheap and undo is free), or `sb restore <file>` for changes you want gone.

**`save blocked — possible secrets detected`** — Section 7. Remove the secret, ignore the file, or `--allow-secrets` if you're sure.

**`pre-save tests failed — save blocked`** — the last 15 lines of the failing script are printed above the error. Reproduce with `sb test pre-save`. Override once with `--no-verify` if the gate itself is broken — then fix the gate.

**`merge stopped — these files conflict`** — the reason is printed per file. Your worktree was not touched. Reconcile the file on one branch (copy the other branch's version over it, or hand-combine), save, merge again.

**`object … does not match its hash` / `sb verify` reports problems** — real corruption or tampering was detected; sb stopped rather than propagating it. Restore `.sb/sandbox.db` from a backup, then `sb verify` again. Undamaged files can be rescued first via `sb restore` from saves whose objects are intact.

**`store error: … database is locked`** — another sb command (or something else) has the database open for writing. Wait a moment and retry.

**`repository format N is newer than this sb understands`** — the repo was made by a newer sb; upgrade the tool. sb never guesses at future formats.

**A file isn't being saved** — it matches an ignore rule. Check `.sbignore` and the built-in defaults (Section 12); note symlinks are skipped with a printed note.

---

## 19. Known limitations and roadmap

Stated plainly, because a tool that hides its edges isn't trustworthy:

- **No live remotes yet.** Moving a repository between machines today is done with encrypted `.sbox` archives (`sb pack` / `sb unpack`, Section 10), which is deliberate and secure but manual. Continuous *sync* — designed journal-first, so the tamper-evidence story extends across machines rather than being bolted on — is the top roadmap item.
- **`pack`/`unpack` need network access** to fetch the vox encryption module at run time (the trade for keeping sb itself crypto-free). Every other command works fully offline.
- **No symlink tracking** (skipped with a note).
- **Whole-file storage.** zlib-compressed but not delta-compressed; fine for code and documents, heavy for huge frequently-changing binaries.
- **Conservative merges.** Adjacent-line edits and same-point insertions conflict rather than merge; and conflicts are resolved by reconciling on a branch, not via in-worktree conflict markers with a merge-in-progress state (an explicit simplicity trade — sb never leaves your worktree in a special mode).
- **No branch rename/delete, no per-save tags** yet.
- **Anchors are manual.** Automatic anchoring (e.g., appending each chain head to a file on another volume) is roadmap.
- **Single-writer.** Concurrent sb invocations are safe but serialized.

---

*sb — one file, no dependencies, nothing silently destroyed.*# sandbox
optimized repository version control
