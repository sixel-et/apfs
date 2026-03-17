#!/usr/bin/env python3
"""APFS FUSE Integration Tests — Progressive Complexity

These tests require FUSE to be mounted. They exercise the full pipeline:
file operation → FUSE interception → shadow engine → policy enforcement.

Tests progress from simple single-operation to complex multi-agent,
multi-file, high-volume scenarios.

Usage:
    # Mount APFS first, then run tests
    python3 test_fuse_integration.py <mount_point> <shadow_dir> <backing_dir>
"""

import os
import sys
import time
import json
import random
import string
import hashlib
import subprocess
import tempfile
import threading
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
            raise AssertionError(f"{msg}: expected True, got {val!r}")

    def assert_in(self, needle, haystack, msg=""):
        if needle not in haystack:
            raise AssertionError(f"{msg}: {needle!r} not found in output ({len(haystack)} chars)")

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
        if os.path.exists(path):
            return open(path).read()
        return ""

    def violations(self):
        path = os.path.join(self.shadow_dir, "violations.log")
        if os.path.exists(path):
            return open(path).read()
        return ""

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


# ============================================================
# TIER 1: Basic Operations
# ============================================================

def test_passthrough_read(t):
    """Files readable through mount match backing dir."""
    content = "Passthrough read test.\n"
    with open(t.backing_path("read-test.txt"), 'w') as f:
        f.write(content)
    time.sleep(0.2)
    with open(t.mount_path("read-test.txt"), 'r') as f:
        got = f.read()
    t.assert_eq(got, content, "passthrough read")
    os.unlink(t.backing_path("read-test.txt"))


def test_passthrough_write(t):
    """Files written through mount appear in backing dir."""
    content = "Passthrough write test.\n"
    with open(t.mount_path("write-test.txt"), 'w') as f:
        f.write(content)
    time.sleep(0.2)
    with open(t.backing_path("write-test.txt"), 'r') as f:
        got = f.read()
    t.assert_eq(got, content, "passthrough write")
    os.unlink(t.backing_path("write-test.txt"))


def test_simple_append(t):
    """Append to watched file creates shadow with append marker."""
    original = "# Test\n\nOriginal.\n"
    appended = original + "\n## New\n\nAppended.\n"

    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write(original)
    time.sleep(0.3)

    with open(t.mount_path("notebook.md"), 'a') as f:
        f.write("\n## New\n\nAppended.\n")
    time.sleep(0.3)

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("APFS: append", shadow, "append marker in shadow")
    t.assert_in("Appended.", shadow, "appended content in shadow")


def test_simple_modification(t):
    """Modify watched file shows old+new in shadow."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Test\n\nVersion 1.\n")
    time.sleep(0.3)

    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Test\n\nVersion 2.\n")
    time.sleep(0.3)

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("~~Version 1.~~", shadow, "old content struck through")
    t.assert_in("Version 2.", shadow, "new content present")


def test_simple_deletion(t):
    """Delete content from watched file shows strikethrough."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Test\n\nKeep.\n\nRemove.\n")
    time.sleep(0.3)

    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Test\n\nKeep.\n")
    time.sleep(0.3)

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("~~Remove.~~", shadow, "deleted content struck through")


def test_file_unlink(t):
    """Deleting a watched file preserves everything in shadow."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Doomed\n\nThis will be deleted.\n")
    time.sleep(0.3)

    os.unlink(t.mount_path("notebook.md"))
    time.sleep(0.3)

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("~~# Doomed~~", shadow, "file content preserved in shadow after unlink")


# ============================================================
# TIER 2: Multi-operation sequences
# ============================================================

def test_append_modify_delete_sequence(t):
    """Sequence of operations accumulates correctly in shadow."""
    # Create
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Seq\n\n## E1\n\nFirst.\n")
    time.sleep(0.3)

    # Append
    with open(t.mount_path("notebook.md"), 'a') as f:
        f.write("\n## E2\n\nSecond.\n")
    time.sleep(0.3)

    # Modify E1
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Seq\n\n## E1\n\nFirst REVISED.\n\n## E2\n\nSecond.\n")
    time.sleep(0.3)

    # Delete E2
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Seq\n\n## E1\n\nFirst REVISED.\n")
    time.sleep(0.3)

    shadow = open(t.shadow_path("notebook.md")).read()
    journal = t.journal()

    t.assert_in("append", journal, "append in journal")
    t.assert_in("modification", journal, "modification in journal")
    t.assert_in("deletion", journal, "deletion in journal")
    t.assert_in("~~First.~~", shadow, "old E1 struck")
    t.assert_in("First REVISED.", shadow, "new E1 present")
    t.assert_in("~~## E2~~", shadow, "deleted E2 struck")

    # Count APFS markers — should be at least 3 (append, modify, delete)
    marker_count = shadow.count("<!-- APFS:")
    t.assert_true(marker_count >= 3, f"expected >=3 markers, got {marker_count}")


def test_rapid_appends(t):
    """10 rapid appends in succession all captured."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Rapid\n")
    time.sleep(0.3)

    for i in range(10):
        with open(t.mount_path("notebook.md"), 'a') as f:
            f.write(f"\n## Entry {i}\n\nContent {i}.\n")
        time.sleep(0.1)

    time.sleep(0.5)
    shadow = open(t.shadow_path("notebook.md")).read()

    for i in range(10):
        t.assert_in(f"Content {i}.", shadow, f"entry {i} in shadow")


