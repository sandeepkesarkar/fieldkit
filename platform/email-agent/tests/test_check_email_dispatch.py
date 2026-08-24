"""
Dispatch-path tests for the check-email Hermes skill (issue #25).

test_check_email_skill.py checks structural consistency between SKILL.md and
check_email.py but never touches Hermes itself, so it can't confirm that
Hermes actually routes an incoming command to this file. These tests close
that gap the same way test_process_photos_dispatch.py (#7/#18) and
test_check_approval_dispatch.py (#8) did -- running Hermes's own
dispatch-resolution code (imported from the local Hermes install, see
platform/docs/hermes/01-install.md) against this skill's actual files on
disk -- no mocking of Hermes's own logic beyond swapping which directory it
scans (so the test doesn't depend on, or mutate, the operator's real
~/.hermes/config.yaml), and no LLM call (deterministic, free, fast).

Unlike check-approval, check_email has no button-callback surface at all --
Telegram acknowledgements are outbound-only notifications, not interactive
messages with callback_data -- so there is no negative-path probe needed
here, only the manual-command positive path (the same shape as
process-photos's dispatch test).

This module covers two distinct layers -- conflating them is what prompted
a cross-review round on #40 against an earlier draft of the accompanying
doc (platform/docs/hermes/08-check-email-skill.md), so they are tested
(and named) separately here:

1. `scan_skill_commands()` / `resolve_skill_command_key()` -- Hermes's
   internal bookkeeping. `/check-email` is the internal canonical command
   key; `resolve_skill_command_key` treats a `check_email`/`check-email`
   *input* as interchangeable when mapping it back to that key. Neither of
   these ever touches Telegram.
2. `hermes_cli.commands._sanitize_telegram_name()` -- the function that
   actually determines the registered Telegram bot command (converting the
   internal hyphenated key back to Telegram's required underscored form,
   since Telegram restricts bot command names to `[a-z0-9_]`). This is the
   only one of the two layers that says anything about what the admin
   actually sees/types in Telegram.

The probe runs under Hermes's own venv interpreter
(~/.hermes/hermes-agent/venv/bin/python), not fieldkit's -- see
test_process_photos_dispatch.py's module docstring for the full rationale
(Hermes has its own dependency set fieldkit's test env doesn't install).

FieldKit's Hermes runtime is a single-machine deployment (see
platform/docs/hermes/01-install.md), so `~/.hermes/hermes-agent` is a fixed,
documented path. These tests are skipped automatically wherever that path
doesn't exist -- override with the HERMES_AGENT_DIR env var if Hermes lives
elsewhere.

What this does NOT cover: a real LLM turn following the skill's prose
instructions. platform/docs/hermes/08-check-email-skill.md documents the
manual CLI-based substitute for that.
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
_SKILL_MD = _SKILLS_DIR / "check-email" / "SKILL.md"

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
    resolved_underscore = skill_commands.resolve_skill_command_key("check_email")
    resolved_hyphen = skill_commands.resolve_skill_command_key("check-email")

print(json.dumps({
    "skill_md_paths": {k: v["skill_md_path"] for k, v in commands.items()},
    "names": {k: v["name"] for k, v in commands.items()},
    "resolved_underscore": resolved_underscore,
    "resolved_hyphen": resolved_hyphen,
}))
"""

# Layer 2 (see module docstring): what Hermes actually registers as the
# Telegram bot command, independent of the internal-key resolution above.
_TELEGRAM_NAME_PROBE = """
from hermes_cli.commands import _sanitize_telegram_name
print(_sanitize_telegram_name("check-email"))
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


@pytest.fixture(scope="module")
def telegram_command_name():
    """Run Hermes's real _sanitize_telegram_name() once and cache the
    registered Telegram command name it produces for this skill."""
    proc = subprocess.run(
        [str(_HERMES_VENV_PYTHON), "-c", _TELEGRAM_NAME_PROBE],
        capture_output=True,
        text=True,
        cwd=str(_HERMES_AGENT_DIR),
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"Hermes Telegram-name probe failed (exit {proc.returncode}):\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return proc.stdout.strip()


def _frontmatter_name() -> str:
    raw = _SKILL_MD.read_text(encoding="utf-8")
    _, frontmatter, _ = raw.split("---\n", 2)
    match = re.search(r"^name:\s*(\S+)\s*$", frontmatter, re.MULTILINE)
    assert match, "expected name: in SKILL.md frontmatter"
    return match.group(1)


def test_skill_file_exists_where_hermes_expects_it():
    assert _SKILL_MD.is_file(), f"expected skill at {_SKILL_MD}"


def test_hermes_registers_the_skill_under_the_expected_command_key(dispatch_result):
    """Hermes's real scan_skill_commands() must map /check-email to this
    exact file -- not merely parse the frontmatter correctly in isolation."""
    assert "/check-email" in dispatch_result["skill_md_paths"]
    assert dispatch_result["skill_md_paths"]["/check-email"] == str(_SKILL_MD)


def test_hermes_resolves_the_underscored_input_form_to_the_same_internal_key(dispatch_result):
    """Layer 1 (see module docstring): agent.skill_commands.resolve_skill_command_key
    treats a `check_email`/`check-email` *input* as interchangeable, both
    mapping back to the same internal canonical key `/check-email`. This is
    Hermes's internal lookup bookkeeping -- it does NOT by itself say
    anything about what Telegram registers as the actual bot command; that
    is verified separately by test_sanitize_telegram_name_converts_the_internal_key_to_the_registered_command
    below (Layer 2), since an earlier draft conflated the two (#40
    cross-review)."""
    assert dispatch_result["resolved_underscore"] == "/check-email"
    assert dispatch_result["resolved_hyphen"] == "/check-email"


def test_frontmatter_name_is_the_source_of_the_registered_command(dispatch_result):
    """Belt-and-suspenders: the discovered command's registered `name` is
    actually this file's own frontmatter `name`, not a coincidence of the
    directory name."""
    assert dispatch_result["names"]["/check-email"] == _frontmatter_name()


def test_sanitize_telegram_name_converts_the_internal_key_to_the_registered_command(
    telegram_command_name,
):
    """Layer 2 (see module docstring): hermes_cli.commands._sanitize_telegram_name
    is what actually determines the Telegram-registered bot command --
    converting the internal hyphenated key `check-email` back to the
    underscored `check_email` Telegram requires (bot command names are
    restricted to `[a-z0-9_]`, no hyphens). This is the boundary
    test_hermes_resolves_the_underscored_input_form_to_the_same_internal_key
    above does NOT cover -- that test only shows Hermes's internal resolver
    treats both spellings as equivalent *input*, not what gets registered
    with Telegram. Added directly in response to #40's cross-review, which
    flagged the accompanying doc for conflating the two."""
    assert telegram_command_name == "check_email"
