"""
Structural tests for the Hermes `photo-reject` skill (issue #49) — new
skill, sibling to `photo-approve`. Mirrors test_photo_approve_skill.py's
structure; see that module's docstring for why these can only check
documented-contract consistency, not real dispatch (that's
test_photo_reject_dispatch.py), and for the naming-symmetry rationale
(`reject` alone doesn't collide with any Hermes core command — this skill
is prefixed `photo-` for symmetry with its sibling, not because of its own
collision).
"""

import re
from pathlib import Path

_SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "photo-reject" / "SKILL.md"
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_approval.py"


def _read_skill():
    raw = _SKILL_PATH.read_text(encoding="utf-8")
    assert raw.startswith("---\n"), "SKILL.md must start with a YAML frontmatter block"
    _, frontmatter, body = raw.split("---\n", 2)
    return frontmatter, body


def _visible_body() -> str:
    """Body text with the leading HTML historical-context comment stripped —
    see test_photo_approve_skill.py's identical helper for rationale."""
    _, body = _read_skill()
    return re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)


def test_skill_file_exists():
    assert _SKILL_PATH.is_file(), f"expected skill at {_SKILL_PATH}"


def test_old_reject_skill_directory_is_removed():
    """The intermediate `reject` naming (before the sibling rename to
    `photo-reject` for symmetry with photo-approve) must not linger."""
    old_path = _SKILL_PATH.parents[1] / "reject" / "SKILL.md"
    assert not old_path.exists(), f"stale reject skill file still present: {old_path}"


def test_frontmatter_name_is_spec_compliant():
    frontmatter, _ = _read_skill()
    assert re.search(r"^name:\s*photo-reject\s*$", frontmatter, re.MULTILINE)


def test_prerequisites_list_python3():
    frontmatter, _ = _read_skill()
    commands_match = re.search(r"commands:\s*\[([^\]]*)\]", frontmatter)
    assert commands_match, "expected prerequisites.commands: [...] in frontmatter"
    commands = [c.strip() for c in commands_match.group(1).split(",")]
    assert "python3" in commands


def test_body_references_the_real_script_path():
    assert "scripts/check_approval.py" in _visible_body()
    assert _SCRIPT_PATH.is_file(), "referenced script must actually exist"


def test_body_invokes_callback_data_reject_only():
    """This skill must invoke the script with `--callback-data reject` and
    must not claim to handle an approve decision — that's the sibling
    `photo-approve` skill's job."""
    body = _visible_body()
    assert "--callback-data reject" in body
    assert "--callback-data approve" not in body


def test_body_instructs_verbatim_relay_not_summarization():
    lowered = _visible_body().lower()
    assert "verbatim" in lowered
    assert "do not summarise" in lowered or "do not summarize" in lowered


def test_body_instructs_no_retry():
    assert "do not retry" in _visible_body().lower()


def test_body_does_not_mention_inline_buttons():
    """Issue #49: no inline Approve/Reject buttons remain anywhere in the flow
    this skill documents. Checked against the visible instructions only."""
    assert "button" not in _visible_body().lower()


def test_body_references_the_underscore_telegram_command():
    """The visible instructions must tell the admin the actual Telegram-facing
    command (/photo_reject, underscored) — not the internal hyphenated slug."""
    assert "/photo_reject" in _visible_body()


def test_script_callback_data_choices_match_what_the_skill_uses():
    """Belt-and-suspenders: check_approval.py's argparse --callback-data
    choices must still include 'reject', or this skill's invocation breaks."""
    script_source = _SCRIPT_PATH.read_text(encoding="utf-8")
    choices_match = re.search(
        r'"--callback-data".*?choices=\[([^\]]*)\]', script_source, re.DOTALL
    )
    assert choices_match, "expected --callback-data argparse choices in check_approval.py"
    choices = {c.strip().strip('"').strip("'") for c in choices_match.group(1).split(",")}
    assert choices == {"approve", "reject"}
