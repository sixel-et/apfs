#!/usr/bin/env python3
"""APFS Adversarial Tests — Moves, Deletes, Spam, Weird I/O

Tests designed to break things. Run after the integration suite passes.

Usage:
    python3 test_adversarial.py <mount_point> <shadow_dir> <backing_dir>
"""

import os
import sys
import time
import random
import string
import threading
import hashlib
from pathlib import Path


class TestRunner:
    def __init__(self, mount, shadow_dir, backing_dir):
        self.mount = mount
        self.shadow_dir = shadow_dir
        self.backing_dir = backing_dir
        self.passed = 0
        self.failed = 0
        self.errors = []

    def assert_eq(self, actual, expected, msg=""):
        if actual != expected:
            raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")

    def assert_true(self, val, msg=""):
        if not val:
            raise AssertionError(f"{msg}: expected True")

    def assert_in(self, needle, haystack, msg=""):
        if needle not in haystack:
            raise AssertionError(f"{msg}: {needle!r} not found ({len(haystack)} chars)")

    def assert_not_in(self, needle, haystack, msg=""):
        if needle in haystack:
            raise AssertionError(f"{msg}: {needle!r} unexpectedly found")

    def mount_path(self, filename):
        return os.path.join(self.mount, filename)

    def backing_path(self, filename):
        return os.path.join(self.backing_dir, filename)

    def shadow_path(self, filename):
        return os.path.join(self.shadow_dir, f"{filename}.shadow.md")

    def journal(self):
        path = os.path.join(self.shadow_dir, "journal.log")
        return open(path).read() if os.path.exists(path) else ""

    def violations(self):
        path = os.path.join(self.shadow_dir, "violations.log")
        return open(path).read() if os.path.exists(path) else ""

    def run_test(self, name, func):
        try:
            func(self)
            self.passed += 1
            print(f"  PASS: {name}")
        except Exception as e:
            self.failed += 1
            self.errors.append((name, str(e)))
            print(f"  FAIL: {name} — {e}")

    def report(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"Results: {self.passed}/{total} passed, {self.failed} failed")
        if self.errors:
            print(f"\nFailures:")
            for name, err in self.errors:
                print(f"  {name}: {err}")
        print(f"{'='*60}")
        return self.failed == 0


def clean_state(t):
    import shutil
    # Clean through mount first
    try:
        for f in Path(t.mount).iterdir():
            try:
                if f.is_file() or f.is_symlink():
                    f.unlink()
                elif f.is_dir() and f.name not in ('.', '..'):
                    shutil.rmtree(f)
            except Exception:
                pass
    except Exception:
        pass
    # Clean backing dir directly (bypass FUSE)
    for f in Path(t.backing_dir).iterdir():
        try:
            if f.is_file() or f.is_symlink():
                f.unlink()
            elif f.is_dir():
                shutil.rmtree(f)
        except Exception:
            pass
    # Clean shadows
    for f in Path(t.shadow_dir).glob("*"):
        try:
            f.unlink()
        except Exception:
            pass
    time.sleep(0.5)
    # Verify the backing dir is empty — if not, force it
    remaining = list(Path(t.backing_dir).iterdir())
    if remaining:
        for f in remaining:
            try:
                if f.is_file():
                    f.unlink()
            except Exception:
                pass
        time.sleep(0.3)


# ============================================================
# RENAMES & MOVES
# ============================================================

def test_rename_watched_file(t):
    """Rename a watched file — shadow should still exist under original name."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Before Rename\n\nContent.\n")
    time.sleep(0.3)

    os.rename(t.mount_path("notebook.md"), t.mount_path("notebook-old.md"))
    time.sleep(0.3)

    # Shadow should exist for original name
    t.assert_true(os.path.exists(t.shadow_path("notebook.md")),
                  "shadow exists under original name")
    # Renamed file should be readable
    with open(t.mount_path("notebook-old.md"), 'r') as f:
        got = f.read()
    t.assert_in("Before Rename", got, "content preserved after rename")


def test_rename_over_watched_file(t):
    """Rename another file OVER the watched file (atomic replace)."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Original\n\nThis will be replaced.\n")
    time.sleep(0.3)

    # Create a replacement file
    with open(t.mount_path("replacement.md"), 'w') as f:
        f.write("# Replacement\n\nDifferent content.\n")
    time.sleep(0.2)

    # Atomic replace: rename replacement over notebook
    os.rename(t.mount_path("replacement.md"), t.mount_path("notebook.md"))
    time.sleep(0.3)

    # Read through mount — should be the replacement
    with open(t.mount_path("notebook.md"), 'r') as f:
        got = f.read()
    t.assert_in("Replacement", got, "replacement content visible")


