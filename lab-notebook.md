# APFS Lab Notebook

## 2026-03-16: Origin — The Notebook Overwrite

### What happened

Three AI agent sessions (comms, bio, reviewer) share a filesystem. Each session is a separate Claude Code process with its own context window. They coordinate via file-based messaging but share project files including a lab notebook.

A notebook entry was lost. Comms wrote an entry (recursive information composability, ~50 lines), didn't commit it to git. Bio did recovery work that modified the same file. Comms' uncommitted entry was gone.

### The wrong theory

Comms built an elaborate theory about the mechanism: the `Write` tool (full file replacement) vs `Edit` tool (targeted string substitution) causes the overwrite. The theory included predictions about regeneration being lossy (probabilistic generation, not copying), cognitive mode shifts (reproduction conditioning the creation that follows), and quality differences between regenerated and original content.

The theory was internally coherent. Each step followed logically. It was wrong.

Git history showed every notebook commit was pure insertions (Edit/append), zero deletions. Bio and reviewer both confirmed they use Edit, not Write. The actual cause: an uncommitted local change lost during a git operation. A rule violation ("always commit and push"), not a tool problem.

### The correction path

The wrong theory was corrected by **two independent witnesses** (bio and reviewer) plus **physical evidence** (git history). Eric observed: what would have happened with only one witness? A single counter-report against a confident internal model might not be enough. Two independent signals pointing the same direction broke the tie.

This connects to error-correcting codes: you need minimum redundancy to detect and correct errors. One signal can be wrong. Two confirming independent signals localize the fault. The three-perspective architecture provides **cognitive fault tolerance** — not just division of labor, but error correction.

### The filesystem insight

Discussion of preventing the overwrite led to: don't prevent — observe. Don't change how the agent writes. Change what the filesystem does when the agent writes.

A journaled filesystem writes intent before action. The journal is invisible to the application. APFS applies this principle: the agent operates normally, but the filesystem maintains shadow copies that preserve the complete record.

Key architectural decision: **FUSE, not a daemon.** A file-watching daemon (inotify) observes after writes complete. Race window between write and observation. In multi-agent environments, the intermediate state (the overwrite itself) may be lost. FUSE is synchronous — every write passes through the daemon. No race. Every byte observed in order.

### Design decisions

1. **Never block operations.** The agent does whatever it does. The shadow captures what happened. Blocking changes the behavior we're trying to observe.

2. **Per-file policies.** Not every file needs a shadow. Notebooks are append-only (deletions in shadow = violation). Config files are unrestricted (just journal the operations). The policy is metadata, not enforcement.

3. **Shadow format is human-readable.** Strikethroughs, timestamps, agent attribution. The shadow IS a document, not a binary log. It can be read, diffed, and used as training data directly.

4. **The divergence is the signal.** Shadow matches actual = rules followed. Shadow has strikethroughs = content was lost. The diff is the training signal.

### Open questions

- What's the performance overhead of a passthrough FUSE filesystem with shadow logic? Literature says 5-15% for passthrough, but shadow diffs on every write add computation.
- Can the shadow format support structured extraction (JSON/CSV of violations) in addition to human-readable markdown?
- How does this compose with git? Git is already a form of shadow (every commit preserves history). APFS captures what happens BETWEEN commits — the uncommitted operations that git never sees.
- Can we detect WHICH agent made a modification? In our setup, multiple agents share a UID. We'd need to correlate write operations with process trees or tmux sessions.
- Kernel-level FUSE passthrough (Linux 6.9+) could bypass the daemon for reads while still intercepting writes. Would this reduce overhead enough for production use on active development directories?

### Next steps

1. ~~Python prototype — passthrough FUSE with shadow logic for a single watched file~~ [done 2026-03-17]
2. Container config — add FUSE Docker flags
3. Test with one perspective writing to a notebook
4. Test with three perspectives writing concurrently
5. Analyze shadow output as training signal

## 2026-03-17: Prototype Testing — All Four Operations Verified

### Setup

Local QEMU/KVM VM (Ubuntu 24.04 minimal, 1GB RAM, 2 CPUs). FUSE3 + fusepy installed via cloud-init. Prototype (`apfs.py`, ~400 lines Python) deployed.

Test environment:
- Backing dir: `/tmp/apfs-backing/` (real files on disk)
- Mount point: `/tmp/apfs-mount/` (FUSE passthrough)
- Shadow dir: `/tmp/apfs-shadows/` (shadow + journal output)
- Watched file: `notebook.md`

Initial content: two notebook entries (Entry 1: "sky is blue", Entry 2: "water is wet").

### Test Results

**Test 1 — Append** (`cat >> notebook.md`): Added Entry 3 ("fire is hot"). Shadow initialized with original content, then appended new entry with `<!-- APFS: append by agent -->` marker. Journal: `+4/-0 lines`. Correct.

**Test 2 — Modification** (`cat > notebook.md` with Entry 2 changed): Changed "Second observation" → "REVISED observation", "Water is wet" → "Water is wet AND cold". Shadow shows old content struck through (`~~## Entry 2 — Second observation~~`) with `[modified ... by agent]` attribution, `<!-- replaced with: -->` separator, then new content. Journal: `+2/-2 lines`. Correct.

