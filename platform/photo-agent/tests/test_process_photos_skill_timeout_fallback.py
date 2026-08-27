"""
Tests for process-photos/SKILL.md's timeout/gtimeout selection logic
(issue #62 review Engineering-7 — this doc/reality gap was flagged once
already in the PR #58 review and never actually fixed; the fix here is in
the SKILL.md script itself, not just doc language, so it can't drift again).

Root problem: the skill's bash block used to run an unconditional
`timeout 660 python3 ...`, which fails outright with "command not found" on
a machine that has neither GNU `timeout` nor its Homebrew-installed
`gtimeout` alias -- confirmed true of the machine this doc was written
against. The walkthrough doc claimed a graceful fallback existed; it did
not.

The fix makes the SKILL.md's own bash block select at runtime between
`timeout`, `gtimeout`, or no wrapper at all -- these tests parse the actual
bash block (not a hand-copied expectation of it) and drive it as a real
subprocess to prove the selection logic actually works in all three cases,
rather than re-asserting a claim.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL_MD = Path(__file__).parents[1] / "skills" / "process-photos" / "SKILL.md"


def _extract_bash_blocks() -> list[str]:
    text = _SKILL_MD.read_text()
    blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    assert blocks, f"{_SKILL_MD}: no ```bash block found"
    return blocks


def _invocation_block() -> str:
    blocks = _extract_bash_blocks()
    block = next(b for b in blocks if "process_photos.py" in b)
    return block


def test_skill_no_longer_hardcodes_unconditional_timeout_660():
    """The old bug: a bare `timeout 660 python3 ...` line with no
    conditional selection at all."""
    block = _invocation_block()
    for line in block.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("timeout 660 python3"), (
            f"found an unconditional 'timeout 660 python3 ...' line with no "
            f"binary-selection logic: {stripped!r}"
        )


def test_skill_selects_timeout_gtimeout_or_neither_at_runtime():
    block = _invocation_block()
    assert "command -v timeout" in block
    assert "command -v gtimeout" in block
    # The actual invocation line must use whatever was selected, not a
    # hardcoded binary name.
    assert re.search(r"\$TIMEOUT_BIN\s+python3\s+scripts/process_photos\.py", block)


@pytest.mark.parametrize(
    "stub_binaries,expected_wrapper",
    [
        (["timeout"], "timeout"),
        (["gtimeout"], "gtimeout"),
        ([], "none"),
        (["timeout", "gtimeout"], "timeout"),  # timeout preferred when both exist
    ],
)
def test_binary_selection_logic_runs_correctly_for_each_case(tmp_path, stub_binaries, expected_wrapper):
    """Actually EXECUTE the skill's selection logic (extracted verbatim,
    swapping only the final invocation line for an echo so this test needs
    no real photo-agent environment) against a PATH stocked with exactly
    the given stub binaries -- proving the selection works for all three
    real-world cases (GNU timeout present, only gtimeout present, neither
    present), not just that the right substrings appear in the file."""
    block = _invocation_block()
    # Extract only the selection logic (up through the TIMEOUT_BIN= lines),
    # replacing the real invocation with a harmless echo so this test needs
    # no fieldkit environment at all.
    selection_lines = []
    for line in block.splitlines():
        if line.strip().startswith("$TIMEOUT_BIN"):
            break
        selection_lines.append(line)
    selection_script = "\n".join(selection_lines)
    assert "TIMEOUT_BIN=" in selection_script

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name in stub_binaries:
        stub = stub_dir / name
        stub.write_text("#!/usr/bin/env bash\nexec \"$@\"\n")
        stub.chmod(0o755)

    script = selection_script + '\necho "SELECTED=[$TIMEOUT_BIN]"\n'
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={"PATH": str(stub_dir)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    if expected_wrapper == "timeout":
        assert "SELECTED=[timeout 660]" in result.stdout
    elif expected_wrapper == "gtimeout":
        assert "SELECTED=[gtimeout 660]" in result.stdout
    else:
        assert "SELECTED=[]" in result.stdout