def test_rename_unwatched_to_watched_name(t):
    """Create a file with a non-watched name, rename it to the watched name."""
    with open(t.mount_path("temp.txt"), 'w') as f:
        f.write("# Surprise Notebook\n\nCreated as temp.\n")
    time.sleep(0.2)

    os.rename(t.mount_path("temp.txt"), t.mount_path("notebook.md"))
    time.sleep(0.3)

    # File should be readable
    with open(t.mount_path("notebook.md"), 'r') as f:
        got = f.read()
    t.assert_in("Surprise Notebook", got, "renamed file readable")


def test_move_to_subdirectory(t):
    """Move a watched file into a subdirectory."""
    # Create via backing dir first to ensure clean state
    with open(t.backing_path("notebook.md"), 'w') as f:
        f.write("# Moving\n\nAbout to be moved.\n")
    time.sleep(0.5)

    os.makedirs(t.mount_path("archive"), exist_ok=True)
    os.rename(t.mount_path("notebook.md"), os.path.join(t.mount, "archive", "notebook.md"))
    time.sleep(0.3)

    # Moved file readable
    with open(os.path.join(t.mount, "archive", "notebook.md"), 'r') as f:
        got = f.read()
    t.assert_in("About to be moved", got, "moved file readable")
    # Original location empty
    t.assert_true(not os.path.exists(t.mount_path("notebook.md")), "original gone")

    # Clean up
    os.unlink(os.path.join(t.mount, "archive", "notebook.md"))
    os.rmdir(t.mount_path("archive"))


def test_rapid_rename_cycle(t):
    """Rename a file back and forth 20 times rapidly."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Ping Pong\n")
    time.sleep(0.2)

    for i in range(20):
        os.rename(t.mount_path("notebook.md"), t.mount_path("temp.md"))
        os.rename(t.mount_path("temp.md"), t.mount_path("notebook.md"))
        time.sleep(0.02)

    time.sleep(0.3)
    with open(t.mount_path("notebook.md"), 'r') as f:
        got = f.read()
    t.assert_in("Ping Pong", got, "content survived rename cycle")


def test_rename_tracked_in_journal(t):
    """Rename of watched file appears in journal."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Tracked Rename\n")
    time.sleep(0.3)

    os.rename(t.mount_path("notebook.md"), t.mount_path("old-notebook.md"))
    time.sleep(0.3)

    journal = t.journal()
    t.assert_in("rename", journal, "rename in journal")
    t.assert_in("notebook.md", journal, "old name in journal")
    t.assert_in("old-notebook.md", journal, "new name in journal")

    # Shadow should note the rename
    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("renamed to", shadow, "rename noted in shadow")

    os.unlink(t.mount_path("old-notebook.md"))


def test_rename_to_watched_name_tracked(t):
    """Renaming a file TO the watched name is captured in shadow."""
    with open(t.mount_path("draft.md"), 'w') as f:
        f.write("# Draft\n\nBecomes the notebook.\n")
    time.sleep(0.2)

    os.rename(t.mount_path("draft.md"), t.mount_path("notebook.md"))
    time.sleep(0.3)

    journal = t.journal()
    t.assert_in("rename", journal, "rename in journal")

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("renamed from", shadow, "arrival noted in shadow")

    # Now modify through mount — should be tracked from this point
    with open(t.mount_path("notebook.md"), 'a') as f:
        f.write("\n## Post-rename entry\n")
    time.sleep(0.3)

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("Post-rename entry", shadow, "post-rename write tracked")


