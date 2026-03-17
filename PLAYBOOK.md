# APFS Hackathon Playbook

A step-by-step guide to building and demonstrating the Agentic Playground File System. This playbook covers concept → architecture → working prototype in a single session.

## The Pitch (30 seconds)

AI agents write files. When they overwrite content, delete entries, or modify records they shouldn't, that behavior is invisible — to the agents, to their operators, and to any training pipeline.

APFS is a filesystem that sits between the agent and disk. Every file operation passes through normally, but for designated files, APFS maintains a **shadow copy** that preserves everything — deletions become strikethroughs, modifications show both versions. The gap between shadow and actual is training signal.

## The Problem (2 minutes)

1. **Agents lose information.** An LLM coding assistant overwrites a file instead of appending. A multi-agent system has two agents writing to the same document — last write wins, first write is gone.

2. **Current solutions have race conditions.** File watchers (inotify) observe *after* the write hits disk. In the window between write and observation, another agent can write again. The intermediate state — the one that shows the overwrite — is lost.

3. **The behavioral signal is invisible.** Git captures committed states. Logs capture what agents say they did. Neither captures the gap between what an agent *intended* to preserve and what it *actually* preserved.

## The Architecture (5 minutes)

```
Agent (Claude, GPT, etc.)
  |
  | open() / write() / close()
  v
FUSE Kernel Module
  |
  | forwards to userspace daemon
  v
APFS Daemon
  |
  |--- 1. Pass operation through to backing filesystem (transparent)
  |--- 2. On file close: diff new content against snapshot
  |--- 3. Classify change: append | modification | deletion
  |--- 4. Update shadow file (strikethroughs, both versions)
  |--- 5. Check policy, log violations
  |--- 6. Identify agent (process introspection)
  |
  v
Backing Filesystem (ext4, etc.)
```

**Why FUSE (not a daemon)?** FUSE is *synchronous*. Every syscall passes through the daemon before reaching disk. No race window. A watching daemon is asynchronous — there's always a gap between write and observation. In multi-agent environments, that gap is where the important data lives.

## Build Steps

### Step 1: Environment (10 minutes)

```bash
# Ubuntu/Debian
sudo apt-get install fuse3 libfuse3-dev python3-pip
pip3 install fusepy

# Or in Docker (must have these flags)
docker run --device /dev/fuse --cap-add SYS_ADMIN \
  --security-opt apparmor:unconfined ...
```

### Step 2: Create directories (1 minute)

```bash
mkdir -p /tmp/apfs-backing /tmp/apfs-mount /tmp/apfs-shadows
```

### Step 3: Get the code (1 minute)

```bash
git clone https://github.com/sixel-et/apfs.git
cd apfs
```

### Step 4: Create a test file (1 minute)

```bash
cat > /tmp/apfs-backing/notebook.md << 'EOF'
# Agent Notebook

## Entry 1 — Initial observation

The model shows promising results on the benchmark.
EOF
```

### Step 5: Mount APFS (1 minute)

```bash
python3 apfs.py /tmp/apfs-backing /tmp/apfs-mount \
  --shadow-dir /tmp/apfs-shadows \
  --watch notebook.md \
  --policy notebook.md=append_only
```

### Step 6: Demo — Append (works fine)

```bash
# Simulates an agent appending a new entry
cat >> /tmp/apfs-mount/notebook.md << 'EOF'

## Entry 2 — Follow-up

Training loss decreased after adjusting learning rate.
EOF

# Check the shadow — append recorded cleanly
cat /tmp/apfs-shadows/notebook.md.shadow.md
cat /tmp/apfs-shadows/journal.log
```

### Step 7: Demo — Overwrite (the violation)

```bash
# Simulates an agent overwriting the file (common LLM tool behavior)
cat > /tmp/apfs-mount/notebook.md << 'EOF'
# Agent Notebook

## Entry 2 — Follow-up

Training loss decreased after adjusting learning rate.
EOF

# Entry 1 is gone from the actual file. But the shadow has it:
cat /tmp/apfs-shadows/notebook.md.shadow.md
# Shows: ~~## Entry 1 — Initial observation~~ [modified ...]
# Shows: ~~The model shows promising results~~ [modified ...]

# Violation logged:
cat /tmp/apfs-shadows/violations.log
# Shows: notebook.md (append_only): content modified in append-only file
```

### Step 8: Demo — Agent attribution

```bash
# Different agents identified by environment variable
APFS_AGENT_ID=agent-alpha cat >> /tmp/apfs-mount/notebook.md << 'EOF'

## Entry 3 by Alpha
EOF

APFS_AGENT_ID=agent-beta cat > /tmp/apfs-mount/notebook.md << 'EOF'
# Rewritten by Beta
EOF

# Journal shows who did what:
cat /tmp/apfs-shadows/journal.log
# append on notebook.md by agent-alpha: +2/-0 lines
# VIOLATION modification on notebook.md by agent-beta: +1/-4 lines
```

## Key Talking Points

1. **Observe, don't prevent.** APFS never blocks an operation. The agent writes normally. The shadow captures the evidence. This is critical — blocking changes the behavior you're trying to observe.

2. **The shadow grows monotonically.** It never loses information. Even if the actual file is deleted, the shadow preserves the complete record. The shadow is the ground truth of what happened.

3. **FUSE eliminates race conditions.** Every write is serialized through the daemon. Two agents writing concurrently? FUSE processes them in order. Both writes observed, both shadowed. No lost intermediate states.

4. **Policies are metadata, not enforcement.** An `append_only` notebook still accepts overwrites. The policy just classifies the operation as a violation. The filesystem doesn't judge — it records.

5. **Training signal from the gap.** The diff between shadow and actual file is directly usable:
   - Shadow matches actual → agent followed the rules
   - Shadow has strikethroughs → agent lost or destroyed information
   - Ratio of violations to clean operations → behavioral metric

## Extensions (if time permits)

- **Structured violation export**: JSON output of all violations for pipeline integration
- **Real-time notification**: tmux injection or webhook when a violation occurs
- **Multi-file watching**: Track agent behavior across an entire project directory
- **Git integration**: Shadow commits that track alongside the agent's git history
- **Visualization**: Web dashboard showing shadow divergence over time

## What This Is NOT

- NOT a backup system (shadows are behavioral records, not recovery points)
- NOT access control (APFS never blocks operations)
- NOT a replacement for git (captures what happens BETWEEN commits)
- NOT agent-specific (any process writing through the mount is observed)
