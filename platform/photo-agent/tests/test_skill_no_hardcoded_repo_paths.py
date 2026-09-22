"""
Regression guard for issue #74: no live-dispatched skill may pin itself to an
absolute repo path.

History this exists to prevent repeating: all four Hermes-dispatched skills
hardcoded `cd ~/src/fieldkit/...`. When the checkout moved to
`~/src/project-fieldkit-dev/fieldkit` on 2026-08-31 that `cd` started failing,
and `/process_photos`, `/photo_approve`, `/photo_reject` and check-email's
manual command were all broken on the live Mac Mini for ~3 weeks. Nothing
caught it: the cron legs use absolute paths from the crontab and kept working,
so automated publishing masked a total loss of the human approval path.

The fix each skill now uses is Hermes's own `${HERMES_SKILL_DIR}` template
token, substituted with the absolute skill directory at dispatch time
(`agent/skill_preprocessing.py::substitute_template_vars`, gated on
`skills.template_vars`, which defaults to true). The agent directory is then
two levels up from the skill directory, so the skill locates the checkout it
actually lives in — whatever that is.

These tests cover EVERY skill under `platform/*/skills/*/SKILL.md`, not a
hardcoded list of four, so a future skill in either agent is guarded the day
it is added. Three angles, because a static substring check alone is not
enough to prove a shell block works:

1. `test_no_hardcoded_repo_path` — static: no home-relative or absolute
   machine path anywhere in the instructions, and every `cd` target is a
   shell variable rather than a literal.
2. `test_resolution_lands_on_the_real_agent_dir` — dynamic: execute the
   block's real resolution logic, substituted exactly as Hermes substitutes
   it, and assert it lands on this checkout's agent directory.
3. `test_unresolved_skill_dir_fails_closed` — dynamic: execute the same block
   with the token left unsubstituted and assert it exits non-zero with an
   actionable message. Fail closed, never degrade into running against the
   wrong directory — issue #59 was a cross-client data leak caused by exactly
   that kind of silent fallback.
"""

import re
import subprocess
from pathlib import Path

import pytest

# platform/  ->  platform/*/skills/*/SKILL.md covers every agent's skills,
# not just photo-agent's, so this guard cannot be sidestepped by adding a
# skill under a different agent.
_PLATFORM_DIR = Path(__file__).resolve().parents[2]
_SKILL_MDS = sorted(_PLATFORM_DIR.glob("*/skills/*/SKILL.md"))

# Machine-specific path shapes that would re-pin a skill to one layout. The
# leading-`~` and `/Users`|`/home` forms are what issue #74 actually was.
_FORBIDDEN_PATH_PATTERNS = (
    (r"~/", "a home-relative path (`~/...`)"),
    (r"/Users/", "an absolute macOS home path (`/Users/...`)"),
    (r"/home/", "an absolute Linux home path (`/home/...`)"),
)

_BASH_BLOCK_RE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
# `cd <target>`, including inside a `$( ... )` command substitution.
_CD_RE = re.compile(r"\bcd\s+(\S+)")
_SKILL_DIR_TOKEN = "${HERMES_SKILL_DIR}"


def _ids(path: Path) -> str:
    return f"{path.parents[2].name}/{path.parent.name}"


def _visible_body(skill_md: Path) -> str:
    """Instruction text only: frontmatter and HTML comments stripped.

    The HTML comments are historical design notes (the OpenClaw -> Hermes
    port, the `approve` command collision) and legitimately quote old paths
    as history; only what the dispatching LLM is actually told to run is
    in scope here.
    """
    raw = skill_md.read_text(encoding="utf-8")
    assert raw.startswith("---\n"), f"{skill_md}: missing YAML frontmatter"
    _, _frontmatter, body = raw.split("---\n", 2)
    return re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)


def _dispatch_block(skill_md: Path) -> str:
    """The bash block that invokes this skill's python script."""
    blocks = _BASH_BLOCK_RE.findall(_visible_body(skill_md))
    assert blocks, f"{skill_md}: no ```bash block found"
    matching = [b for b in blocks if re.search(r"python3 scripts/\S+\.py", b)]
    assert len(matching) == 1, (
        f"{skill_md}: expected exactly one bash block invoking "
        f"`python3 scripts/*.py`, found {len(matching)}"
    )
    return matching[0]