def test_rename_over_watched_destroys_content(t):
    """Rename another file OVER notebook.md — old content captured as deleted."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Original\n\nThis will be destroyed by rename.\n")
    time.sleep(0.3)

    with open(t.mount_path("replacement.md"), 'w') as f:
        f.write("# Replacement\n\nI replaced the notebook.\n")
    time.sleep(0.2)

    os.rename(t.mount_path("replacement.md"), t.mount_path("notebook.md"))
    time.sleep(0.3)

    journal = t.journal()
    t.assert_in("rename", journal, "rename-over in journal")

    shadow = open(t.shadow_path("notebook.md")).read()
    # The original content should be captured (struck through or noted)
    t.assert_in("Original", shadow, "original content preserved in shadow")
    t.assert_in("Replacement", shadow, "replacement noted in shadow")


# ============================================================
# DELETE STRESS
# ============================================================

def test_delete_recreate_cycle(t):
    """Delete and recreate watched file 20 times."""
    for i in range(20):
        with open(t.mount_path("notebook.md"), 'w') as f:
            f.write(f"# Cycle {i}\n\nIteration {i}.\n")
        time.sleep(0.1)
        os.unlink(t.mount_path("notebook.md"))
        time.sleep(0.05)

    time.sleep(0.5)
    journal = t.journal()
    # Should have entries for creates and deletes
    t.assert_true(len(journal) > 0, "journal has entries from cycle")


def test_delete_while_reading(t):
    """Open file for reading, delete it, try to read."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Vanishing\n\nNow you see me...\n")
    time.sleep(0.2)

    fd = open(t.mount_path("notebook.md"), 'r')
    os.unlink(t.mount_path("notebook.md"))
    time.sleep(0.1)

    # On Linux, open fd survives unlink — should still be readable
    try:
        got = fd.read()
        t.assert_in("Vanishing", got, "content readable after unlink via open fd")
    finally:
        fd.close()


def test_delete_nonexistent(t):
    """Trying to delete a file that doesn't exist should raise."""
    raised = False
    try:
        os.unlink(t.mount_path("notebook.md"))
    except FileNotFoundError:
        raised = True
    t.assert_true(raised, "FileNotFoundError raised for missing file")


def test_rmdir_nonempty(t):
    """rmdir on non-empty directory should fail."""
    os.makedirs(t.mount_path("nonempty"), exist_ok=True)
    with open(os.path.join(t.mount, "nonempty", "file.txt"), 'w') as f:
        f.write("content\n")

    raised = False
    try:
        os.rmdir(t.mount_path("nonempty"))
    except OSError:
        raised = True
    t.assert_true(raised, "OSError for non-empty rmdir")

    # Clean up
    os.unlink(os.path.join(t.mount, "nonempty", "file.txt"))
    os.rmdir(t.mount_path("nonempty"))


# ============================================================
# SPAM / ABUSE
# ============================================================

def test_thousand_byte_writes(t):
    """1000 individual 1-byte appends to watched file."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Spam\n")
    time.sleep(0.2)

    for i in range(1000):
        with open(t.mount_path("notebook.md"), 'a') as f:
            f.write("X")
    time.sleep(1)

    with open(t.mount_path("notebook.md"), 'r') as f:
        got = f.read()
    # Should have header + 1000 X's
    t.assert_true(got.count("X") == 1000, f"expected 1000 X's, got {got.count('X')}")

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_true(len(shadow) > 0, "shadow has content")


def test_rapid_open_close_no_write(t):
    """Open and close watched file 500 times without writing."""
    with open(t.backing_path("notebook.md"), 'w') as f:
        f.write("# Untouched\n")
    time.sleep(0.3)

    for i in range(500):
        fd = open(t.mount_path("notebook.md"), 'r')
        fd.close()

    time.sleep(0.5)
    # File should be unchanged
    with open(t.mount_path("notebook.md"), 'r') as f:
        got = f.read()
    t.assert_eq(got, "# Untouched\n", "content unchanged after 500 open/close")


def test_twenty_concurrent_writers(t):
    """20 threads all appending to the same watched file."""
    with open(t.backing_path("notebook.md"), 'w') as f:
        f.write("# Crowd\n")
    time.sleep(0.3)

    errors = []

    def writer(n):
        try:
            for i in range(10):
                with open(t.mount_path("notebook.md"), 'a') as f:
                    f.write(f"\nW{n}-{i}")
                time.sleep(0.01)
        except Exception as e:
            errors.append(f"writer-{n}: {e}")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)

    time.sleep(1)
    t.assert_eq(len(errors), 0, f"writer errors: {errors}")

    with open(t.mount_path("notebook.md"), 'r') as f:
        got = f.read()

    # All 200 entries should be present (20 writers × 10 entries)
    for n in range(20):
        for i in range(10):
            t.assert_in(f"W{n}-{i}", got, f"writer {n} entry {i}")


def test_zero_byte_write(t):
    """Write zero bytes to watched file — should be no-op."""
    with open(t.backing_path("notebook.md"), 'w') as f:
        f.write("# Zero\n\nOriginal content.\n")
    time.sleep(0.3)

    with open(t.mount_path("notebook.md"), 'a') as f:
        f.write("")  # zero bytes
    time.sleep(0.3)

    with open(t.mount_path("notebook.md"), 'r') as f:
        got = f.read()
    t.assert_eq(got, "# Zero\n\nOriginal content.\n", "unchanged after zero write")


def test_massive_single_write(t):
    """Single 5MB write to watched file."""
    big = "A" * (5 * 1024 * 1024) + "\n"
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write(big)
    time.sleep(1)

    with open(t.mount_path("notebook.md"), 'r') as f:
        got = f.read()
    t.assert_eq(len(got), len(big), "5MB file written correctly")

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_true(len(shadow) > 0, "shadow exists for 5MB file")


def test_rapid_create_delete_different_files(t):
    """Create and delete 200 different files rapidly (not watched)."""
    for i in range(200):
        path = t.mount_path(f"spam_{i}.txt")
        with open(path, 'w') as f:
            f.write(f"spam {i}\n")
        os.unlink(path)

    time.sleep(0.5)
    remaining = [f for f in os.listdir(t.mount) if f.startswith("spam_")]
    t.assert_eq(len(remaining), 0, "all spam files deleted")


# ============================================================
# WEIRD I/O
# ============================================================

def test_symlink_to_watched(t):
    """Create symlink pointing to watched file — read through symlink."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Symlinked\n\nContent.\n")
    time.sleep(0.2)

    os.symlink(t.mount_path("notebook.md"), t.mount_path("link.md"))
    time.sleep(0.1)

    with open(t.mount_path("link.md"), 'r') as f:
        got = f.read()
    t.assert_in("Symlinked", got, "content readable through symlink")

    os.unlink(t.mount_path("link.md"))


