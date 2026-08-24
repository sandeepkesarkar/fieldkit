"""
Structural tests for the Hermes check-email skill (platform/.specify/003-hermes-runtime,
issue #25).

SKILL.md is prose an LLM follows, not executable code, so these tests can't
exercise the dispatch behavior itself. Dispatch-path coverage (Hermes
actually routing a command to this file) lives in
test_check_email_dispatch.py; manual LLM-driven verification is in
platform/docs/hermes/08-check-email-skill.md. What these tests check: the
skill's documented contract stays consistent with the script it dispatches
to, so an edit to one side can't silently drift from the other -- the same
posture test_process_photos_skill.py and test_check_approval_skill.py take
for their own skills.
"""

import re
from pathlib import Path

_SKILL_PATH = (
    Path(__file__).resolve().parents[1] / "skills" / "check-email" / "SKILL.md"
)
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_email.py"


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
    """The old OpenClaw-format platform/email-agent/SKILL.md is superseded by
    this Hermes skill, same as #18/#8 removed their SKILL_*.md files."""
    old_path = _SKILL_PATH.resolve().parents[2] / "SKILL.md"
    assert not old_path.exists(), f"stale OpenClaw skill file still present: {old_path}"


def test_frontmatter_name_is_spec_compliant_and_hyphenated():
    """The skill's `name` must be lowercase letters/digits/hyphens only, per
    the agentskills.io spec (no underscores) -- same rule #18/#8 applied.
    Hermes normalizes this to the same internal command key and
    auto-converts it to underscores for the actual Telegram bot command
    regardless (see test_check_email_dispatch.py), so the admin's
    `/check_email` muscle memory is unaffected by this."""
    frontmatter, _ = _read_skill()
    assert re.search(r"^name:\s*check-email\s*$", frontmatter, re.MULTILINE)


def test_frontmatter_has_no_openclaw_metadata():
    """The old frontmatter's `metadata: {"openclaw": ...}` block and
    `user-invocable: true` field must be gone -- Hermes has no equivalent for
    either (every installed skill's name is automatically a slash command)."""
    frontmatter, _ = _read_skill()
    assert "openclaw" not in frontmatter.lower()
    assert "user-invocable" not in frontmatter.lower()


def test_prerequisites_list_required_binaries():
    frontmatter, _ = _read_skill()
    commands_match = re.search(r"commands:\s*\[([^\]]*)\]", frontmatter)
    assert commands_match, "expected prerequisites.commands: [...] in frontmatter"
    commands = [c.strip() for c in commands_match.group(1).split(",")]
    for required in ("python3", "gws"):
        assert required in commands, f"{required} missing from prerequisites.commands"


def test_body_references_the_real_script_path():
    _, body = _read_skill()
    assert "scripts/check_email.py" in body
    assert _SCRIPT_PATH.is_file(), "referenced script must actually exist"


def test_body_invokes_the_script_with_no_source_flag():
    """A manual invocation must not pass --source cron (that flag is
    reserved for the cron leg and suppresses the 'no new emails' reply,
    which a manual /check_email should still show per today's behavior)."""
    _, body = _read_skill()
    match = re.search(r"python3 scripts/check_email\.py([^\n`]*)", body)
    assert match, "expected a scripts/check_email.py invocation in the skill body"
    assert "--source" not in match.group(1)


def test_body_instructs_verbatim_relay_not_summarization():
    _, body = _read_skill()
    lowered = body.lower()
    assert "verbatim" in lowered
    assert "do not summarise" in lowered or "do not summarize" in lowered


def test_body_never_tells_the_agent_to_read_email_itself():
    """The skill dispatches to the script; the agent must never improvise or
    read emails directly -- that's the whole point of the script split."""
    _, body = _read_skill()
    assert "do not improvise" in body.lower() or "do not read" in body.lower()