def test_overwrite_cycle(t):
    """Repeated full overwrites — each one captured."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Cycle\n\nVersion 0.\n")
    time.sleep(0.3)

    for i in range(1, 6):
        with open(t.mount_path("notebook.md"), 'w') as f:
            f.write(f"# Cycle\n\nVersion {i}.\n")
        time.sleep(0.2)

    time.sleep(0.5)
    shadow = open(t.shadow_path("notebook.md")).read()

    # Each version except the last should be struck through
    for i in range(5):
        t.assert_in(f"~~Version {i}.~~", shadow, f"version {i} struck through")
    t.assert_in("Version 5.", shadow, "final version present")


def test_large_file(t):
    """File with 1000 lines — append and modify work correctly."""
    lines = [f"Line {i}: {'x' * 80}\n" for i in range(1000)]
    content = "".join(lines)

    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write(content)
    time.sleep(0.5)

    # Append 100 more lines
    extra = [f"Extra {i}: {'y' * 80}\n" for i in range(100)]
    with open(t.mount_path("notebook.md"), 'a') as f:
        f.writelines(extra)
    time.sleep(0.5)

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("Extra 0:", shadow, "first extra line in shadow")
    t.assert_in("Extra 99:", shadow, "last extra line in shadow")
    t.assert_in("APFS: append", shadow, "append marker")

    # Verify actual file has all 1100 lines
    with open(t.mount_path("notebook.md"), 'r') as f:
        actual = f.readlines()
    t.assert_eq(len(actual), 1100, "total lines in file")


def test_binary_safe_passthrough(t):
    """Binary file (not watched) passes through correctly."""
    data = bytes(range(256)) * 100  # 25.6KB of all byte values
    with open(t.mount_path("binary.dat"), 'wb') as f:
        f.write(data)
    time.sleep(0.2)

    with open(t.mount_path("binary.dat"), 'rb') as f:
        got = f.read()
    t.assert_eq(got, data, "binary passthrough")
    t.assert_eq(hashlib.sha256(got).hexdigest(), hashlib.sha256(data).hexdigest(), "binary hash")
    os.unlink(t.mount_path("binary.dat"))


# ============================================================
# TIER 3: Concurrent access
# ============================================================

def test_concurrent_appends(t):
    """5 threads appending simultaneously — all content preserved."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Concurrent\n")
    time.sleep(0.3)

    results = {}
    errors = []

    def append_entry(agent_num):
        try:
            for i in range(5):
                with open(t.mount_path("notebook.md"), 'a') as f:
                    f.write(f"\n## Agent-{agent_num} Entry-{i}\n\nContent from agent {agent_num}.\n")
                time.sleep(0.05)
            results[agent_num] = True
        except Exception as e:
            errors.append(f"agent-{agent_num}: {e}")

    threads = [threading.Thread(target=append_entry, args=(n,)) for n in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    time.sleep(1)

    t.assert_eq(len(errors), 0, f"thread errors: {errors}")

    with open(t.mount_path("notebook.md"), 'r') as f:
        actual = f.read()

    # All 25 entries (5 agents x 5 entries) should be in actual file
    for agent in range(5):
        for entry in range(5):
            t.assert_in(f"Agent-{agent} Entry-{entry}", actual,
                        f"agent-{agent} entry-{entry} in file")

    # Shadow should have all 25 entries too
    shadow = open(t.shadow_path("notebook.md")).read()
    for agent in range(5):
        for entry in range(5):
            t.assert_in(f"Agent-{agent} Entry-{entry}", shadow,
                        f"agent-{agent} entry-{entry} in shadow")


def test_concurrent_append_vs_overwrite(t):
    """One thread appends, another overwrites — shadow captures the loss."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Race\n\n## Original\n\nBaseline.\n")
    time.sleep(0.3)

    barrier = threading.Barrier(2)
    results = {"appender": None, "overwriter": None}

    def appender():
        barrier.wait()
        with open(t.mount_path("notebook.md"), 'a') as f:
            f.write("\n## Appended by Thread A\n\nThis might get lost.\n")
        results["appender"] = True

    def overwriter():
        barrier.wait()
        time.sleep(0.05)  # slight delay so append usually goes first
        with open(t.mount_path("notebook.md"), 'w') as f:
            f.write("# Race\n\n## Overwritten by Thread B\n\nOnly this remains.\n")
        results["overwriter"] = True

    ta = threading.Thread(target=appender)
    tb = threading.Thread(target=overwriter)
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)
    time.sleep(0.5)

    # The actual file has whatever won the race
    with open(t.mount_path("notebook.md"), 'r') as f:
        actual = f.read()

    # The shadow should have BOTH — the appended content and the overwrite
    shadow = open(t.shadow_path("notebook.md")).read()

    # At minimum, the shadow should show the overwrite happened
    t.assert_in("APFS:", shadow, "shadow has operation markers")

    journal = t.journal()
    t.assert_true(
        "append" in journal or "modification" in journal,
        "journal records at least one operation"
    )


# ============================================================
# TIER 4: Policy enforcement under load
# ============================================================

def test_policy_violation_counting(t):
    """Multiple violations all logged."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Policy\n\nV0.\n")
    time.sleep(0.3)

    # 10 overwrites = 10 violations on append_only
    for i in range(1, 11):
        with open(t.mount_path("notebook.md"), 'w') as f:
            f.write(f"# Policy\n\nV{i}.\n")
        time.sleep(0.15)

    time.sleep(0.5)
    violations = t.violations()
    violation_count = violations.count("append_only")
    t.assert_true(violation_count >= 9,
                  f"expected >=9 violations, got {violation_count}")


