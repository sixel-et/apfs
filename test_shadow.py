#!/usr/bin/env python3
"""Tests for APFS ShadowEngine and FilePolicy.

These tests exercise the shadow logic without FUSE — they test the
diff-and-record engine directly. No kernel module or mount needed.

Run: python3 test_shadow.py
"""

import os
import sys
import tempfile
import shutil
import unittest.mock

# Mock the fuse module before importing apfs
sys.modules['fuse'] = unittest.mock.MagicMock()

# Import from the same directory
sys.path.insert(0, os.path.dirname(__file__))
from apfs import ShadowEngine, FilePolicy


def test_append():
    """Appending content should be recorded as append with no strikethrough."""
    with tempfile.TemporaryDirectory() as shadow_dir:
        engine = ShadowEngine(shadow_dir, ["notebook.md"])
        engine.snapshot("notebook.md", "# Notebook\n\n## Entry 1\n\nOriginal.\n")

        result = engine.process_write(
            "notebook.md",
            "# Notebook\n\n## Entry 1\n\nOriginal.\n\n## Entry 2\n\nNew content.\n",
            agent_id="comms"
        )

        assert result["type"] == "append", f"Expected append, got {result['type']}"
        assert result["agent"] == "comms"
        assert result["additions"] > 0
        assert result["deletions"] == 0

        shadow = open(engine.shadow_path("notebook.md")).read()
        assert "<!-- APFS: append by comms" in shadow
        assert "~~" not in shadow  # no strikethrough for appends
        assert "Entry 2" in shadow

        journal = open(os.path.join(shadow_dir, "journal.log")).read()
        assert "append" in journal
        assert "comms" in journal

    print("  PASS: test_append")


def test_modification():
    """Modifying content should show old (struck) and new versions."""
    with tempfile.TemporaryDirectory() as shadow_dir:
        engine = ShadowEngine(shadow_dir, ["notebook.md"])
        engine.snapshot("notebook.md", "# Notebook\n\nWater is wet.\n")

        result = engine.process_write(
            "notebook.md",
            "# Notebook\n\nWater is cold.\n",
            agent_id="bio"
        )

        assert result["type"] == "modification"
        assert result["deletions"] > 0
        assert result["additions"] > 0

        shadow = open(engine.shadow_path("notebook.md")).read()
        assert "~~Water is wet.~~" in shadow
        assert "Water is cold." in shadow
        assert "replaced with" in shadow

    print("  PASS: test_modification")


def test_deletion():
    """Deleting content should produce strikethroughs."""
    with tempfile.TemporaryDirectory() as shadow_dir:
        engine = ShadowEngine(shadow_dir, ["notebook.md"])
        engine.snapshot("notebook.md", "# Notebook\n\n## Entry 1\n\nKeep.\n\n## Entry 2\n\nRemove.\n")

        result = engine.process_write(
            "notebook.md",
            "# Notebook\n\n## Entry 1\n\nKeep.\n",
            agent_id="reviewer"
        )

        assert result["type"] == "deletion"
        assert result["deletions"] > 0
        assert result["additions"] == 0

        shadow = open(engine.shadow_path("notebook.md")).read()
        assert "~~## Entry 2~~" in shadow
        assert "[deleted" in shadow

    print("  PASS: test_deletion")


def test_no_change():
    """Writing identical content should return no_change."""
    with tempfile.TemporaryDirectory() as shadow_dir:
        content = "# Notebook\n\nSame content.\n"
        engine = ShadowEngine(shadow_dir, ["notebook.md"])
        engine.snapshot("notebook.md", content)

        result = engine.process_write("notebook.md", content, agent_id="comms")

        assert result["type"] == "no_change"

    print("  PASS: test_no_change")


def test_full_deletion():
    """Deleting all content (file truncation/emptying) should strikethrough everything."""
    with tempfile.TemporaryDirectory() as shadow_dir:
        engine = ShadowEngine(shadow_dir, ["notebook.md"])
        engine.snapshot("notebook.md", "# Notebook\n\nAll content.\n")

        result = engine.process_write("notebook.md", "", agent_id="bio-delete")

        assert result["type"] == "deletion"
        assert result["deletions"] > 0

        shadow = open(engine.shadow_path("notebook.md")).read()
        assert "~~# Notebook~~" in shadow

    print("  PASS: test_full_deletion")


