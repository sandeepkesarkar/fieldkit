"""
Dispatch-path tests for the `photo-reject` Hermes skill (issue #49) — new
skill, sibling to `photo-approve`. Mirrors test_photo_approve_dispatch.py's
structure and rationale; see that module's docstring for why this exercises
Hermes's real dispatch-resolution code rather than just structural checks,
and for the naming-symmetry rationale (`reject` alone doesn't collide with
any Hermes core command — confirmed here explicitly, since this skill is
prefixed `photo-` for symmetry with its sibling, not because of its own
collision).
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
_SKILL_MD = _SKILLS_DIR / "photo-reject" / "SKILL.md"

_COMMAND_PROBE = """
import json, sys
from pathlib import Path
from unittest.mock import patch
from agent import skill_commands, skill_utils
from hermes_cli.commands import resolve_command

skills_dir = Path(sys.argv[1])
with patch.object(skill_utils, "get_external_skills_dirs", return_value=[skills_dir]):
    commands = skill_commands.scan_skill_commands()
    resolved = skill_commands.resolve_skill_command_key("photo-reject")
    resolved_underscore = skill_commands.resolve_skill_command_key("photo_reject")

print(json.dumps({
    "skill_md_paths": {k: v["skill_md_path"] for k, v in commands.items()},
    "names": {k: v["name"] for k, v in commands.items()},
    "resolved": resolved,
    "resolved_underscore": resolved_underscore,
    "reject_core_collision": resolve_command("reject") is not None,
}))
"""


@pytest.fixture(scope="module")
def command_dispatch_result():
    """Run the real Hermes skill-command scan once and cache the parsed result."""
    proc = subprocess.run(
        [str(_HERMES_VENV_PYTHON), "-c", _COMMAND_PROBE, str(_SKILLS_DIR)],
        capture_output=True,
        text=True,
        cwd=str(_HERMES_AGENT_DIR),
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"Hermes command-dispatch probe failed (exit {proc.returncode}):\n"
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


def test_reject_does_not_collide_with_a_hermes_core_command(command_dispatch_result):
    """Confirms the naming-symmetry premise: unlike 'approve', a bare
    'reject' does NOT collide with any Hermes core command — the
    `photo-` prefix here is for pairing with photo-approve, not a second
    collision workaround."""
    assert command_dispatch_result["reject_core_collision"] is False


def test_hermes_registers_the_skill_under_the_expected_command_key(command_dispatch_result):
    """Hermes's real scan_skill_commands() must map /photo-reject to this
    exact file -- not merely parse the frontmatter correctly in isolation."""
    assert "/photo-reject" in command_dispatch_result["skill_md_paths"]
    assert command_dispatch_result["skill_md_paths"]["/photo-reject"] == str(_SKILL_MD)


def test_hermes_resolves_both_hyphen_and_underscore_forms(command_dispatch_result):
    """Telegram sanitizes hyphens to underscores when it registers the bot
    command, so `/photo_reject` -- not `/photo-reject` -- is what actually
    arrives at the gateway from a real Telegram message."""
    assert command_dispatch_result["resolved"] == "/photo-reject"
    assert command_dispatch_result["resolved_underscore"] == "/photo-reject"


def test_frontmatter_name_is_the_source_of_the_registered_command(command_dispatch_result):
    assert command_dispatch_result["names"]["/photo-reject"] == _frontmatter_name()


def test_approve_and_reject_are_distinct_registered_commands(command_dispatch_result):
    """Sanity check against a copy-paste-rename mistake: /photo-reject must
    not accidentally resolve to the photo-approve skill's file."""
    approve_md = _SKILL_MD.parents[1] / "photo-approve" / "SKILL.md"
    assert command_dispatch_result["skill_md_paths"]["/photo-reject"] != str(approve_md)