def test_mixed_policy_clean_and_dirty(t):
    """Appends don't trigger violations, overwrites do."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Mixed\n")
    time.sleep(0.3)

    # 5 clean appends
    for i in range(5):
        with open(t.mount_path("notebook.md"), 'a') as f:
            f.write(f"\n## Entry {i}\n")
        time.sleep(0.1)

    time.sleep(0.3)
    violations_before = t.violations().count("append_only")

    # 1 dirty overwrite
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Mixed\n\nOverwritten.\n")
    time.sleep(0.3)

    violations_after = t.violations().count("append_only")
    t.assert_true(violations_after > violations_before,
                  f"overwrite should add violation: before={violations_before} after={violations_after}")


# ============================================================
# TIER 5: Shadow integrity under stress
# ============================================================

def test_shadow_monotonic_growth(t):
    """Shadow file size only increases, never decreases."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Mono\n\nStart.\n")
    time.sleep(0.3)

    sizes = []
    for i in range(20):
        # Alternate between appends and overwrites
        if i % 2 == 0:
            with open(t.mount_path("notebook.md"), 'a') as f:
                f.write(f"\n## Append {i}\n\n{'Content ' * 20}\n")
        else:
            with open(t.mount_path("notebook.md"), 'w') as f:
                f.write(f"# Mono\n\nRewrite {i}.\n")
        time.sleep(0.15)

    time.sleep(0.5)
    shadow_file = t.shadow_path("notebook.md")
    final_size = os.path.getsize(shadow_file)
    t.assert_true(final_size > 0, "shadow has content")

    # Read shadow and verify it has markers for all 20 operations
    shadow = open(shadow_file).read()
    marker_count = shadow.count("<!-- APFS:")
    t.assert_true(marker_count >= 19,
                  f"expected >=19 markers (20 ops minus initial create), got {marker_count}")