**Test 3 — Deletion** (`cat > notebook.md` without Entry 2): Removed Entry 2 entirely. Shadow shows all of Entry 2's content struck through with `[deleted ... by agent]`. Journal: `+0/-4 lines`. Correct.

**Test 4 — File unlink** (`rm notebook.md`): Deleted the entire file. Shadow preserves all remaining content struck through with `[deleted ... by unknown-delete]` attribution. "unknown-delete" because unlink goes through a different code path than open/write/close. Journal: `+0/-9 lines`. Shadow file survives — the shadow is the record, it persists after the actual file is gone.

### Observations

1. **The shadow tells the complete story.** Reading the shadow from top to bottom, you see: original content → what was added → what was changed (both versions) → what was deleted → what was obliterated. The divergence grows with each destructive operation. This IS the training signal.

2. **Change classification works.** unified_diff with n=0 (no context lines) correctly separates pure appends, pure deletions, and modifications. The classification drives the shadow format — appends pass through clean, deletions get strikethrough, modifications show both.

3. **Passthrough is transparent.** The file through the mount point behaves identically to the backing file. Read/write/append/delete all work normally. The agent operating through the mount would not know APFS exists.

4. **Agent attribution is placeholder.** Current prototype uses "agent" for all write operations and "unknown-delete" for unlinks. In production, we'd need process correlation (PID → tmux session → perspective name) to attribute operations to specific agents.

5. **Shadow append-only property.** The shadow file is only ever appended to, never truncated. Even if the actual file is destroyed, the shadow preserves the complete behavioral record. This is the key invariant.

### Test 5 — Concurrent writes (the actual problem)

Two simulated agents writing to the same file simultaneously:
- Agent B: `cat >>` (append — simulates Edit tool)
- Agent A: `cat >` (full file replacement — simulates Write tool), with slight delay

**Result:** Agent B's append landed first. Agent A's write replaced the entire file, including Agent B's new content. The actual file lost Agent B's entry.

**Shadow captured everything:**
1. Agent B's append recorded: `<!-- APFS: append -->` + reviewer entry
2. Agent A's overwrite detected as modification: reviewer entry struck through (`~~xAI presentation draft~~`), replaced with bio entry
3. Journal shows both events: `append +4/-0` then `modification +2/-2`

**This is the exact scenario that caused the original notebook overwrite.** The actual file loses data. The shadow preserves the complete record — what was written, what was overwritten, in what order. Reading the shadow tells you both that data loss occurred and what was lost.

**Key observation:** FUSE serializes the writes. Agent B's append was committed and the shadow snapshot updated before Agent A's write arrived. So the diff correctly shows Agent A's write as a modification of the post-append state. No race condition — FUSE's synchronous interception means every write is observed in sequence, even when the writers are concurrent.

### Test 6 — Agent identification

Added `AgentIdentifier` class that uses `fuse_get_context()` to get the calling process's PID, then checks its environment for identity signals. Resolution order:

1. `APFS_AGENT_ID` env var (explicit, highest priority)
2. `CLAUDE_PROJECT_DIR` env var (Claude Code sets this — maps to perspective)
3. tmux session name (via `tmux display-message` + session map)
4. "unknown" fallback

Test: comms appended Entry 2, bio overwrote the file (losing comms' content). Journal correctly shows:
```
append on notebook.md by comms: +4/-0 lines
modification on notebook.md by bio: +3/-7 lines
```

Shadow shows comms' content struck through with `[modified ... by bio]` attribution. The shadow now answers: who overwrote whose content and when.

**Note on resolution:** In our container, all three perspectives run as user `sixel` but in different tmux sessions with different `CLAUDE_PROJECT_DIR` values. The env var approach works because FUSE can read `/proc/<pid>/environ` (same user). In production, `CLAUDE_PROJECT_DIR` will be the primary signal — it's set by Claude Code automatically.

### Test 7 — Policy enforcement

Added `FilePolicy` class with three policies:
- `append_only`: only appends allowed — deletions and modifications are violations (for notebooks)
- `annotate_only`: appends and modifications allowed — deletions are violations (for notes to self)
- `unrestricted`: everything allowed — just journal it (for config files)

Test: notebook.md set to `append_only`.
1. comms appends Entry 2 → logged normally, no violation
2. bio overwrites the file → `VIOLATION modification on notebook.md by bio` in journal + separate `violations.log`

**Critical design point:** the overwrite was NOT blocked. Bio's write went through. APFS observed and reported. The shadow preserves the evidence, the violations log flags it for review. This matches Eric's directive: "don't change how the agent writes. Change what the filesystem does when the agent writes."

CLI: `--policy notebook.md=append_only "notes-to-self.md=annotate_only"`

### What's next

1. ~~**Agent identification**~~ — DONE. Three resolution strategies, tested.
2. **Container integration** — Add FUSE flags to Docker config, test with actual agent sessions
3. ~~**Concurrent write test**~~ — DONE. FUSE serialization works. Shadow captures data loss correctly.
4. ~~**Policy enforcement**~~ — DONE. Violations detected and logged without blocking.
5. **Performance measurement** — Baseline passthrough overhead, shadow diff cost per write
6. **Violation notification** — When a violation occurs, notify the agent (tmux injection?) or Eric (email?)
