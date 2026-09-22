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
it is added. A static substring check alone cannot prove a shell block works,
so most of these execute the block's own code:

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
4. `test_shell_metacharacter_in_path_fails_closed` /
   `test_benign_path_shapes_still_resolve` — dynamic: the substituted path is
   shell source, so prove a metacharacter-bearing checkout path neither
   executes nor silently resolves elsewhere, while a path containing a space
   still works. See the section comment below for the full reasoning.
5. `test_no_stale_repo_path_in_current_docs` — static, and not about the
   skills: an operator following a stale runbook reintroduces the same
   breakage by hand, so current operator docs must not name the pre-move
   checkout location either.
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
# ---------------------------------------------------------------------------
# Substitution-as-shell-source injection (PR #75 review, blocking 1)
# ---------------------------------------------------------------------------
# Hermes substitutes the skill directory TEXTUALLY into the block before Bash
# parses it, so the path is shell *source*, not shell *data*. Double quotes do
# not protect it: `$VAR` and `$(...)` still expand inside them. The original
# fix interpolated the token in double quotes, which meant a checkout path
# containing `$`, `$(...)` or a backtick either executed during the
# assignment or collapsed onto a DIFFERENT real checkout and ran there at
# exit 0 — reintroducing the exact silent-wrong-directory shape of issue #59
# that the guards exist to prevent.
#
# Each case below builds a fake checkout whose directory name carries the
# payload, PLUS a decoy checkout at the name the payload collapses to if it
# were expanded, so an implementation that expands cannot quietly pass by
# landing somewhere that happens to exist.

_INJECTION_CASES = [
    # (label, directory name carrying the payload, name it collapses to if expanded)
    ("dollar-var", "repo$USER", "repo"),
    ("cmd-subst", "repo$(printf INJECTED)", "repoINJECTED"),
    ("backtick", "repo`printf INJECTED`", "repoINJECTED"),
    ("double-quote", 'repo"x', None),
    ("single-quote", "repo'x", None),
    # $(touch PWNED) leaves a file behind in the working directory if the
    # substitution is ever evaluated — a direct, positive proof of execution
    # rather than an inference from the resolved path.
    ("rce-sentinel", "repo$(touch PWNED)", "repo"),
]

# Path shapes that are unusual but legitimate and MUST keep working: quoting
# already handles a space, and rejecting one would be a capability regression.
_BENIGN_CASES = [("plain", "repo"), ("space", "repo with spaces")]


def _script_name(block: str) -> str:
    match = re.search(r"python3 scripts/(\S+\.py)", block)
    assert match, "no `python3 scripts/*.py` invocation found"
    return match.group(1)


def _fake_checkout(root: Path, dirname: str, skill_md: Path) -> Path:
    """Create <root>/<dirname>/platform/<agent>/skills/<skill>/ with a stub script.

    Mirrors the real layout so the block's own resolution and its
    target-script guard are exercised against a complete, plausible checkout
    — the payload is carried only in the directory name.
    """
    agent_dir = root / dirname / "platform" / skill_md.parents[2].name
    skill_dir = agent_dir / "skills" / skill_md.parent.name
    skill_dir.mkdir(parents=True, exist_ok=True)
    scripts = agent_dir / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / _script_name(_dispatch_block(skill_md))).write_text("# stub\n")
    return skill_dir


