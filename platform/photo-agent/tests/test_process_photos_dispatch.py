"""
Dispatch-path tests for the process-photos Hermes skill (issue #7 cross-review
finding, PR #18).

test_process_photos_skill.py checks structural consistency between SKILL.md
and process_photos.py (frontmatter shape, regex parity, prose content) but
never touches Hermes itself, so it can't confirm that Hermes actually routes
an incoming command to this file. These tests close that gap by running
Hermes's own dispatch-resolution code (imported from the local Hermes
install, see platform/docs/hermes/01-install.md) against this skill's actual
files on disk -- no mocking of Hermes's own logic beyond swapping which
directory it scans (so the test doesn't depend on, or mutate, the operator's
real ~/.hermes/config.yaml), and no LLM call (deterministic, free, fast).

The probe runs under Hermes's own venv interpreter
(~/.hermes/hermes-agent/venv/bin/python), not fieldkit's, because Hermes has
its own dependency set fieldkit's test env doesn't install -- importing its
modules under fieldkit's interpreter fails (or silently no-ops, since
scan_skill_commands() swallows import errors). Subprocessing into Hermes's
real venv is what actually exercises its real dispatch code, matching how
the running gateway invokes it.

FieldKit's Hermes runtime is a single-machine deployment (Platform Feature
003 installs Hermes on one admin Mac Mini, not as portable multi-environment
software -- see platform/docs/hermes/01-install.md), so `~/.hermes/
hermes-agent` is a fixed, documented path rather than fragile hardcoding.
These tests are skipped automatically wherever that path doesn't exist (e.g.
a contributor's machine without Hermes installed) -- override with the
HERMES_AGENT_DIR env var if Hermes lives elsewhere.

What this does NOT cover: a real LLM turn following the skill's prose
instructions (extracting the argument, validating it, invoking the script).
That requires an actual model call and isn't reproducible/free/deterministic
enough for an automated suite. platform/docs/hermes/03-process-photos-skill.md's
"Verification" section documents the manual CLI-based substitute for that --
exact commands run and output observed, via `hermes -z ... --skills
process-photos`.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

_HERMES_AGENT_DIR = Path(
    os.environ.get("HERMES_AGENT_DIR", os.path.expanduser("~/.hermes/hermes-agent"))
)
_HERMES_VENV_PYTHON = _HERMES_AGENT_DIR / "venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(
    not _HERMES_VENV_PYTHON.is_file(),
    reason=(
        f"Hermes not installed at {_HERMES_AGENT_DIR} -- dispatch-path tests "
        "require a real Hermes install (platform/docs/hermes/01-install.md); "
        "override with HERMES_AGENT_DIR"
    ),
)

_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
_SKILL_MD = _SKILLS_DIR / "process-photos" / "SKILL.md"

# Runs inside Hermes's own venv (see module docstring) via `python -c`.
# argv[1] = external skills dir to scan (this repo's skills/, not the
# operator's real ~/.hermes/config.yaml external_dirs).
_PROBE = """
import json, sys
from pathlib import Path
from unittest.mock import patch
from agent import skill_commands, skill_utils

skills_dir = Path(sys.argv[1])
with patch.object(skill_utils, "get_external_skills_dirs", return_value=[skills_dir]):
    commands = skill_commands.scan_skill_commands()
    resolved_underscore = skill_commands.resolve_skill_command_key("process_photos")
    resolved_hyphen = skill_commands.resolve_skill_command_key("process-photos")

print(json.dumps({
    "skill_md_paths": {k: v["skill_md_path"] for k, v in commands.items()},
    "names": {k: v["name"] for k, v in commands.items()},
    "resolved_underscore": resolved_underscore,
    "resolved_hyphen": resolved_hyphen,
}))
"""


@pytest.fixture(scope="module")
def dispatch_result():
    """Run the real Hermes dispatch-resolution pipeline once and cache the
    parsed result for all tests in this module."""
    proc = subprocess.run(
        [str(_HERMES_VENV_PYTHON), "-c", _PROBE, str(_SKILLS_DIR)],
        capture_output=True,
        text=True,
        cwd=str(_HERMES_AGENT_DIR),
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"Hermes dispatch probe failed (exit {proc.returncode}):\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _frontmatter_name() -> str:
    raw = _SKILL_MD.read_text(encoding="utf-8")
    _, frontmatter, _ = raw.split("---\n", 2)
    match = re.search(r"^name:\s*(\S+)\s*$", frontmatter, re.MULTILINE)
    assert match, "expected name: in SKILL.md frontmatter"
    return match.group(1)


def test_skill_file_exists_where_hermes_expects_it():
    assert _SKILL_MD.is_file(), f"expected skill at {_SKILL_MD}"


def test_hermes_registers_the_skill_under_the_expected_command_key(dispatch_result):
    """Hermes's real scan_skill_commands() must map /process-photos to this
    exact file -- not merely parse the frontmatter correctly in isolation."""
    assert "/process-photos" in dispatch_result["skill_md_paths"]
    assert dispatch_result["skill_md_paths"]["/process-photos"] == str(_SKILL_MD)


def test_hermes_resolves_the_telegram_form_of_the_command(dispatch_result):
    """Telegram sanitizes hyphens to underscores when it registers the bot
    command (hermes_cli.commands._sanitize_telegram_name), so `/process_photos`
    -- not `/process-photos` -- is what actually arrives at the gateway from a
    real Telegram message. Confirm Hermes's own resolver
    (agent.skill_commands.resolve_skill_command_key) still routes that
    underscored form to this skill."""
    assert dispatch_result["resolved_underscore"] == "/process-photos"
    assert dispatch_result["resolved_hyphen"] == "/process-photos"


def test_frontmatter_name_is_the_source_of_the_registered_command(dispatch_result):
    """Belt-and-suspenders: the discovered command's registered `name` is
    actually this file's own frontmatter `name`, not a coincidence of the
    directory name."""
    assert dispatch_result["names"]["/process-photos"] == _frontmatter_name()
