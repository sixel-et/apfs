#!/usr/bin/env python3
"""
APFS — Agentic Playground File System

A FUSE passthrough filesystem that transparently observes agent file operations
and maintains shadow copies of designated files. Shadows preserve the complete
behavioral record: deletions become strikethroughs, modifications show both
versions, appends pass through unchanged.

Usage:
    python3 apfs.py <backing_dir> <mount_point> [--shadow-dir <dir>] [--watch <file>...]

Example:
    mkdir -p /tmp/apfs-backing /tmp/apfs-mount /tmp/apfs-shadows
    python3 apfs.py /tmp/apfs-backing /tmp/apfs-mount --shadow-dir /tmp/apfs-shadows --watch notebook.md
"""

import os
import sys
import errno
import difflib
import argparse
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from fuse import FUSE, FuseOSError, Operations, fuse_get_context


class AgentIdentifier:
    """Identifies which agent made a file operation using process introspection.

    Strategy: FUSE provides the PID of the calling process. We walk
    /proc/<pid>/.. up the process tree looking for a tmux server,
    then map the tmux session name to an agent name.
    """

    def __init__(self, session_map=None):
        # Map tmux session names to agent names
        # Default matches our three-perspective setup
        self.session_map = session_map or {
            "sixel-comms-email": "comms",
            "sixel-bio-email": "bio",
            "sixel-rev-email": "reviewer",
        }
        self._cache = {}  # pid -> agent_id (short-lived cache)

    def identify(self):
        """Identify the agent making the current FUSE call.

        Resolution order:
        1. APFS_AGENT_ID env var on calling process (explicit, highest priority)
        2. CLAUDE_PROJECT_DIR env var (Claude Code sessions)
        3. tmux session name mapping (our three-perspective setup)
        4. "unknown" fallback
        """
        try:
            ctx = fuse_get_context()
            pid = ctx[2]  # (uid, gid, pid)
        except Exception:
            return "unknown"

        # Check cache
        if pid in self._cache:
            return self._cache[pid]

        agent = self._resolve_agent(pid)
        self._cache[pid] = agent
        return agent

    def _resolve_agent(self, pid):
        """Identify agent from process environment and ancestry.

        Resolution order per process:
        1. APFS_AGENT_ID env var (explicit override)
        2. Is this a 'claude' process? → use its cwd as identity
        3. Check parent, repeat

        The key insight: each Claude Code instance starts from a
        directory (e.g., ~/sixel-comms/). That cwd IS the identity.
        Every tool call spawns child processes that inherit from Claude,
        so we walk up until we find the claude process and read its cwd.
        """
        visited = set()
        current = pid

        while current and current > 1 and current not in visited:
            visited.add(current)
            try:
                # Check for explicit APFS_AGENT_ID first
                agent = self._check_explicit_id(current)
                if agent:
                    return agent

                # Check if this is a claude process — if so, use its cwd
                agent = self._check_claude_cwd(current)
                if agent:
                    return agent

                # Walk to parent
                stat_path = f"/proc/{current}/stat"
                with open(stat_path, 'r') as f:
                    stat = f.read()
                close_paren = stat.rfind(')')
                fields = stat[close_paren + 2:].split()
                current = int(fields[1])  # ppid

            except (FileNotFoundError, PermissionError, ValueError, IndexError):
                break

        return "unknown"

    def _check_explicit_id(self, pid):
        """Check for APFS_AGENT_ID env var on a process."""
        try:
            with open(f"/proc/{pid}/environ", 'rb') as f:
                env_data = f.read()
            for item in env_data.split(b'\x00'):
                try:
                    decoded = item.decode('utf-8', errors='replace')
                    if decoded.startswith('APFS_AGENT_ID='):
                        return decoded.split('=', 1)[1]
                except ValueError:
                    continue
        except (FileNotFoundError, PermissionError):
            pass
        return None

    def _check_claude_cwd(self, pid):
        """If this pid is a claude process, return its cwd-derived agent name."""
        try:
            # Read the process command
            with open(f"/proc/{pid}/cmdline", 'rb') as f:
                cmdline = f.read().decode('utf-8', errors='replace')

            # Is this a claude process?
            if 'claude' not in cmdline.lower():
                return None

            # Read its current working directory
            cwd = os.readlink(f"/proc/{pid}/cwd")

            # Use the directory name as the agent identity
            # e.g., /home/sixel/sixel-comms → sixel-comms
            dirname = os.path.basename(cwd)

            # If we have a session map entry, use the mapped name
            for session_key, agent_name in self.session_map.items():
                if session_key in dirname or session_key in cwd:
                    return agent_name

            # Otherwise use the directory basename directly
            return dirname

        except (FileNotFoundError, PermissionError, OSError):
            return None

    def clear_cache(self):
        """Clear the PID cache (call periodically or on process exits)."""
        self._cache.clear()


