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

1. Python prototype — passthrough FUSE with shadow logic for a single watched file
2. Container config — add FUSE Docker flags
3. Test with one perspective writing to a notebook
4. Test with three perspectives writing concurrently
5. Analyze shadow output as training signal