def _run_as_dispatched(skill_md: Path, skill_dir: Path, cwd: Path):
    """Substitute the token with *skill_dir* exactly as Hermes does, then run.

    Hermes performs a plain textual replacement of the bare `${HERMES_SKILL_DIR}`
    token (`agent/skill_preprocessing.py::substitute_template_vars`), which is
    what makes the substituted text shell source in the first place; modelling
    it as a textual replace here keeps the test hermetic (no Hermes import)
    while reproducing the property under test.
    """
    script = _resolution_only(_dispatch_block(skill_md))
    script = script.replace(_SKILL_DIR_TOKEN, str(skill_dir))
    return subprocess.run(
        ["/bin/bash", "-c", script],
        # USER is deliberately absent: an expanded `$USER` then collapses to
        # the empty string, which is how a `$`-bearing path silently lands on
        # a neighbouring checkout.
        env={"PATH": "/usr/bin:/bin"},
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
@pytest.mark.parametrize(
    "dirname,collapsed",
    [case[1:] for case in _INJECTION_CASES],
    ids=[case[0] for case in _INJECTION_CASES],
)
def test_shell_metacharacter_in_path_fails_closed(tmp_path, skill_md, dirname, collapsed):
    """A checkout path carrying shell metacharacters must abort loudly — never
    execute the path and never resolve to a different directory."""
    skill_dir = _fake_checkout(tmp_path, dirname, skill_md)
    if collapsed is not None:
        # A complete, working checkout at the collapsed name, so expansion
        # would silently succeed instead of erroring.
        decoy = _fake_checkout(tmp_path, collapsed, skill_md).parents[1]
    else:
        decoy = None

    result = _run_as_dispatched(skill_md, skill_dir, cwd=tmp_path)
    combined = result.stdout + result.stderr
    landed = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""

    assert not (tmp_path / "PWNED").exists(), (
        f"{skill_md}: the substituted path was EXECUTED — `$(touch PWNED)` in "
        f"the checkout directory name ran. The skill directory must be "
        f"interpolated as data, not as shell source."
    )
    if decoy is not None:
        assert landed != str(decoy), (
            f"{skill_md}: silently resolved to a DIFFERENT checkout "
            f"({decoy}) because the path was expanded — this is the "
            f"issue #59 silent-wrong-directory shape, at exit "
            f"{result.returncode}."
        )
    assert result.returncode != 0, (
        f"{skill_md}: exited 0 on a metacharacter-bearing path.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ERROR" in combined, (
        f"{skill_md}: aborted without an actionable ERROR message (a raw Bash "
        f"syntax error is not actionable).\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
@pytest.mark.parametrize(
    "dirname", [case[1] for case in _BENIGN_CASES], ids=[case[0] for case in _BENIGN_CASES]
)
def test_benign_path_shapes_still_resolve(tmp_path, skill_md, dirname):
    """Hardening must not cost legitimate path shapes — a space in the
    checkout path is already handled by quoting and must keep working."""
    skill_dir = _fake_checkout(tmp_path, dirname, skill_md)
    result = _run_as_dispatched(skill_md, skill_dir, cwd=tmp_path)
    assert result.returncode == 0, (
        f"{skill_md}: a legitimate path containing {dirname!r} was rejected.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    landed = Path(result.stdout.strip().splitlines()[-1]).resolve()
    assert landed == skill_dir.parents[1].resolve()
# ---------------------------------------------------------------------------
# Stale-path guard for operator-facing docs (PR #75 review, non-blocking 2)
# ---------------------------------------------------------------------------
# The skills are fixed, but an operator following a stale runbook reintroduces
# the same breakage by hand. `platform/email-agent/SETUP.md` still carried
# pre-move `~/src/fieldkit` paths three weeks after the move, including a
# crontab template that would have registered a broken cron entry.

_REPO_ROOT = _PLATFORM_DIR.parent
_STALE_PATHS = ("~/src/fieldkit", "/Users/sandeep_a_k/src/fieldkit", "${HOME}/src/fieldkit")

# Dated records, deliberately exempt: these document what was configured or
# run at a specific past date, and the numbered Hermes docs are cited
# elsewhere as evidence of past verification runs. Rewriting their transcripts
# would falsify the record, so each instead carries a pointer to doc 12, the
# single current location. Nothing new belongs on this list — a new doc with a
# stale path is a test failure, which is the point.
_DATED_RECORDS = {
    "platform/docs/cross-review.md",
    "platform/docs/hermes/03-process-photos-skill.md",
    "platform/docs/hermes/04-check-approval-skill.md",
    "platform/docs/hermes/05-cron-verification.md",
    "platform/docs/hermes/06-openclaw-removal.md",
    "platform/docs/hermes/08-check-email-skill.md",
    "platform/docs/hermes/10-text-based-approval-migration.md",
}


def _current_docs() -> list[Path]:
    """Every markdown doc that is current instructions rather than history."""
    docs = []
    for path in sorted(_REPO_ROOT.rglob("*.md")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        # .specify/ holds per-feature spec history; .worktrees/ holds other
        # checkouts of this same repo.
        if ".specify/" in rel or rel.startswith(".worktrees/") or "/.worktrees/" in rel:
            continue
        if rel in _DATED_RECORDS:
            continue
        docs.append(path)
    return docs


def test_current_docs_are_not_discoverable_as_empty():
    docs = _current_docs()
    assert len(docs) > 10, f"doc discovery looks broken, found {len(docs)}"


def test_no_stale_repo_path_in_current_docs():
    """An operator-facing doc must not name the pre-move checkout location."""
    offenders = []
    for path in _current_docs():
        text = path.read_text(encoding="utf-8", errors="replace")
        for stale in _STALE_PATHS:
            if stale in text:
                line = text[: text.index(stale)].count("\n") + 1
                offenders.append(
                    f"{path.relative_to(_REPO_ROOT)}:{line} contains {stale!r}"
                )
    assert not offenders, (
        "Stale pre-move repo paths in current operator docs — an operator "
        "following these reintroduces issue #74 by hand:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse a path derived at run time (e.g. `$(git rev-parse "
        "--show-toplevel)` or an exported $FIELDKIT_ROOT). If the file is a "
        "dated record rather than current instructions, add it to "
        "_DATED_RECORDS with a pointer to doc 12."
    )


def test_dated_records_point_at_the_current_location():
    """Every exempt file must still exist, so the list cannot rot silently."""
    for rel in sorted(_DATED_RECORDS):
        assert (_REPO_ROOT / rel).is_file(), (
            f"_DATED_RECORDS lists {rel}, which no longer exists — remove it "
            f"from the exemption list."
        )
