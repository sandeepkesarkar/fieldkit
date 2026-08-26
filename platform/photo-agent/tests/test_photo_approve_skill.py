"""
Structural tests for the Hermes `photo-approve` skill (issue #49, superseding
issue #8's `check-approval`).

Named `photo-approve`, not the shorter `approve` the issue originally asked
for — `approve` collides with a Hermes core command (its own dangerous-
shell-command approval gate) and Hermes's scan_skill_commands() skips
auto-registering a same-named skill's slash command in that case (verified
empirically; see test_photo_approve_dispatch.py and SKILL.md's naming
note). Confirmed with the repo owner via AskUserQuestion rather than
guessed; the owner chose the symmetric `photo-approve`/`photo-reject` pair.

SKILL.md is prose an LLM follows, not executable code, so these tests can't
exercise the dispatch behavior itself. Dispatch-path coverage (Hermes
actually routing /photo_approve to this file) lives in
test_photo_approve_dispatch.py. What these tests check: the skill's
documented contract stays consistent with the script it dispatches to, so
an edit to one side can't silently drift from the other.
"""

import re
from pathlib import Path

_SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "photo-approve" / "SKILL.md"
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


def _visible_body() -> str:
    """Body text with the leading HTML historical-context comment stripped.

    The comment legitimately discusses the retired button flow and the
    approve/Hermes-core-command collision in prose (both mention "button"
    and other retired terms as history) — content checks below apply only
    to the instructions actually shown to the dispatching LLM.
    """
    _, body = _read_skill()
    return re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)


def test_skill_file_exists():
    assert _SKILL_PATH.is_file(), f"expected skill at {_SKILL_PATH}"


def test_old_check_approval_skill_directory_is_removed():
    """check-approval/SKILL.md (issue #8) is superseded by this renamed skill,
    same pattern #18 used to remove the older OpenClaw-format file."""
    old_path = _SKILL_PATH.parents[1] / "check-approval" / "SKILL.md"
    assert not old_path.exists(), f"stale check-approval skill file still present: {old_path}"


def test_old_approve_skill_directory_is_removed():
    """The intermediate `approve` naming (abandoned after the Hermes core-command
    collision was found) must not linger alongside the final `photo-approve`."""
    old_path = _SKILL_PATH.parents[1] / "approve" / "SKILL.md"
    assert not old_path.exists(), f"stale approve skill file still present: {old_path}"


def test_frontmatter_name_is_spec_compliant():
    """The skill's `name` must be lowercase letters/digits/hyphens only, per
    the agentskills.io spec."""
    frontmatter, _ = _read_skill()
    assert re.search(r"^name:\s*photo-approve\s*$", frontmatter, re.MULTILINE)


def test_prerequisites_list_python3():
    frontmatter, _ = _read_skill()
    commands_match = re.search(r"commands:\s*\[([^\]]*)\]", frontmatter)
    assert commands_match, "expected prerequisites.commands: [...] in frontmatter"
    commands = [c.strip() for c in commands_match.group(1).split(",")]
    assert "python3" in commands


def test_body_references_the_real_script_path():
    assert "scripts/check_approval.py" in _visible_body()
    assert _SCRIPT_PATH.is_file(), "referenced script must actually exist"


def test_body_invokes_callback_data_approve_only():
    """This skill must invoke the script with `--callback-data approve` and
    must not claim to handle a reject decision — that's the sibling
    `photo-reject` skill's job."""
    body = _visible_body()
    assert "--callback-data approve" in body
    assert "--callback-data reject" not in body


def test_body_instructs_verbatim_relay_not_summarization():
    lowered = _visible_body().lower()
    assert "verbatim" in lowered
    assert "do not summarise" in lowered or "do not summarize" in lowered


def test_body_instructs_no_retry():
    """Acceptance criteria: 'Run the script once and do not retry.'"""
    assert "do not retry" in _visible_body().lower()


def test_body_does_not_mention_inline_buttons():
    """Issue #49: no inline Approve/Reject buttons remain anywhere in the flow
    this skill documents — a stale reference here would mean the doc drifted
    from the actual (button-free) approval-request message. Checked against
    the visible instructions only; the historical HTML comment legitimately
    discusses the retired button flow as history."""
    assert "button" not in _visible_body().lower()


def test_body_references_the_underscore_telegram_command():
    """The visible instructions must tell the admin the actual Telegram-facing
    command (/photo_approve, underscored) — not the internal hyphenated slug."""
    assert "/photo_approve" in _visible_body()


def test_script_callback_data_choices_match_what_the_skill_uses():
    """Belt-and-suspenders: check_approval.py's argparse --callback-data
    choices must still include 'approve', or this skill's invocation breaks."""
    script_source = _SCRIPT_PATH.read_text(encoding="utf-8")
    choices_match = re.search(
        r'"--callback-data".*?choices=\[([^\]]*)\]', script_source, re.DOTALL
    )
    assert choices_match, "expected --callback-data argparse choices in check_approval.py"
    choices = {c.strip().strip('"').strip("'") for c in choices_match.group(1).split(",")}
    assert choices == {"approve", "reject"}
