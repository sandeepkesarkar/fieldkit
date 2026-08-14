"""
Structural tests for the Hermes process_photos skill (platform/.specify/003-hermes-runtime,
issue #7).

SKILL.md is prose an LLM follows, not executable code, so these tests can't
exercise the dispatch behavior itself (verified manually — see
platform/docs/hermes/03-process-photos-skill.md). What they can and do check:
the skill's documented contract stays consistent with the script it dispatches
to, so an edit to one side can't silently drift from the other.
"""

import re
from pathlib import Path

_SKILL_PATH = (
    Path(__file__).resolve().parents[1] / "skills" / "process_photos" / "SKILL.md"
)
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "process_photos.py"


def _read_skill():
    """Split SKILL.md into (frontmatter text, body text).

    Deliberately not a full YAML parse -- this frontmatter is simple
    (flat scalars + one bracketed list) and the project has no PyYAML
    dependency; a minimal split keeps it that way.
    """
    raw = _SKILL_PATH.read_text(encoding="utf-8")
    assert raw.startswith("---\n"), "SKILL.md must start with a YAML frontmatter block"
    _, frontmatter, body = raw.split("---\n", 2)
    return frontmatter, body


def test_skill_file_exists():
    assert _SKILL_PATH.is_file(), f"expected skill at {_SKILL_PATH}"


def test_frontmatter_name_matches_slash_command():
    """The skill's `name` becomes its Telegram slash command (no separate
    user-invocable field in Hermes) -- must stay `process_photos` (underscore)
    to match the admin's existing muscle memory from OpenClaw."""
    frontmatter, _ = _read_skill()
    assert re.search(r"^name:\s*process_photos\s*$", frontmatter, re.MULTILINE)


def test_prerequisites_list_required_binaries():
    frontmatter, _ = _read_skill()
    commands_match = re.search(r"commands:\s*\[([^\]]*)\]", frontmatter)
    assert commands_match, "expected prerequisites.commands: [...] in frontmatter"
    commands = [c.strip() for c in commands_match.group(1).split(",")]
    for required in ("python3", "ffmpeg", "gws"):
        assert required in commands, f"{required} missing from prerequisites.commands"


def test_body_references_the_real_script_path():
    _, body = _read_skill()
    assert "scripts/process_photos.py" in body
    assert _SCRIPT_PATH.is_file(), "referenced script must actually exist"


def test_body_validation_regex_matches_script_regex():
    """The skill tells the LLM to validate against a specific pattern before
    ever invoking the script -- it must be the exact same pattern the script
    enforces independently (scripts/process_photos.py's _PROJECT_NAME_RE), or
    the two layers of validation can silently diverge."""
    _, body = _read_skill()
    skill_pattern_match = re.search(r"pattern `([^`]+)`", body)
    assert skill_pattern_match, "expected a `pattern ...` reference in the skill body"
    skill_pattern = skill_pattern_match.group(1)

    script_source = _SCRIPT_PATH.read_text(encoding="utf-8")
    script_pattern_match = re.search(
        r"_PROJECT_NAME_RE\s*=\s*re\.compile\(r?['\"](.+?)['\"]\)", script_source
    )
    assert script_pattern_match, "expected _PROJECT_NAME_RE in process_photos.py"
    script_pattern = script_pattern_match.group(1)

    assert skill_pattern == script_pattern


def test_body_instructs_verbatim_relay_not_summarization():
    _, body = _read_skill()
    lowered = body.lower()
    assert "verbatim" in lowered
    assert "do not summarise" in lowered or "do not summarize" in lowered


def test_body_never_tells_the_agent_to_touch_drive_directly():
    """The skill dispatches to the script; the agent must never read Drive
    or generate video itself -- that's the whole point of the script split."""
    _, body = _read_skill()
    assert "do not access drive" in body.lower()