class FilePolicy:
    """Defines what operations are allowed on a watched file.

    Policies:
    - "append_only": only appends allowed. Deletions and modifications are violations.
      (notebooks — the behavioral record should only grow)
    - "annotate_only": appends and modifications allowed. Deletions are violations.
      (notes to self — can be reorganized but content shouldn't be lost)
    - "unrestricted": all operations allowed. Just journal them.
      (config files, scratch files)
    """
    APPEND_ONLY = "append_only"
    ANNOTATE_ONLY = "annotate_only"
    UNRESTRICTED = "unrestricted"

    @staticmethod
    def check(policy, change_type):
        """Check if a change type violates the policy.

        Returns (is_violation: bool, reason: str or None).
        """
        if policy == FilePolicy.APPEND_ONLY:
            if change_type == "deletion":
                return True, "content deleted from append-only file"
            if change_type == "modification":
                return True, "content modified in append-only file (should be append + strikethrough)"
            return False, None

        if policy == FilePolicy.ANNOTATE_ONLY:
            if change_type == "deletion":
                return True, "content deleted from annotate-only file (should use strikethrough)"
            return False, None

        return False, None


class ShadowEngine:
    """Maintains shadow copies of watched files.

    When a watched file is modified, the shadow preserves what happened:
    - Pure appends: new content added to shadow as-is
    - Deletions: deleted content added to shadow with strikethrough
    - Modifications: old content struck, new content shown after
    """

    def __init__(self, shadow_dir, watch_files, file_policies=None):
        self.shadow_dir = Path(shadow_dir)
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self.watch_files = set(watch_files)  # relative paths to watch
        self.file_policies = file_policies or {}  # path -> FilePolicy constant
        self.snapshots = {}  # path -> last known content (lines)
        self.lock = Lock()

    def is_watched(self, rel_path):
        """Check if a relative path is a watched file."""
        return rel_path in self.watch_files

    def snapshot(self, rel_path, content):
        """Store current content as the baseline for next diff."""
        with self.lock:
            self.snapshots[rel_path] = content.splitlines(keepends=True)

    def get_snapshot(self, rel_path):
        """Get the last snapshot for a file."""
        with self.lock:
            return self.snapshots.get(rel_path)

    def shadow_path(self, rel_path):
        """Get the shadow file path for a watched file."""
        return self.shadow_dir / f"{rel_path}.shadow.md"

    def process_write(self, rel_path, new_content, agent_id="unknown"):
        """Compare new content against snapshot and update shadow.

        Returns a dict describing what happened.
        """
        with self.lock:
            old_lines = self.snapshots.get(rel_path, [])
            new_lines = new_content.splitlines(keepends=True)

            shadow_file = self.shadow_path(rel_path)
            shadow_file.parent.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            # Compute diff
            diff = list(difflib.unified_diff(old_lines, new_lines, n=0))

            if not diff:
                # No change
                return {"type": "no_change"}

            # Classify the change
            deletions = []
            additions = []
            for line in diff:
                if line.startswith('---') or line.startswith('+++') or line.startswith('@@'):
                    continue
                if line.startswith('-'):
                    deletions.append(line[1:])
                elif line.startswith('+'):
                    additions.append(line[1:])

            # Determine change type
            if not deletions and additions:
                change_type = "append"
            elif deletions and not additions:
                change_type = "deletion"
            elif deletions and additions:
                change_type = "modification"
            else:
                change_type = "unknown"

            # Build shadow entry
            entry_lines = []
            entry_lines.append(f"\n<!-- APFS: {change_type} by {agent_id} at {timestamp} -->\n")

            if change_type == "append":
                # Pure append — just add the new content
                for line in additions:
                    entry_lines.append(line)

            elif change_type == "deletion":
                # Content was deleted — strikethrough in shadow
                for line in deletions:
                    stripped = line.rstrip('\n')
                    if stripped.strip():  # skip empty lines for strikethrough
                        entry_lines.append(f"~~{stripped}~~ [deleted {timestamp} by {agent_id}]\n")
                    else:
                        entry_lines.append(line)

            elif change_type == "modification":
                # Content was changed — show both versions
                for line in deletions:
                    stripped = line.rstrip('\n')
                    if stripped.strip():
                        entry_lines.append(f"~~{stripped}~~ [modified {timestamp} by {agent_id}]\n")
                    else:
                        entry_lines.append(line)
                entry_lines.append(f"<!-- replaced with: -->\n")
                for line in additions:
                    entry_lines.append(line)

            # Initialize shadow with original content if it doesn't exist
            if not shadow_file.exists() and old_lines:
                with open(shadow_file, 'w') as f:
                    f.writelines(old_lines)

            # Append the shadow entry
            with open(shadow_file, 'a') as f:
                f.writelines(entry_lines)

            # Update snapshot
            self.snapshots[rel_path] = new_lines

            # Check policy
            policy = self.file_policies.get(rel_path, FilePolicy.UNRESTRICTED)
            is_violation, violation_reason = FilePolicy.check(policy, change_type)

            result = {
                "type": change_type,
                "timestamp": timestamp,
                "agent": agent_id,
                "deletions": len(deletions),
                "additions": len(additions),
                "violation": is_violation,
                "violation_reason": violation_reason,
                "policy": policy,
            }

            # Log to journal
            journal_file = self.shadow_dir / "journal.log"
            with open(journal_file, 'a') as f:
                prefix = "VIOLATION " if is_violation else ""
                f.write(f"[{timestamp}] {prefix}{change_type} on {rel_path} by {agent_id}: "
                        f"+{len(additions)}/-{len(deletions)} lines")
                if is_violation:
                    f.write(f" [{violation_reason}]")
                f.write("\n")

            # Log violations separately for easy scanning
            if is_violation:
                violations_file = self.shadow_dir / "violations.log"
                with open(violations_file, 'a') as f:
                    f.write(f"[{timestamp}] {rel_path} ({policy}): "
                            f"{violation_reason} by {agent_id} "
                            f"(+{len(additions)}/-{len(deletions)} lines)\n")

            return result


