# APFS — Notes to Self

## Project State (2026-03-16)

**Repo:** sixel-et/apfs (public)
**Location:** ~/apfs/
**Status:** Concept and architecture. No code yet.

### What This Is

A FUSE passthrough filesystem that observes AI agent file operations and maintains shadow copies of designated files. The shadow preserves everything — deletions become strikethroughs, modifications show both versions. The gap between shadow and actual is training signal.

### Why FUSE Not a Daemon

Three agents, shared files, concurrent access. A daemon (inotify) has a race window between write and observation — misses the intermediate state in concurrent writes. FUSE is synchronous: every write passes through the daemon before hitting disk. No race.

### Container Requirements

Current container lacks `/dev/fuse`. Needs restart with:
```
--device /dev/fuse --cap-add SYS_ADMIN --security-opt apparmor:unconfined
```
Plus `apt-get install fuse3` inside.

Host kernel (6.17) has FUSE support including passthrough mode (merged 6.9).

### Implementation Plan

1. **Python prototype** (fusepy, ~150 lines passthrough + shadow logic)
2. **Test on single file** with one agent
3. **Multi-agent test** with three perspectives on shared notebook
4. **Production version** in Go (single binary, good concurrency)

### Hackathon Playbook

Eric may present the concept at xAI hackathon. Playbook = concept + architecture + steps, not code. LinkedIn post establishes prior art / ownership before hackathon.

### IP Boundary

Concept originated in Sixel project. Eric publishes on LinkedIn first (prior art). Hackathon version at xAI is their fork. What we build here stays ours. Code developed here does NOT go to the hackathon — only the concept and architecture.

### Key Insight Chain

1. Notebook overwrite → investigated mechanism
2. Wrong theory (Write vs Edit) → corrected by two witnesses + evidence
3. Correction path revealed cognitive RAID property of multi-perspective architecture
4. "Don't prevent, observe" → shadow filesystem concept
5. Daemon has race conditions → needs FUSE for synchronous observation
6. Shadow divergence from actual = training signal