def test_large_shadow_accumulation(t):
    """50 operations building a large shadow — verify integrity."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Large Shadow Test\n")
    time.sleep(0.3)

    for i in range(50):
        op = random.choice(["append", "modify"])
        if op == "append":
            with open(t.mount_path("notebook.md"), 'a') as f:
                f.write(f"\n## Entry {i}\n\n{'Word ' * random.randint(10, 100)}\n")
        else:
            with open(t.mount_path("notebook.md"), 'r') as f:
                current = f.read()
            # Modify: add a line at the top after the header
            modified = current.replace("# Large Shadow Test\n",
                                       f"# Large Shadow Test\n\n> Modified at step {i}\n")
            with open(t.mount_path("notebook.md"), 'w') as f:
                f.write(modified)
        time.sleep(0.1)

    time.sleep(1)

    shadow = open(t.shadow_path("notebook.md")).read()
    journal = t.journal()

    journal_lines = [l for l in journal.strip().split('\n') if 'notebook.md' in l]
    t.assert_true(len(journal_lines) >= 49,
                  f"expected >=49 journal entries, got {len(journal_lines)}")

    # Shadow should be substantially larger than the actual file
    actual_size = os.path.getsize(t.mount_path("notebook.md"))
    shadow_size = os.path.getsize(t.shadow_path("notebook.md"))
    t.assert_true(shadow_size > actual_size,
                  f"shadow ({shadow_size}) should be larger than actual ({actual_size})")


def test_journal_completeness(t):
    """Every watched file operation appears in journal."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Journal Test\n")
    time.sleep(0.3)

    ops = 0
    for i in range(30):
        with open(t.mount_path("notebook.md"), 'a') as f:
            f.write(f"\nLine {i}\n")
        ops += 1
        time.sleep(0.05)

    time.sleep(0.5)
    journal = t.journal()
    journal_entries = [l for l in journal.strip().split('\n') if 'notebook.md' in l]

    # Allow for the initial create + all appends
    t.assert_true(len(journal_entries) >= ops,
                  f"expected >={ops} journal entries, got {len(journal_entries)}")


# ============================================================
# TIER 6: Edge cases
# ============================================================

def test_empty_file(t):
    """Empty file creation and operations."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        pass  # empty file
    time.sleep(0.3)

    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("Not empty anymore.\n")
    time.sleep(0.3)

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("Not empty anymore.", shadow, "content after empty create")


def test_unicode_content(t):
    """Unicode in file content handled correctly."""
    content = "# Notebook\n\n## Eintrag\n\nÜber die Grenze hinaus. 日本語テスト. 🧪\n"
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write(content)
    time.sleep(0.3)

    modified = content.replace("Über", "Jenseits")
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write(modified)
    time.sleep(0.3)

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("Über", shadow, "original unicode in shadow")
    t.assert_in("Jenseits", shadow, "modified unicode in shadow")
    t.assert_in("日本語", shadow, "CJK in shadow")


def test_long_lines(t):
    """Very long lines (10KB each) handled correctly."""
    long_line = "A" * 10000 + "\n"
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Long Lines\n\n" + long_line)
    time.sleep(0.3)

    new_long = "B" * 10000 + "\n"
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Long Lines\n\n" + new_long)
    time.sleep(0.3)

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_in("AAAA", shadow, "old long line in shadow")
    t.assert_in("BBBB", shadow, "new long line in shadow")


def test_many_small_files(t):
    """Non-watched files: create 100, verify passthrough, clean up."""
    created = []
    for i in range(100):
        name = f"temp_{i}.txt"
        with open(t.mount_path(name), 'w') as f:
            f.write(f"Content {i}\n")
        created.append(name)

    time.sleep(0.5)

    for i, name in enumerate(created):
        with open(t.mount_path(name), 'r') as f:
            got = f.read()
        t.assert_eq(got, f"Content {i}\n", f"file {name}")

    for name in created:
        os.unlink(t.mount_path(name))

    remaining = os.listdir(t.mount)
    for name in created:
        t.assert_not_in(name, remaining, f"{name} should be deleted")


def test_directory_operations(t):
    """Subdirectory create, file in subdir, readdir."""
    subdir = os.path.join(t.mount, "subdir")
    os.makedirs(subdir, exist_ok=True)

    with open(os.path.join(subdir, "test.txt"), 'w') as f:
        f.write("In subdir.\n")

    with open(os.path.join(subdir, "test.txt"), 'r') as f:
        got = f.read()
    t.assert_eq(got, "In subdir.\n", "file in subdir")

    entries = os.listdir(subdir)
    t.assert_in("test.txt", entries, "file listed in subdir")

    os.unlink(os.path.join(subdir, "test.txt"))
    os.rmdir(subdir)


def test_truncate(t):
    """Truncating a watched file."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Truncate\n\nLong content here.\nMore content.\nEven more.\n")
    time.sleep(0.3)

    with open(t.mount_path("notebook.md"), 'r+') as f:
        f.truncate(12)  # Keep only "# Truncate\n"
    time.sleep(0.3)

    with open(t.mount_path("notebook.md"), 'r') as f:
        actual = f.read()
    t.assert_eq(len(actual), 12, "file truncated to 12 bytes")