def test_hardlink(t):
    """Create hard link to a file through the mount."""
    with open(t.mount_path("original.txt"), 'w') as f:
        f.write("Hard link test.\n")
    time.sleep(0.1)

    try:
        os.link(t.mount_path("original.txt"), t.mount_path("hardlink.txt"))
        with open(t.mount_path("hardlink.txt"), 'r') as f:
            got = f.read()
        t.assert_eq(got, "Hard link test.\n", "hardlink readable")
        os.unlink(t.mount_path("hardlink.txt"))
    except OSError as e:
        # Some FUSE configs don't support hard links — that's OK
        pass
    finally:
        if os.path.exists(t.mount_path("original.txt")):
            os.unlink(t.mount_path("original.txt"))


def test_seek_write(t):
    """Seek to middle of watched file and overwrite bytes."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("AAAAAAAAAA\n")  # 11 bytes
    time.sleep(0.3)

    with open(t.mount_path("notebook.md"), 'r+b') as f:
        f.seek(3)
        f.write(b"BBB")
    time.sleep(0.3)

    with open(t.mount_path("notebook.md"), 'r') as f:
        got = f.read()
    t.assert_eq(got, "AAABBBAAAA\n", "seek+write correct")


def test_chmod_watched(t):
    """chmod on watched file — should work, not break shadow."""
    with open(t.backing_path("notebook.md"), 'w') as f:
        f.write("# Perms\n")
    time.sleep(0.3)

    os.chmod(t.mount_path("notebook.md"), 0o444)
    time.sleep(0.1)

    # Should still be readable
    with open(t.mount_path("notebook.md"), 'r') as f:
        got = f.read()
    t.assert_in("Perms", got, "readable after chmod 444")

    # Restore write permission for cleanup
    os.chmod(t.mount_path("notebook.md"), 0o644)


def test_interleaved_binary_text(t):
    """Alternate between text and binary writes to non-watched file."""
    path = t.mount_path("mixed.dat")
    with open(path, 'wb') as f:
        for i in range(50):
            if i % 2 == 0:
                f.write(f"Text line {i}\n".encode())
            else:
                f.write(bytes(range(256)))
    time.sleep(0.3)

    with open(path, 'rb') as f:
        got = f.read()

    expected_size = sum(
        len(f"Text line {i}\n".encode()) if i % 2 == 0 else 256
        for i in range(50)
    )
    t.assert_eq(len(got), expected_size, "mixed binary/text size correct")
    os.unlink(path)


def test_many_nested_directories(t):
    """Create 10-level deep directory tree through mount."""
    path = t.mount
    for i in range(10):
        path = os.path.join(path, f"level_{i}")
        os.makedirs(path, exist_ok=True)

    # Write a file at the bottom
    deep_file = os.path.join(path, "deep.txt")
    with open(deep_file, 'w') as f:
        f.write("I'm 10 levels deep.\n")

    with open(deep_file, 'r') as f:
        got = f.read()
    t.assert_eq(got, "I'm 10 levels deep.\n", "deep file readable")

    # Clean up (reverse order)
    os.unlink(deep_file)
    path = t.mount
    dirs = []
    for i in range(10):
        path = os.path.join(path, f"level_{i}")
        dirs.append(path)
    for d in reversed(dirs):
        os.rmdir(d)


def test_write_then_read_consistency(t):
    """Write through mount, read through backing — must match."""
    content = "Consistency check: " + "Z" * 50000 + "\n"
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write(content)
    time.sleep(0.3)

    # Read from backing dir directly (bypass FUSE)
    with open(t.backing_path("notebook.md"), 'r') as f:
        backing = f.read()
    # Read from mount
    with open(t.mount_path("notebook.md"), 'r') as f:
        mounted = f.read()

    t.assert_eq(mounted, content, "mount read matches written")
    t.assert_eq(backing, content, "backing read matches written")
    t.assert_eq(mounted, backing, "mount and backing match each other")


def test_sparse_writes(t):
    """Write at offset 0, then at offset 10000 — gap should be null bytes."""
    path = t.mount_path("sparse.dat")
    with open(path, 'wb') as f:
        f.write(b"START")
        f.seek(10000)
        f.write(b"END")
    time.sleep(0.2)

    with open(path, 'rb') as f:
        got = f.read()

    t.assert_eq(got[:5], b"START", "start correct")
    t.assert_eq(got[10000:10003], b"END", "end at offset 10000")
    t.assert_eq(got[5:10000], b'\x00' * 9995, "gap is null bytes")
    os.unlink(path)


def test_file_descriptor_leak(t):
    """Open 500 files through mount without closing — then close all."""
    fds = []
    for i in range(500):
        path = t.mount_path(f"leak_{i}.txt")
        fd = open(path, 'w')
        fd.write(f"leak {i}\n")
        fds.append((fd, path))

    # Close all
    for fd, path in fds:
        fd.close()

    time.sleep(1)

    # Verify some files are correct
    for i in range(0, 500, 50):
        path = t.mount_path(f"leak_{i}.txt")
        with open(path, 'r') as f:
            got = f.read()
        t.assert_eq(got, f"leak {i}\n", f"leak file {i}")

    # Clean up
    for i in range(500):
        os.unlink(t.mount_path(f"leak_{i}.txt"))


def test_concurrent_read_write(t):
    """One thread writes continuously, another reads continuously."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# ConcurrentRW\n")
    time.sleep(0.2)

    stop = threading.Event()
    write_errors = []
    read_errors = []

    def writer():
        try:
            for i in range(100):
                if stop.is_set():
                    break
                with open(t.mount_path("notebook.md"), 'a') as f:
                    f.write(f"\nLine {i}: {'data' * 20}\n")
                time.sleep(0.02)
        except Exception as e:
            write_errors.append(str(e))

    def reader():
        try:
            for i in range(200):
                if stop.is_set():
                    break
                try:
                    with open(t.mount_path("notebook.md"), 'r') as f:
                        data = f.read()
                    # Should always start with the header
                    if not data.startswith("# ConcurrentRW"):
                        read_errors.append(f"iter {i}: header missing")
                except FileNotFoundError:
                    pass  # file might be momentarily gone during clean_state
                time.sleep(0.01)
        except Exception as e:
            read_errors.append(str(e))

    tw = threading.Thread(target=writer)
    tr = threading.Thread(target=reader)
    tw.start()
    tr.start()
    tw.join(timeout=30)
    stop.set()
    tr.join(timeout=10)

    t.assert_eq(len(write_errors), 0, f"write errors: {write_errors}")
    t.assert_eq(len(read_errors), 0, f"read errors: {read_errors}")


