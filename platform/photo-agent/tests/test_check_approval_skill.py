"""
Structural tests for the Hermes check-approval skill (platform/.specify/003-hermes-runtime,
issue #8).

SKILL.md is prose an LLM follows, not executable code, so these tests can't
exercise the dispatch behavior itself. Dispatch-path coverage (Hermes
actually routing a command to this file) lives in
test_check_approval_dispatch.py; manual LLM-driven verification is in
platform/docs/hermes/04-check-approval-skill.md. What these tests check: the
skill's documented contract stays consistent with the script it dispatches
to, so an edit to one side can't silently drift from the other.

Unlike process-photos (#7/#18), this skill covers only the manual
`/check_approval` command trigger — the Approve/Reject button-callback
trigger is not reachable through Hermes at all (FR-002a, verified in
test_check_approval_dispatch.py and documented in SKILL.md's mapping
comment). These tests assert that scope boundary stays honest: the skill
body must never claim to handle a button tap directly.
"""

import re
from pathlib import Path

_SKILL_PATH = (
    Path(__file__).resolve().parents[1] / "skills" / "check-approval" / "SKILL.md"
)
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_approval.py"


def _read_skill():
    """Split SKILL.md into (frontmatter text, body text).

    Deliberately not a full YAML parse -- see test_process_photos_skill.py's
    identical helper for the rationale (no PyYAML dependency, simple flat
    frontmatter).
    """
    raw = _SKILL_PATH.read_text(encoding="utf-8")
    assert raw.startswith("---\n"), "SKILL.md must start with a YAML frontmatter block"
    _, frontmatter, body = raw.split("---\n", 2)
    return frontmatter, body


def test_skill_file_exists():
    assert _SKILL_PATH.is_file(), f"expected skill at {_SKILL_PATH}"


def test_old_openclaw_skill_file_is_removed():
    """SKILL_check_approval.md (OpenClaw) is superseded by this Hermes skill,
    same as #18 removed SKILL_process_photos.md."""
    old_path = _SKILL_PATH.resolve().parents[2] / "SKILL_check_approval.md"
    assert not old_path.exists(), f"stale OpenClaw skill file still present: {old_path}"


def test_frontmatter_name_is_spec_compliant_and_hyphenated():
    """The skill's `name` must be lowercase letters/digits/hyphens only, per
    the agentskills.io spec (no underscores) -- same rule #18 applied to
    process-photos. Hermes normalizes this to the same internal command key
    and auto-converts it to underscores for the actual Telegram bot command
    regardless (see test_check_approval_dispatch.py), so the admin's
    `/check_approval` muscle memory is unaffected by this."""
    frontmatter, _ = _read_skill()
    assert re.search(r"^name:\s*check-approval\s*$", frontmatter, re.MULTILINE)


def test_prerequisites_list_python3():
    frontmatter, _ = _read_skill()
    commands_match = re.search(r"commands:\s*\[([^\]]*)\]", frontmatter)
    assert commands_match, "expected prerequisites.commands: [...] in frontmatter"
    commands = [c.strip() for c in commands_match.group(1).split(",")]
    assert "python3" in commands


def test_body_references_the_real_script_path():
    _, body = _read_skill()
    assert "scripts/check_approval.py" in body
    assert _SCRIPT_PATH.is_file(), "referenced script must actually exist"


def test_body_only_invokes_the_manual_command_trigger():
    """FR-002a: the button-callback trigger is not reachable through Hermes.
    The body must invoke the script with `--callback-data approve` (the
    manual-command trigger's only valid argument) and must not claim to
    handle a Reject-button tap or a bare cron invocation as something this
    skill dispatches."""
    _, body = _read_skill()
    assert "--callback-data approve" in body
    assert "--callback-data reject" not in body


def test_body_instructs_verbatim_relay_not_summarization():
    _, body = _read_skill()
    lowered = body.lower()
    assert "verbatim" in lowered
    assert "do not summarise" in lowered or "do not summarize" in lowered


def test_body_instructs_no_retry():
    """Acceptance criteria: 'Run the script once and do not retry.'"""
    _, body = _read_skill()
    assert "do not retry" in body.lower()


def test_script_callback_data_choices_match_what_the_mapping_comment_claims():
    """Belt-and-suspenders: check_approval.py's argparse --callback-data
    choices must still be exactly {approve, reject}, or the skill's mapping
    comment (which explains why only "approve" is ever passed manually) goes
    stale."""
    script_source = _SCRIPT_PATH.read_text(encoding="utf-8")
    choices_match = re.search(
        r'"--callback-data".*?choices=\[([^\]]*)\]', script_source, re.DOTALL
    )
    assert choices_match, "expected --callback-data argparse choices in check_approval.py"
    choices = {c.strip().strip('"').strip("'") for c in choices_match.group(1).split(",")}
    assert choices == {"approve", "reject"}