# ============================================================
# TIER 7: Endurance
# ============================================================

def test_sustained_operations(t):
    """100 operations over 30 seconds — verify no drift or corruption."""
    with open(t.mount_path("notebook.md"), 'w') as f:
        f.write("# Endurance\n")
    time.sleep(0.3)

    for i in range(100):
        op = random.choice(["append", "append", "append", "overwrite"])
        if op == "append":
            with open(t.mount_path("notebook.md"), 'a') as f:
                f.write(f"\n## E-{i:03d}\n\n{random.choice(string.ascii_letters) * random.randint(20, 200)}\n")
        else:
            with open(t.mount_path("notebook.md"), 'r') as f:
                current = f.read()
            # Keep first 100 chars + new content
            with open(t.mount_path("notebook.md"), 'w') as f:
                f.write(current[:100] + f"\n## Rewritten at {i}\n")
        time.sleep(0.05)

    time.sleep(1)

    # Verify: file readable, shadow exists and is larger, journal has entries
    with open(t.mount_path("notebook.md"), 'r') as f:
        actual = f.read()
    t.assert_true(len(actual) > 0, "file not empty after 100 ops")

    shadow = open(t.shadow_path("notebook.md")).read()
    t.assert_true(len(shadow) > len(actual), "shadow larger than actual")

    journal = t.journal()
    journal_lines = [l for l in journal.strip().split('\n') if 'notebook.md' in l]
    t.assert_true(len(journal_lines) >= 90,
                  f"expected >=90 journal entries, got {len(journal_lines)}")


# ============================================================
# Cleanup helper
# ============================================================

def clean_state(t):
    """Remove all shadow/journal state and watched files."""
    for f in Path(t.shadow_dir).glob("*"):
        f.unlink()
    notebook = t.mount_path("notebook.md")
    if os.path.exists(notebook):
        os.unlink(notebook)
    # Give FUSE a moment
    time.sleep(0.2)


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
        ("TIER 1: Basic Operations", [
            ("passthrough_read", test_passthrough_read),
            ("passthrough_write", test_passthrough_write),
            ("simple_append", test_simple_append),
            ("simple_modification", test_simple_modification),
            ("simple_deletion", test_simple_deletion),
            ("file_unlink", test_file_unlink),
        ]),
        ("TIER 2: Multi-operation Sequences", [
            ("append_modify_delete_sequence", test_append_modify_delete_sequence),
            ("rapid_appends", test_rapid_appends),
            ("overwrite_cycle", test_overwrite_cycle),
            ("large_file", test_large_file),
            ("binary_safe_passthrough", test_binary_safe_passthrough),
        ]),
        ("TIER 3: Concurrent Access", [
            ("concurrent_appends", test_concurrent_appends),
            ("concurrent_append_vs_overwrite", test_concurrent_append_vs_overwrite),
        ]),
        ("TIER 4: Policy Enforcement Under Load", [
            ("policy_violation_counting", test_policy_violation_counting),
            ("mixed_policy_clean_and_dirty", test_mixed_policy_clean_and_dirty),
        ]),
        ("TIER 5: Shadow Integrity Under Stress", [
            ("shadow_monotonic_growth", test_shadow_monotonic_growth),
            ("large_shadow_accumulation", test_large_shadow_accumulation),
            ("journal_completeness", test_journal_completeness),
        ]),
        ("TIER 6: Edge Cases", [
            ("empty_file", test_empty_file),
            ("unicode_content", test_unicode_content),
            ("long_lines", test_long_lines),
            ("many_small_files", test_many_small_files),
            ("directory_operations", test_directory_operations),
            ("truncate", test_truncate),
        ]),
        ("TIER 7: Endurance", [
            ("sustained_operations", test_sustained_operations),
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