def test_shadow_accumulates():
    """Multiple operations should accumulate in the shadow."""
    with tempfile.TemporaryDirectory() as shadow_dir:
        engine = ShadowEngine(shadow_dir, ["notebook.md"])
        engine.snapshot("notebook.md", "# Notebook\n")

        # Append
        engine.process_write("notebook.md", "# Notebook\n\n## Entry 1\n", agent_id="comms")
        # Modify
        engine.process_write("notebook.md", "# Notebook\n\n## Entry ONE\n", agent_id="bio")
        # Delete
        engine.process_write("notebook.md", "# Notebook\n", agent_id="reviewer")

        shadow = open(engine.shadow_path("notebook.md")).read()
        assert shadow.count("<!-- APFS:") == 3  # three operations
        assert "comms" in shadow
        assert "bio" in shadow
        assert "reviewer" in shadow

        journal = open(os.path.join(shadow_dir, "journal.log")).read()
        assert journal.count("\n") == 3

    print("  PASS: test_shadow_accumulates")


def test_unwatched_file():
    """Unwatched files should not be tracked."""
    with tempfile.TemporaryDirectory() as shadow_dir:
        engine = ShadowEngine(shadow_dir, ["notebook.md"])

        assert engine.is_watched("notebook.md") is True
        assert engine.is_watched("other.md") is False

    print("  PASS: test_unwatched_file")


def test_policy_append_only():
    """Append-only policy: appends OK, modifications and deletions are violations."""
    assert FilePolicy.check("append_only", "append") == (False, None)

    is_v, reason = FilePolicy.check("append_only", "modification")
    assert is_v is True
    assert "append-only" in reason

    is_v, reason = FilePolicy.check("append_only", "deletion")
    assert is_v is True
    assert "append-only" in reason

    print("  PASS: test_policy_append_only")


def test_policy_annotate_only():
    """Annotate-only policy: appends and modifications OK, deletions are violations."""
    assert FilePolicy.check("annotate_only", "append") == (False, None)
    assert FilePolicy.check("annotate_only", "modification") == (False, None)

    is_v, reason = FilePolicy.check("annotate_only", "deletion")
    assert is_v is True
    assert "annotate-only" in reason

    print("  PASS: test_policy_annotate_only")


def test_policy_unrestricted():
    """Unrestricted policy: nothing is a violation."""
    assert FilePolicy.check("unrestricted", "append") == (False, None)
    assert FilePolicy.check("unrestricted", "modification") == (False, None)
    assert FilePolicy.check("unrestricted", "deletion") == (False, None)

    print("  PASS: test_policy_unrestricted")


def test_policy_violation_logged():
    """Violations should appear in violations.log."""
    with tempfile.TemporaryDirectory() as shadow_dir:
        engine = ShadowEngine(shadow_dir, ["notebook.md"], {"notebook.md": "append_only"})
        engine.snapshot("notebook.md", "# Notebook\n\nOriginal.\n")

        # Append — no violation
        engine.process_write("notebook.md", "# Notebook\n\nOriginal.\n\n## New\n", agent_id="comms")
        assert not os.path.exists(os.path.join(shadow_dir, "violations.log"))

        # Modification — violation
        result = engine.process_write("notebook.md", "# Notebook\n\nChanged.\n\n## New\n", agent_id="bio")
        assert result["violation"] is True

        violations = open(os.path.join(shadow_dir, "violations.log")).read()
        assert "bio" in violations
        assert "append_only" in violations

        journal = open(os.path.join(shadow_dir, "journal.log")).read()
        assert "VIOLATION" in journal

    print("  PASS: test_policy_violation_logged")


def test_policy_no_blocking():
    """Policy violations should NOT prevent the write — shadow records it, that's all."""
    with tempfile.TemporaryDirectory() as shadow_dir:
        engine = ShadowEngine(shadow_dir, ["notebook.md"], {"notebook.md": "append_only"})
        engine.snapshot("notebook.md", "# Notebook\n\nOriginal.\n")

        result = engine.process_write("notebook.md", "# Completely rewritten.\n", agent_id="bio")

        # The write happened (snapshot updated)
        assert engine.snapshots["notebook.md"] == ["# Completely rewritten.\n"]
        # But violation was flagged
        assert result["violation"] is True
        assert result["type"] == "modification"

    print("  PASS: test_policy_no_blocking")


if __name__ == "__main__":
    print("Running APFS ShadowEngine tests...")
    test_append()
    test_modification()
    test_deletion()
    test_no_change()
    test_full_deletion()
    test_shadow_accumulates()
    test_unwatched_file()
    test_policy_append_only()
    test_policy_annotate_only()
    test_policy_unrestricted()
    test_policy_violation_logged()
    test_policy_no_blocking()
    print(f"\nAll 12 tests passed.")
