"""
Dispatch-path tests for the `photo-approve` Hermes skill (issue #49,
superseding issue #8's `check-approval` dispatch coverage).

Named `photo-approve`, not the shorter `approve` the issue originally asked
for. Hermes reserves a built-in core command named `approve` (its own
dangerous-shell-command approval gate, `hermes_cli/commands.py`) and
`agent/skill_commands.py::scan_skill_commands()` has an explicit
core-command-collision guard (`resolve_command(cmd_name) is not None`) that
skips auto-registering a same-named skill's slash command in that case —
confirmed empirically against this exact skill's files before this test
module existed (a skill literally named `approve` produced zero entries for
`/approve` and logged "collides with a core Hermes command; skipping
auto-registration"). No frontmatter field lets a skill claim a slash
command different from its normalized `name`. Put to the repo owner via
AskUserQuestion rather than guessed; the owner chose the symmetric
`photo-approve`/`photo-reject` pair. This module proves the RENAMED skill
dispatches cleanly (no collision) and that the abandoned `approve` naming
is fully gone, not just that some command resolves.

test_photo_approve_skill.py checks structural consistency between SKILL.md
and check_approval.py but never touches Hermes itself. These tests close
that gap by running Hermes's own dispatch-resolution code (imported from
the local Hermes install, see platform/docs/hermes/01-install.md) against
this skill's actual files on disk — same rigor as issue #7/#18's review
required for process-photos, and #8's for check-approval: not just
structural or frontmatter self-consistency checks, but Hermes's real
scan_skill_commands() / resolve_skill_command_key() run against this exact
skill.

Both probes run under Hermes's own venv interpreter
(~/.hermes/hermes-agent/venv/bin/python), not fieldkit's -- see
test_process_photos_dispatch.py's module docstring for why (Hermes's own
dependency set, no mocking of Hermes's own logic).

FieldKit's Hermes runtime is a single-machine deployment (see
platform/docs/hermes/01-install.md), so `~/.hermes/hermes-agent` is a fixed,
documented path. These tests are skipped automatically wherever that path
doesn't exist -- override with the HERMES_AGENT_DIR env var if Hermes lives
elsewhere.
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
_SKILL_MD = _SKILLS_DIR / "photo-approve" / "SKILL.md"

_COMMAND_PROBE = """
import json, sys
from pathlib import Path
from unittest.mock import patch
from agent import skill_commands, skill_utils
from hermes_cli.commands import resolve_command

skills_dir = Path(sys.argv[1])
with patch.object(skill_utils, "get_external_skills_dirs", return_value=[skills_dir]):
    commands = skill_commands.scan_skill_commands()
    resolved = skill_commands.resolve_skill_command_key("photo-approve")
    resolved_underscore = skill_commands.resolve_skill_command_key("photo_approve")

print(json.dumps({
    "skill_md_paths": {k: v["skill_md_path"] for k, v in commands.items()},
    "names": {k: v["name"] for k, v in commands.items()},
    "resolved": resolved,
    "resolved_underscore": resolved_underscore,
    "approve_core_collision": resolve_command("approve") is not None,
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


def test_approve_still_collides_with_a_hermes_core_command(command_dispatch_result):
    """Sanity check on the premise of this whole rename: confirms Hermes's
    installed core command registry still reserves 'approve' as of this
    test run — if it ever stopped, the rename to photo-approve would no
    longer be necessary and this whole naming decision should be revisited."""
    assert command_dispatch_result["approve_core_collision"] is True


def test_hermes_registers_the_skill_under_the_expected_command_key(command_dispatch_result):
    """Hermes's real scan_skill_commands() must map /photo-approve to this
    exact file -- not merely parse the frontmatter correctly in isolation,
    and (the actual point of the rename) without hitting the core-command
    collision guard that silently drops /approve."""
    assert "/photo-approve" in command_dispatch_result["skill_md_paths"]
    assert command_dispatch_result["skill_md_paths"]["/photo-approve"] == str(_SKILL_MD)


def test_hermes_resolves_both_hyphen_and_underscore_forms(command_dispatch_result):
    """Telegram sanitizes hyphens to underscores when it registers the bot
    command, so `/photo_approve` -- not `/photo-approve` -- is what actually
    arrives at the gateway from a real Telegram message. Confirm Hermes's
    own resolver routes both forms to this skill."""
    assert command_dispatch_result["resolved"] == "/photo-approve"
    assert command_dispatch_result["resolved_underscore"] == "/photo-approve"


def test_frontmatter_name_is_the_source_of_the_registered_command(command_dispatch_result):
    assert command_dispatch_result["names"]["/photo-approve"] == _frontmatter_name()


def test_bare_approve_command_key_does_not_resolve_to_this_skill(command_dispatch_result):
    """The whole reason for this rename: a skill literally named 'approve'
    is silently dropped from auto-registration. Confirms /approve is not
    claimed by this skill (it isn't claimed by ANY skill — it's reserved by
    Hermes's own core command of the same name, asserted separately above)."""
    assert "/approve" not in command_dispatch_result["skill_md_paths"]


def test_check_approval_command_key_no_longer_resolves(command_dispatch_result):
    """The retired check-approval skill's command key must not still be
    reachable through Hermes's scan of the current skills directory --
    proves the rename actually replaced the old command rather than
    merely adding a new one alongside it."""
    assert "/check-approval" not in command_dispatch_result["skill_md_paths"]