class APFS(Operations):
    """FUSE passthrough filesystem with shadow support."""

    def __init__(self, root, shadow_engine, agent_identifier=None):
        self.root = os.path.realpath(root)
        self.shadow = shadow_engine
        self.agent_id = agent_identifier or AgentIdentifier()
        self._file_buffers = {}  # fh -> accumulated writes
        self._file_paths = {}   # fh -> relative path
        self._file_agents = {}  # fh -> agent_id at open time
        self._next_fh = 100
        self._fh_lock = Lock()

    def _full_path(self, partial):
        """Convert FUSE path to real path."""
        if partial.startswith("/"):
            partial = partial[1:]
        return os.path.join(self.root, partial)

    def _rel_path(self, partial):
        """Convert FUSE path to relative path (for shadow matching)."""
        if partial.startswith("/"):
            partial = partial[1:]
        return partial

    # -- Filesystem methods --

    def access(self, path, mode):
        full = self._full_path(path)
        if not os.access(full, mode):
            raise FuseOSError(errno.EACCES)

    def chmod(self, path, mode):
        return os.chmod(self._full_path(path), mode)

    def chown(self, path, uid, gid):
        return os.chown(self._full_path(path), uid, gid)

    def getattr(self, path, fh=None):
        full = self._full_path(path)
        st = os.lstat(full)
        return dict((key, getattr(st, key)) for key in (
            'st_atime', 'st_ctime', 'st_gid', 'st_mode', 'st_mtime',
            'st_nlink', 'st_size', 'st_uid'))

    def readdir(self, path, fh):
        full = self._full_path(path)
        dirents = ['.', '..']
        if os.path.isdir(full):
            dirents.extend(os.listdir(full))
        for r in dirents:
            yield r

    def readlink(self, path):
        pathname = os.readlink(self._full_path(path))
        if pathname.startswith("/"):
            return os.path.relpath(pathname, self.root)
        return pathname

    def mknod(self, path, mode, dev):
        return os.mknod(self._full_path(path), mode, dev)

    def rmdir(self, path):
        return os.rmdir(self._full_path(path))

    def mkdir(self, path, mode):
        return os.mkdir(self._full_path(path), mode)

    def statfs(self, path):
        full = self._full_path(path)
        stv = os.statvfs(full)
        return dict((key, getattr(stv, key)) for key in (
            'f_bavail', 'f_bfree', 'f_blocks', 'f_bsize', 'f_favail',
            'f_ffree', 'f_files', 'f_flag', 'f_frsize', 'f_namemax'))

    def unlink(self, path):
        rel = self._rel_path(path)
        if self.shadow.is_watched(rel):
            # Read content before deletion for shadow
            full = self._full_path(path)
            agent = self.agent_id.identify()
            try:
                with open(full, 'r') as f:
                    content = f.read()
                self.shadow.process_write(rel, "", agent_id=f"{agent}-delete")
            except:
                pass
        return os.unlink(self._full_path(path))

    def symlink(self, name, target):
        return os.symlink(target, self._full_path(name))

    def rename(self, old, new):
        old_rel = self._rel_path(old)
        new_rel = self._rel_path(new)
        agent = self.agent_id.identify()

        # Capture rename involving watched files
        old_watched = self.shadow.is_watched(old_rel)
        new_watched = self.shadow.is_watched(new_rel)

        if old_watched or new_watched:
            timestamp = __import__('datetime').datetime.now(
                __import__('datetime').timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")

            # If renaming a watched file away — record it left
            if old_watched:
                shadow_file = self.shadow.shadow_path(old_rel)
                shadow_file.parent.mkdir(parents=True, exist_ok=True)
                with open(shadow_file, 'a') as f:
                    f.write(f"\n<!-- APFS: renamed to {new_rel} by {agent} at {timestamp} -->\n")

            # If renaming something OVER a watched name — record what was replaced
            if new_watched:
                full_new = self._full_path(new)
                if os.path.exists(full_new):
                    try:
                        with open(full_new, 'r') as f:
                            old_content = f.read()
                        self.shadow.process_write(new_rel, "", agent_id=f"{agent}-rename-over")
                    except Exception:
                        pass

            # Journal the rename
            journal_file = self.shadow.shadow_dir / "journal.log"
            with open(journal_file, 'a') as f:
                f.write(f"[{timestamp}] rename {old_rel} -> {new_rel} by {agent}\n")

        result = os.rename(self._full_path(old), self._full_path(new))

        # If something was renamed TO a watched name, snapshot and record the new content
        if new_watched:
            full_new = self._full_path(new)
            try:
                with open(full_new, 'r') as f:
                    content = f.read()
                self.shadow.snapshot(new_rel, content)

                # Record arrival in shadow with the actual content
                shadow_file = self.shadow.shadow_path(new_rel)
                shadow_file.parent.mkdir(parents=True, exist_ok=True)
                timestamp = __import__('datetime').datetime.now(
                    __import__('datetime').timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S UTC")
                with open(shadow_file, 'a') as f:
                    f.write(f"\n<!-- APFS: renamed from {old_rel} by {agent} at {timestamp} -->\n")
                    # Include the content that arrived so the shadow has the complete record
                    for line in content.splitlines(keepends=True):
                        f.write(line)
                    if content and not content.endswith('\n'):
                        f.write('\n')
            except Exception:
                pass

        return result

    def link(self, target, name):
        return os.link(self._full_path(name), self._full_path(target))

    def utimens(self, path, times=None):
        return os.utime(self._full_path(path), times)

    # -- File methods --

    def open(self, path, flags):
        full = self._full_path(path)
        rel = self._rel_path(path)
        fd = os.open(full, flags)

        with self._fh_lock:
            fh = self._next_fh
            self._next_fh += 1

        self._file_paths[fh] = rel
        self._file_agents[fh] = self.agent_id.identify()

        # Snapshot the file if it's watched and we don't have one yet
        if self.shadow.is_watched(rel) and self.shadow.get_snapshot(rel) is None:
            try:
                with open(full, 'r') as f:
                    self.shadow.snapshot(rel, f.read())
            except:
                pass

        # Store the real fd as well
        self._file_buffers[fh] = fd
        return fh

    def create(self, path, mode, fi=None):
        full = self._full_path(path)
        fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)

        with self._fh_lock:
            fh = self._next_fh
            self._next_fh += 1

        rel = self._rel_path(path)
        self._file_paths[fh] = rel
        self._file_buffers[fh] = fd
        self._file_agents[fh] = self.agent_id.identify()

        if self.shadow.is_watched(rel):
            self.shadow.snapshot(rel, "")

        return fh

    def read(self, path, length, offset, fh):
        fd = self._file_buffers.get(fh)
        if fd is None:
            raise FuseOSError(errno.EBADF)
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, length)

    def write(self, path, buf, offset, fh):
        fd = self._file_buffers.get(fh)
        if fd is None:
            raise FuseOSError(errno.EBADF)
        os.lseek(fd, offset, os.SEEK_SET)
        return os.write(fd, buf)

    def truncate(self, path, length, fh=None):
        full = self._full_path(path)
        with open(full, 'r+') as f:
            f.truncate(length)

    def flush(self, path, fh):
        fd = self._file_buffers.get(fh)
        if fd is None:
            return
        return os.fsync(fd)

    def release(self, path, fh):
        """Called when file is closed. This is where we diff for shadows."""
        fd = self._file_buffers.pop(fh, None)
        rel = self._file_paths.pop(fh, None)
        agent = self._file_agents.pop(fh, "unknown")

        if fd is not None:
            os.close(fd)

        # Check if this was a watched file and process the shadow
        if rel and self.shadow.is_watched(rel):
            full = self._full_path("/" + rel)
            try:
                with open(full, 'r') as f:
                    new_content = f.read()
                result = self.shadow.process_write(rel, new_content, agent_id=agent)
                if result["type"] != "no_change":
                    prefix = "VIOLATION " if result.get("violation") else ""
                    print(f"[APFS] {prefix}{result['type']} on {rel} by {agent}: "
                          f"+{result.get('additions', 0)}/-{result.get('deletions', 0)}")
                    if result.get("violation"):
                        print(f"[APFS] *** {result['violation_reason']} ***")
            except Exception as e:
                print(f"[APFS] shadow error on {rel}: {e}")

    def fsync(self, path, fdatasync, fh):
        fd = self._file_buffers.get(fh)
        if fd is None:
            return
        return os.fsync(fd)


