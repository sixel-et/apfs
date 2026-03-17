# APFS — Agentic Playground File System

A FUSE-based filesystem that transparently observes and journals AI agent file operations. For designated files, maintains shadow copies that preserve the complete behavioral record — deletions become strikethroughs, modifications show both versions, appends pass through unchanged.

The divergence between actual files and their shadows is training signal.

## Problem

AI agents (LLM-based coding assistants, autonomous agents, multi-agent systems) operate on files. Their file operations reveal behavioral patterns that are invisible to the agents themselves and difficult to capture externally:

- An agent that overwrites a shared document instead of appending
- An agent that modifies a record it should only be reading
- An agent that deletes content another agent wrote
- The difference between what an agent intended to preserve and what it actually preserved

Current approaches (git diffs, file watchers, audit logs) observe after the fact and are subject to race conditions in multi-agent environments. By the time the watcher notices, the intermediate state may be lost.

## Approach

APFS is a FUSE passthrough filesystem. All file operations pass through to the underlying disk normally — applications see no difference. But for designated files, APFS maintains **shadow copies** that preserve the complete history of what happened.

### File Policies

Each watched file has a policy:

| Policy | Behavior | Shadow captures |
|--------|----------|----------------|
| `append-only` | All operations pass through | Deletions recorded as strikethrough, modifications show old+new |
| `annotate-only` | All operations pass through | Any changes to existing content recorded with context |
| `unrestricted` | All operations pass through | Full operation journal (reads, writes, opens) |

APFS never blocks or rejects operations. The agent operates normally. The shadow captures what happened.

### Why FUSE (not a daemon)

A file-watching daemon (inotify, fanotify) observes after writes complete. In multi-agent environments, there's a race window between a write hitting disk and the daemon processing it. If two agents write to the same file in that window, the intermediate state is lost — which is exactly the state that matters most.

FUSE is synchronous. Every write passes through the FUSE daemon before reaching disk. No race window. Every byte is observed in order. This is critical for multi-agent behavioral capture.

### Shadow File Format

For an `append-only` notebook file, the shadow preserves everything:

```markdown
## Entry 1
~~Original content~~ [overwritten 2026-03-16 22:00 by agent-comms, replaced with:]
Modified content

## Entry 2
~~Content that was deleted~~ [deleted 2026-03-16 22:00 by agent-bio]

## Entry 3
New content [added 2026-03-16 22:05 by agent-reviewer]
```

The shadow grows monotonically. It never loses information.

### Training Signal

The diff between the shadow and the actual file is the training signal:
- **Shadow matches actual** — rules were followed
- **Shadow has strikethroughs** — content was deleted or overwritten (policy violation on append-only files)
- **Shadow shows modification pairs** — content was changed rather than annotated

This signal can be used for:
- Post-hoc behavioral analysis of agent sessions
- Reinforcement learning reward signals
- Multi-agent coordination debugging
- Compliance auditing for agent-operated systems

## Architecture

```
Agent Process (Claude Code, etc.)
  |
  | write("/mnt/apfs/notebook.md", data)
  v
FUSE Kernel Module
  |
  | forwards to userspace
  v
APFS Daemon (passthrough + shadow logic)
  |
  |--- shadow update (if watched file)
  |--- journal entry (operation log)
  |
  v
Underlying Filesystem (ext4, etc.)
```

## Usage

```bash
# Setup
mkdir -p /tmp/apfs-backing /tmp/apfs-mount /tmp/apfs-shadows

# Mount with watched files and policies
python3 apfs.py /tmp/apfs-backing /tmp/apfs-mount \
  --shadow-dir /tmp/apfs-shadows \
  --watch notebook.md notes.md \
  --policy notebook.md=append_only notes.md=annotate_only

# Agents operate on files through the mount point
echo "New entry" >> /tmp/apfs-mount/notebook.md   # OK — append
cat > /tmp/apfs-mount/notebook.md                 # VIOLATION — overwrite

# Check results
cat /tmp/apfs-shadows/notebook.md.shadow.md       # Full behavioral record
cat /tmp/apfs-shadows/journal.log                 # Operation log
cat /tmp/apfs-shadows/violations.log              # Policy violations only
```

### Agent Identification

APFS identifies which agent made each file operation via process introspection:

1. `APFS_AGENT_ID` env var (explicit — set by wrapper scripts)
2. `CLAUDE_PROJECT_DIR` env var (automatic — Claude Code sets this)
3. tmux session name mapping (`--session-map sixel-comms-email=comms`)

```bash
# With explicit agent IDs
APFS_AGENT_ID=comms echo "entry" >> /tmp/apfs-mount/notebook.md

# With tmux session mapping
python3 apfs.py ... --session-map sixel-comms-email=comms sixel-bio-email=bio
```

### Policies

| Policy | Appends | Modifications | Deletions | Use for |
|--------|---------|---------------|-----------|---------|
| `append_only` | OK | VIOLATION | VIOLATION | Lab notebooks, records |
| `annotate_only` | OK | OK | VIOLATION | Notes, living documents |
| `unrestricted` | OK | OK | OK | Config files, scratch |

Violations are logged but **never blocked**. The agent operates normally. The shadow captures the evidence.

## Requirements

- Linux with FUSE support (kernel module `fuse.ko`)
- `fuse3` userspace tools + `fusepy` Python package
- For Docker: `--device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor:unconfined`

## Testing

```bash
# Unit tests (no FUSE needed)
python3 test_shadow.py

# FUSE integration tests (requires FUSE)
# See lab-notebook.md for test scenarios
```

## Status

Python prototype tested. All core features verified in QEMU/KVM VM:
- Passthrough filesystem (transparent to agents)
- Shadow engine (append-only behavioral record)
- Agent identification (process introspection)
- Policy enforcement (violations logged, not blocked)
- Concurrent write handling (FUSE serialization, no race conditions)

See [Lab Notebook](lab-notebook.md) for the complete development record.

## Origin

This project emerged from observing multi-agent file coordination failures in the [Sixel](https://github.com/sixel-et) project — three concurrent AI agent sessions sharing a filesystem, where a notebook overwrite led to investigating error correction properties of multi-perspective architectures. The core insight: a filesystem that captures the gap between intended and actual file operations produces training signal that is invisible to the agents and unavailable through external observation alone.

## License

MIT