# ============================================================
# SHADOW CORRUPTION CHECKS
# ============================================================

def test_shadow_valid_utf8(t):
    """Shadow file should always be valid UTF-8."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# UTF8\n\nLine 1.\n")
    time.sleep(0.2)

    for i in range(10):
        with open(t.mount_path("notebook.md"), 'w') as f:
            f.write(f"# UTF8\n\nRewrite {i}: {'x' * 100}\n")
        time.sleep(0.1)

    time.sleep(0.5)
    shadow_file = t.shadow_path("notebook.md")
    with open(shadow_file, 'rb') as f:
        raw = f.read()

    try:
        raw.decode('utf-8')
    except UnicodeDecodeError as e:
        raise AssertionError(f"shadow not valid UTF-8: {e}")


def test_shadow_no_partial_markers(t):
    """Every APFS marker in shadow should be complete (not truncated)."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Markers\n")
    time.sleep(0.2)

    for i in range(20):
        with open(t.mount_path("notebook.md"), 'a') as f:
            f.write(f"\nEntry {i}\n")
        time.sleep(0.05)

    time.sleep(0.5)
    shadow = open(t.shadow_path("notebook.md")).read()

    opens = shadow.count("<!-- APFS:")
    closes = shadow.count("-->")
    # Every open marker should have a close (may have extra --> from "replaced with")
    t.assert_true(closes >= opens,
                  f"marker mismatch: {opens} opens, {closes} closes")


