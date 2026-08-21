"""
Dispatch-path tests for the check-approval Hermes skill (issue #8).

test_check_approval_skill.py checks structural consistency between SKILL.md
and check_approval.py but never touches Hermes itself. These tests close
that gap the same way test_process_photos_dispatch.py did for #7/#18 --
running Hermes's own dispatch-resolution code (imported from the local
Hermes install, see platform/docs/hermes/01-install.md) against this skill's
actual files on disk, and running Hermes's own Telegram callback handler
against FieldKit's actual button payloads.

Per FR-002a (platform/.specify/003-hermes-runtime/spec.md, amended for issue
#8), this skill covers only the manual `/check_approval` command trigger --
the Approve/Reject button-callback trigger is NOT reachable through Hermes
at all, a structural limitation of Hermes's closed callback-prefix design
(verified empirically, not assumed). So this module covers BOTH paths named
in the issue's acceptance criteria, with opposite expected outcomes:

- Manual command path: proves Hermes's scan_skill_commands() /
  resolve_skill_command_key() actually route `/check_approval` to this exact
  file, the same positive assertion #18 made for process-photos.
- Button-callback path: proves the NEGATIVE -- that Hermes's own
  `_handle_callback_query` takes zero action (no answer_callback_query, no
  edit_message_text, no skill/agent-turn dispatch) for FieldKit's actual
  `callback_data` values ("approve" / "reject"), and that
  `_normalize_platform_event` (Hermes's only other generic inbound-event
  hook) returns None for a bare callback_query update. This is what
  SKILL.md's HTML-comment mapping documents in prose; this test is the
  reproducible evidence behind that claim, run against Hermes's real source,
  not a description of it.

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
_SKILL_MD = _SKILLS_DIR / "check-approval" / "SKILL.md"

# ---------------------------------------------------------------------------
# Manual `/check_approval` command path -- same pattern as
# test_process_photos_dispatch.py's _PROBE.
# ---------------------------------------------------------------------------
_COMMAND_PROBE = """
import json, sys
from pathlib import Path
from unittest.mock import patch
from agent import skill_commands, skill_utils

skills_dir = Path(sys.argv[1])
with patch.object(skill_utils, "get_external_skills_dirs", return_value=[skills_dir]):
    commands = skill_commands.scan_skill_commands()
    resolved_underscore = skill_commands.resolve_skill_command_key("check_approval")
    resolved_hyphen = skill_commands.resolve_skill_command_key("check-approval")

print(json.dumps({
    "skill_md_paths": {k: v["skill_md_path"] for k, v in commands.items()},
    "names": {k: v["name"] for k, v in commands.items()},
    "resolved_underscore": resolved_underscore,
    "resolved_hyphen": resolved_hyphen,
}))
"""

# ---------------------------------------------------------------------------
# Approve/Reject button-callback path -- proves Hermes takes no action for
# FieldKit's own bare "approve"/"reject" callback_data, and that the generic
# platform_event hook doesn't cover callback_query either.
# ---------------------------------------------------------------------------
_CALLBACK_PROBE = """
import asyncio, json, sys
from unittest.mock import AsyncMock, MagicMock

from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig

config = PlatformConfig(enabled=True, token="test-token", extra={})
adapter = TelegramAdapter(config)
adapter._bot = AsyncMock()
adapter._app = MagicMock()

results = {}
for cb_data in ("approve", "reject"):
    query = AsyncMock()
    query.data = cb_data
    query.message = MagicMock()
    query.message.chat_id = 12345
    query.from_user = MagicMock()
    query.from_user.id = "12345"
    query.from_user.first_name = "Admin"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    context = MagicMock()

    asyncio.run(adapter._handle_callback_query(update, context))

    results[cb_data] = {
        "answer_called": query.answer.called,
        "edit_message_text_called": query.edit_message_text.called,
    }

norm_update = MagicMock()
norm_update.message_reaction = None
norm_update.edited_message = None
results["normalize_platform_event"] = adapter._normalize_platform_event(norm_update)

print(json.dumps(results))
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


@pytest.fixture(scope="module")
def callback_dispatch_result():
    """Run the real Hermes Telegram callback handler once and cache the result."""
    proc = subprocess.run(
        [str(_HERMES_VENV_PYTHON), "-c", _CALLBACK_PROBE],
        capture_output=True,
        text=True,
        cwd=str(_HERMES_AGENT_DIR),
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"Hermes callback-dispatch probe failed (exit {proc.returncode}):\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _frontmatter_name() -> str:
    raw = _SKILL_MD.read_text(encoding="utf-8")
    _, frontmatter, _ = raw.split("---\n", 2)
    match = re.search(r"^name:\s*(\S+)\s*$", frontmatter, re.MULTILINE)
    assert match, "expected name: in SKILL.md frontmatter"
    return match.group(1)


# ===========================================================================
# Manual command path -- Hermes DOES dispatch this
# ===========================================================================


def test_skill_file_exists_where_hermes_expects_it():
    assert _SKILL_MD.is_file(), f"expected skill at {_SKILL_MD}"


def test_hermes_registers_the_skill_under_the_expected_command_key(command_dispatch_result):
    """Hermes's real scan_skill_commands() must map /check-approval to this
    exact file -- not merely parse the frontmatter correctly in isolation."""
    assert "/check-approval" in command_dispatch_result["skill_md_paths"]
    assert command_dispatch_result["skill_md_paths"]["/check-approval"] == str(_SKILL_MD)


def test_hermes_resolves_the_telegram_form_of_the_command(command_dispatch_result):
    """Telegram sanitizes hyphens to underscores when it registers the bot
    command, so `/check_approval` -- not `/check-approval` -- is what
    actually arrives at the gateway from a real Telegram message. Confirm
    Hermes's own resolver still routes that underscored form to this skill."""
    assert command_dispatch_result["resolved_underscore"] == "/check-approval"
    assert command_dispatch_result["resolved_hyphen"] == "/check-approval"


def test_frontmatter_name_is_the_source_of_the_registered_command(command_dispatch_result):
    assert command_dispatch_result["names"]["/check-approval"] == _frontmatter_name()


# ===========================================================================
# Button-callback path -- Hermes does NOT dispatch this (FR-002a)
# ===========================================================================


def test_hermes_takes_no_action_on_a_bare_approve_callback(callback_dispatch_result):
    """FieldKit's Approve button sends callback_data="approve" with no
    Hermes-recognized prefix. Hermes's _handle_callback_query must fall
    through every branch and do nothing -- proving there is no path from
    this button tap to a skill or agent-turn dispatch."""
    result = callback_dispatch_result["approve"]
    assert result["answer_called"] is False
    assert result["edit_message_text_called"] is False


def test_hermes_takes_no_action_on_a_bare_reject_callback(callback_dispatch_result):
    result = callback_dispatch_result["reject"]
    assert result["answer_called"] is False
    assert result["edit_message_text_called"] is False


def test_generic_platform_event_hook_does_not_cover_callback_query(callback_dispatch_result):
    """_normalize_platform_event is Hermes's only other generic inbound-event
    escape hatch. It must return None for a callback_query-bearing update --
    confirming there is no alternate route for a foreign callback_data to
    reach a skill."""
    assert callback_dispatch_result["normalize_platform_event"] is None