def _resolution_only(block: str) -> str:
    """The block with its real script invocation replaced by `pwd`.

    Keeps every line of the actual resolution and guard logic intact — this
    is the block's own code, not a hand-copied imitation — while making it
    safe to execute with no fieldkit environment, no Drive access and no
    pipeline run.
    """
    lines = []
    for line in block.splitlines():
        if re.search(r"python3 scripts/\S+\.py", line) and not line.lstrip().startswith(
            ("[", "#")
        ):
            lines.append("pwd")
        else:
            lines.append(line)
    script = "\n".join(lines)
    assert "pwd" in script, "script invocation line not found for substitution"
    return script


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", "-c", script],
        # A deliberately minimal environment: no HERMES_SKILL_DIR, proving the
        # resolution comes from Hermes's textual substitution rather than an
        # inherited variable (Hermes never exports one).
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def test_skills_were_discovered():
    """Guard the guard: a broken glob must not silently pass everything."""
    assert len(_SKILL_MDS) >= 4, f"expected at least 4 skills, found {_SKILL_MDS}"


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
def test_no_hardcoded_repo_path(skill_md):
    body = _visible_body(skill_md)

    for pattern, description in _FORBIDDEN_PATH_PATTERNS:
        for match in re.finditer(re.escape(pattern), body):
            line = body[: match.start()].count("\n") + 1
            pytest.fail(
                f"{skill_md} (body line {line}) contains {description}. "
                f"Skills must resolve the repo from {_SKILL_DIR_TOKEN}, which "
                f"Hermes substitutes with this skill's own directory — a "
                f"literal path breaks on the next repo move (issue #74)."
            )

    block = _dispatch_block(skill_md)
    for target in _CD_RE.findall(block):
        assert target.startswith('"$'), (
            f'{skill_md}: `cd {target}` must change into a shell variable '
            f'(e.g. `cd "$AGENT_DIR"`), not a literal path, so the skill '
            f"follows the checkout it lives in (issue #74)."
        )


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
def test_block_resolves_via_the_hermes_skill_dir_token(skill_md):
    assert _SKILL_DIR_TOKEN in _dispatch_block(skill_md), (
        f"{skill_md}: the dispatch block must resolve the repo from "
        f"{_SKILL_DIR_TOKEN} (Hermes substitutes it with the absolute skill "
        f"directory at dispatch time)."
    )


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
def test_resolution_lands_on_the_real_agent_dir(skill_md):
    """Execute the block's own resolution logic, substituted as Hermes
    substitutes it, and assert it finds the agent directory of THIS
    checkout — the property that makes the fix move-proof."""
    script = _resolution_only(_dispatch_block(skill_md))
    script = script.replace(_SKILL_DIR_TOKEN, str(skill_md.parent))

    result = _run(script)
    assert result.returncode == 0, (
        f"{skill_md}: resolution failed with exit {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    resolved = Path(result.stdout.strip().splitlines()[-1]).resolve()
    expected = skill_md.resolve().parents[2]
    assert resolved == expected, f"resolved to {resolved}, expected {expected}"


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
def test_unresolved_skill_dir_fails_closed(skill_md):
    """With the token left unsubstituted (e.g. skills.template_vars turned
    off), the block must abort with an actionable error — never fall through
    to `cd ""` and run against whatever directory it happens to be in."""
    script = _resolution_only(_dispatch_block(skill_md))
    assert _SKILL_DIR_TOKEN in script, "expected the raw token to still be present"

    result = _run(script)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"{skill_md}: an unresolved skill directory exited 0 — it must fail "
        f"closed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ERROR" in combined, (
        f"{skill_md}: failure was not reported with an actionable ERROR "
        f"message.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "HERMES_SKILL_DIR" in combined, (
        f"{skill_md}: the error message must name HERMES_SKILL_DIR so the "
        f"operator knows what to fix.\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