def test_journal_line_integrity(t):
    """Every journal line should be complete (timestamp + operation)."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Journal\n")
    time.sleep(0.2)

    for i in range(30):
        op = random.choice(["append", "overwrite"])
        if op == "append":
            with open(t.mount_path("notebook.md"), 'a') as f:
                f.write(f"\nE{i}\n")
        else:
            with open(t.mount_path("notebook.md"), 'w') as f:
                f.write(f"# Journal\n\nRewrite {i}\n")
        time.sleep(0.05)

    time.sleep(0.5)
    journal = t.journal()
    for line in journal.strip().split('\n'):
        if not line.strip():
            continue
        t.assert_true(line.startswith("["), f"journal line missing timestamp: {line[:60]}")
        t.assert_in("notebook.md", line, f"journal line missing filename: {line[:60]}")


# ============================================================
# Main
# ============================================================

def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <mount_point> <shadow_dir> <backing_dir>")
        sys.exit(1)

    mount, shadow_dir, backing_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    t = TestRunner(mount, shadow_dir, backing_dir)

    tiers = [
        ("RENAMES & MOVES", [
            ("rename_watched_file", test_rename_watched_file),
            ("rename_over_watched_file", test_rename_over_watched_file),
            ("rename_unwatched_to_watched_name", test_rename_unwatched_to_watched_name),
            ("move_to_subdirectory", test_move_to_subdirectory),
            ("rapid_rename_cycle", test_rapid_rename_cycle),
            ("rename_tracked_in_journal", test_rename_tracked_in_journal),
            ("rename_to_watched_name_tracked", test_rename_to_watched_name_tracked),
            ("rename_over_watched_destroys_content", test_rename_over_watched_destroys_content),
        ]),
        ("DELETE STRESS", [
            ("delete_recreate_cycle", test_delete_recreate_cycle),
            ("delete_while_reading", test_delete_while_reading),
            ("delete_nonexistent", test_delete_nonexistent),
            ("rmdir_nonempty", test_rmdir_nonempty),
        ]),
        ("SPAM & ABUSE", [
            ("thousand_byte_writes", test_thousand_byte_writes),
            ("rapid_open_close_no_write", test_rapid_open_close_no_write),
            ("twenty_concurrent_writers", test_twenty_concurrent_writers),
            ("zero_byte_write", test_zero_byte_write),
            ("massive_single_write", test_massive_single_write),
            ("rapid_create_delete_different_files", test_rapid_create_delete_different_files),
        ]),
        ("WEIRD I/O", [
            ("symlink_to_watched", test_symlink_to_watched),
            ("hardlink", test_hardlink),
            ("seek_write", test_seek_write),
            ("chmod_watched", test_chmod_watched),
            ("interleaved_binary_text", test_interleaved_binary_text),
            ("many_nested_directories", test_many_nested_directories),
            ("write_then_read_consistency", test_write_then_read_consistency),
            ("sparse_writes", test_sparse_writes),
            ("file_descriptor_leak", test_file_descriptor_leak),
            ("concurrent_read_write", test_concurrent_read_write),
        ]),
        ("SHADOW CORRUPTION CHECKS", [
            ("shadow_valid_utf8", test_shadow_valid_utf8),
            ("shadow_no_partial_markers", test_shadow_no_partial_markers),
            ("journal_line_integrity", test_journal_line_integrity),
        ]),
    ]

    start = time.time()
    for tier_name, tests in tiers:
        print(f"\n{tier_name}")
        print("-" * len(tier_name))
        for name, func in tests:
            clean_state(t)
            t.run_test(name, func)

    elapsed = time.time() - start
    print(f"\nCompleted in {elapsed:.1f}s")
    ok = t.report()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