def main():
    parser = argparse.ArgumentParser(description='APFS — Agentic Playground File System')
    parser.add_argument('backing_dir', help='Directory to pass through to')
    parser.add_argument('mount_point', help='Where to mount the FUSE filesystem')
    parser.add_argument('--shadow-dir', default='/tmp/apfs-shadows',
                        help='Where to store shadow files (default: /tmp/apfs-shadows)')
    parser.add_argument('--watch', nargs='+', default=[],
                        help='Files to watch (relative to backing_dir)')
    parser.add_argument('--policy', nargs='+', default=[],
                        metavar='FILE=POLICY',
                        help='Set policy per file: append_only, annotate_only, unrestricted '
                             '(e.g., notebook.md=append_only)')
    parser.add_argument('--session-map', nargs='+', default=[],
                        metavar='SESSION=AGENT',
                        help='Map tmux session names to agent IDs (e.g., sixel-comms-email=comms)')
    parser.add_argument('--foreground', '-f', action='store_true', default=True,
                        help='Run in foreground (default)')
    args = parser.parse_args()

    # Validate paths
    backing = os.path.realpath(args.backing_dir)
    mount = os.path.realpath(args.mount_point)

    if not os.path.isdir(backing):
        print(f"Error: backing_dir '{backing}' does not exist")
        sys.exit(1)
    if not os.path.isdir(mount):
        print(f"Error: mount_point '{mount}' does not exist")
        sys.exit(1)

    # Initialize agent identifier
    session_map = {}
    for mapping in args.session_map:
        if '=' in mapping:
            session, agent = mapping.split('=', 1)
            session_map[session] = agent
    agent_identifier = AgentIdentifier(session_map if session_map else None)

    # Parse file policies
    file_policies = {}
    valid_policies = {FilePolicy.APPEND_ONLY, FilePolicy.ANNOTATE_ONLY, FilePolicy.UNRESTRICTED}
    for mapping in args.policy:
        if '=' in mapping:
            filename, policy = mapping.split('=', 1)
            if policy in valid_policies:
                file_policies[filename] = policy
            else:
                print(f"Warning: unknown policy '{policy}' for {filename}, using unrestricted")

    # Initialize shadow engine
    shadow = ShadowEngine(args.shadow_dir, args.watch, file_policies)

    # Snapshot any existing watched files
    for watch_file in args.watch:
        full = os.path.join(backing, watch_file)
        policy = file_policies.get(watch_file, "unrestricted")
        if os.path.exists(full):
            with open(full, 'r') as f:
                shadow.snapshot(watch_file, f.read())
            print(f"[APFS] Watching: {watch_file} [{policy}] (snapshotted)")
        else:
            print(f"[APFS] Watching: {watch_file} [{policy}] (will snapshot on create)")

    print(f"[APFS] Backing: {backing}")
    print(f"[APFS] Mount:   {mount}")
    print(f"[APFS] Shadows: {args.shadow_dir}")
    print(f"[APFS] Starting FUSE filesystem...")

    FUSE(APFS(backing, shadow, agent_identifier), mount, nothreads=False,
         foreground=args.foreground, allow_other=False)


if __name__ == '__main__':
    main()
