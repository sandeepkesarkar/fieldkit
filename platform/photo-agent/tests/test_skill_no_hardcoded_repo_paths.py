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


def _resolution_only(block: str, invocation: str = "pwd") -> str:
    """The block with its real script invocation replaced by *invocation*.

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
            lines.append(invocation)
        else:
            lines.append(line)
    script = "\n".join(lines)
    assert invocation in script, "script invocation line not found for substitution"
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
    # The real SKILL.md, so the same fake checkout can be driven either through
    # the substitution model or through Hermes's own builder (mechanism=...).
    (skill_dir / "SKILL.md").write_text(
        skill_md.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return skill_dir


def _run_as_dispatched(
    skill_md: Path,
    skill_dir: Path,
    cwd: Path,
    invocation: str = "pwd",
    mechanism: str = "model",
):
    """Produce the dispatch block for *skill_dir* and run it.

    Two mechanisms, because the distinction matters to what a test may claim:

    * ``"model"`` — replace the bare `${HERMES_SKILL_DIR}` token textually,
      which is exactly what `substitute_template_vars` does, and is what makes
      the result shell source in the first place. Hermetic: no Hermes needed.
    * ``"hermes"`` — call the installed Hermes's real `_build_skill_message()`
      and take the block it produces. Slower and skipped when Hermes is
      absent, but it pins live dispatch behaviour rather than a model of it.
    """
    if mechanism == "hermes":
        script = _resolution_only(_dispatch_block_via_hermes(skill_dir), invocation)
    else:
        script = _resolution_only(_dispatch_block(skill_md), invocation)
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
# Heredoc delimiter and newline payloads (PR #75 round-3 review, blocking 1)
# ---------------------------------------------------------------------------
# A quoted heredoc stops expanding, but it still TERMINATES: a pasted pathname
# containing a newline followed by a line exactly equal to the delimiter ends
# the heredoc early, and the rest of the pathname becomes shell source that
# runs before any guard can inspect it.
#
# That is not fixable in bash, and this suite should not pretend otherwise.
# Every bash quoting construct has a finite, known terminator — `"` for double
# quotes, `'` for single quotes and $'...', a newline for a comment, a
# delimiter line for a heredoc — and a pathname may contain any printable
# text, since the only bytes a path cannot hold are `/` within a component and
# NUL, while no heredoc delimiter can contain NUL. So for every construct
# there exists a pathname that escapes it. The airtight fix is not ours to
# make: it would be Hermes passing the skill directory as an environment
# variable (data) instead of pasting it into the skill body (source).
#
# What IS in our control, and what these tests pin:
#   * the delimiter must not be a guessable or obvious string, so realistic
#     and accidental collisions are nil;
#   * a newline alone must fail closed;
#   * delimiter text inside an ordinary path component must resolve normally.
#
# What is NOT bounded, and must not be described as if it were: once the
# delimiter collides, the remainder of the pathname is ARBITRARY SHELL SOURCE.
# It can bypass every guard below, suppress the ERROR output and exit 0 —
# `test_delimiter_collision_permits_arbitrary_shell_source` demonstrates
# exactly that. An earlier version of this file claimed the damage stayed
# bounded to the injected text; that was true only of the one `touch` payload
# it happened to test, and false for the class.
#
# Severity bound, stated rather than implied: the path comes from
# operator-configured `skills.external_dirs`, so reaching any of this requires
# someone who can already write the Hermes config — i.e. who already has code
# execution as that user.

# The obvious delimiter name, and the one this repo used before round 3. A
# payload carrying it must not escape: that is what makes "improbable
# delimiter" a property under test rather than a claim in a comment.
_GUESSABLE_DELIMITER = "FIELDKIT_SKILL_DIR_EOF"

# Written by an injected `touch` if the pasted path ever becomes shell source.
# A filesystem sentinel rather than a marker in the output, because the guards
# legitimately echo the offending path back, which would make any
# string-matching check on stdout self-triggering.
_SENTINEL = "PWNED"

_REACHED = "FIELDKIT_REACHED_INVOCATION"


def _heredoc_delimiter(block: str) -> str:
    match = re.search(r"read -r SKILL_DIR <<'([A-Za-z0-9_]+)'", block)
    assert match, "no `read -r SKILL_DIR <<'DELIM'` heredoc found in the block"
    return match.group(1)


def _payload(delimiter: str, *injected: str) -> str:
    """A directory name that terminates *delimiter* early, then runs *injected*.

    The trailing `#` comments out the remainder of the pathname Hermes pastes
    after the skill directory, so the injected lines stand alone.
    """
    lines = "\n".join(injected)
    return f"repo\n{delimiter}\n{lines}\n#"


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
def test_guessable_heredoc_delimiter_does_not_escape(tmp_path, skill_md):
    """A pathname carrying the OBVIOUS delimiter name must not break out.

    Round 2 used `FIELDKIT_SKILL_DIR_EOF` verbatim, so this exact payload
    terminated the heredoc and executed. The delimiter is now long and
    improbable, which is the practical mitigation — and this test fails if
    anyone shortens it back to something guessable.
    """
    skill_dir = _fake_checkout(
        tmp_path, _payload(_GUESSABLE_DELIMITER, f"touch {_SENTINEL}"), skill_md
    )
    result = _run_as_dispatched(skill_md, skill_dir, cwd=tmp_path)
    combined = result.stdout + result.stderr

    assert not (tmp_path / _SENTINEL).exists(), (
        f"{skill_md}: a pathname containing the line {_GUESSABLE_DELIMITER!r} "
        f"terminated the heredoc and EXECUTED the rest of the path. The "
        f"delimiter must not be a guessable string.\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert result.returncode != 0 and "ERROR" in combined, (
        f"{skill_md}: did not fail closed.\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
def test_newline_without_delimiter_line_fails_closed(tmp_path, skill_md):
    """A newline alone truncates the read and must abort, running nothing."""
    skill_dir = _fake_checkout(tmp_path, f"repo\ntouch {_SENTINEL}", skill_md)
    result = _run_as_dispatched(skill_md, skill_dir, cwd=tmp_path)
    combined = result.stdout + result.stderr

    assert not (tmp_path / _SENTINEL).exists(), (
        f"{skill_md}: text after a newline in the pathname executed.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert result.returncode != 0 and "ERROR" in combined, (
        f"{skill_md}: a truncated skill directory did not fail closed.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
def test_delimiter_text_inside_a_path_component_resolves(tmp_path, skill_md):
    """The delimiter as a SUBSTRING of an ordinary component is harmless.

    Only a whole line equal to the delimiter terminates a heredoc, so this
    must keep resolving normally rather than being rejected out of caution.
    """
    delimiter = _heredoc_delimiter(_dispatch_block(skill_md))
    skill_dir = _fake_checkout(tmp_path, f"repo{delimiter}x", skill_md)
    result = _run_as_dispatched(skill_md, skill_dir, cwd=tmp_path)
    assert result.returncode == 0, (
        f"{skill_md}: rejected a legitimate path containing the delimiter as "
        f"a substring.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    landed = Path(result.stdout.strip().splitlines()[-1]).resolve()
    assert landed == skill_dir.parents[1].resolve()


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
@pytest.mark.parametrize("mechanism", ["model", "hermes"])
def test_delimiter_collision_permits_arbitrary_shell_source(
    tmp_path, skill_md, mechanism
):
    """Pin the residual as it actually is: a full guard bypass is possible.

    This test asserts the LIMITATION, not a guarantee. Once a pathname
    terminates the heredoc, its remainder is arbitrary shell source, so it can
    do anything a shell can — including the three things the guards below are
    otherwise responsible for preventing: skipping every check, producing no
    ERROR output, and exiting 0.

    The payload is the reviewer's: `cd ..` then `exit 0`. It short-circuits the
    block before any guard runs, which is why no bound on the blast radius can
    be claimed. An earlier revision of this test asserted the opposite —
    non-zero exit, ERROR reported, invocation not reached — which held only
    because its own payload was a `touch` that did not alter control flow. The
    claim did not generalise, and the accepted risk is documented in
    platform/docs/hermes/12-skill-path-resolution.md instead.

    Runs under both mechanisms, so the disclosure can name the one that backs
    it: ``model`` always runs, and ``hermes`` drives the installed Hermes's
    real `_build_skill_message()` so the documented behaviour is pinned as
    live dispatch behaviour, not only as a property of the substitution model.

    If this test FAILS, do not assume the limitation has been fixed: it may
    have been closed upstream (for instance Hermes passing the skill directory
    as an environment variable), OR a regression may have changed this block's
    behaviour — check the other tests in this file before concluding which. If
    it really is fixed, update the disclosure in doc 12 and the PR body to
    match rather than deleting this test.
    """
    if mechanism == "hermes" and not _HERMES_PYTHON.is_file():
        pytest.skip("Hermes is not installed on this machine")

    delimiter = _heredoc_delimiter(_dispatch_block(skill_md))
    skill_dir = _fake_checkout(
        tmp_path, _payload(delimiter, "cd ..", "exit 0"), skill_md
    )

    result = _run_as_dispatched(
        skill_md,
        skill_dir,
        cwd=tmp_path,
        invocation=f"echo {_REACHED}",
        mechanism=mechanism,
    )
    combined = result.stdout + result.stderr

    assert result.returncode == 0, (
        f"{skill_md}: expected the documented bypass (exit 0) but got exit "
        f"{result.returncode} — if the delimiter collision no longer yields "
        f"arbitrary shell source, update the disclosure in doc 12 and the PR "
        f"body.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ERROR" not in combined, (
        f"{skill_md}: expected the injected source to suppress all guard "
        f"output; it reported an ERROR instead. Re-check the disclosure.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert _REACHED not in result.stdout, (
        f"{skill_md}: the injected `exit 0` should have short-circuited the "
        f"block before dispatch.\nstdout: {result.stdout}"
    )


# ---------------------------------------------------------------------------
# One test per guard branch (PR #75 round-5 review, item 1)
# ---------------------------------------------------------------------------
# Rounds 1–4 of this review kept finding claims that outran their tests. Both
# round-5 reviewers then found the inverse gap: several guard branches were
# correct but had no dedicated test, while the PR claimed coverage of them. The
# guards below were verified by hand; these tests make the claim true, so the
# coverage statement stands on the suite rather than on a manual check.
#
# Each case drives the block's own code — no hand-copied expectation of it.

# Every distinct abort branch, with the text that identifies it. `condition`
# builds the skill directory that triggers it, given a tmp root and the skill.
_GUARD_BRANCHES = (
    ("empty", "empty or blank"),
    ("whitespace-only", "empty or blank"),
    ("unsubstituted-placeholder", "reached the shell unsubstituted"),
    ("metacharacter", "contains a shell metacharacter"),
    ("outside-platform-parent", "not a fieldkit platform agent directory"),
    ("missing-script", "not found under"),
)


def _skill_dir_for_branch(branch: str, tmp_path: Path, skill_md: Path) -> str | None:
    """The substituted value that drives *branch*, or None to leave it raw."""
    if branch == "empty":
        return ""
    if branch == "whitespace-only":
        return "   "
    if branch == "unsubstituted-placeholder":
        return None  # leave the token in place
    if branch == "metacharacter":
        return str(_fake_checkout(tmp_path, "repo$USER", skill_md))
    if branch == "outside-platform-parent":
        # Mirrors a skill copied into Hermes's own skills directory, where two
        # levels up is the Hermes profile rather than an agent directory.
        hermes_skill = tmp_path / ".hermes" / "skills" / skill_md.parent.name
        hermes_skill.mkdir(parents=True, exist_ok=True)
        return str(hermes_skill)
    if branch == "missing-script":
        # Correct layout, but scripts/ is absent — a mispointed external_dirs.
        skill_dir = tmp_path / "repo" / "platform" / skill_md.parents[2].name / "skills" / skill_md.parent.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        return str(skill_dir)
    raise AssertionError(f"unknown branch {branch!r}")


def _run_branch(skill_md: Path, tmp_path: Path, branch: str):
    substitute = _skill_dir_for_branch(branch, tmp_path, skill_md)
    script = _resolution_only(_dispatch_block(skill_md))
    if substitute is not None:
        script = script.replace(_SKILL_DIR_TOKEN, substitute)
    return subprocess.run(
        ["/bin/bash", "-c", script],
        env={"PATH": "/usr/bin:/bin"},
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
@pytest.mark.parametrize(
    "branch,expected_text",
    _GUARD_BRANCHES,
    ids=[b for b, _ in _GUARD_BRANCHES],
)
def test_each_guard_branch_aborts_with_its_own_diagnostic(
    tmp_path, skill_md, branch, expected_text
):
    """Each abort branch must fire, exit 1, and say which condition it was.

    Covers items (a), (b) and (c) of the round-5 list — empty/whitespace-only,
    resolution outside a `platform/` parent, and a missing dispatched script —
    alongside the two branches earlier rounds already exercised, so all six
    live in one matrix rather than being covered unevenly.
    """
    result = _run_branch(skill_md, tmp_path, branch)
    combined = result.stdout + result.stderr

    assert result.returncode == 1, (
        f"{skill_md}: branch {branch!r} exited {result.returncode}, expected 1"
        f"\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert expected_text in combined, (
        f"{skill_md}: branch {branch!r} did not report its own diagnostic "
        f"({expected_text!r}) — a misidentified cause sends the operator to "
        f"the wrong setting.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
@pytest.mark.parametrize(
    "branch", [b for b, _ in _GUARD_BRANCHES], ids=[b for b, _ in _GUARD_BRANCHES]
)
def test_every_guard_branch_reports_one_error_line(tmp_path, skill_md, branch):
    """Pins doc 12's statement that a guard prints exactly ONE `ERROR:` line.

    That document previously claimed every failure also "names the Hermes
    setting involved", which is false — only the two configuration-caused
    branches do, and the statement has been narrowed to match. This test pins
    the part that is true of all of them.
    """
    result = _run_branch(skill_md, tmp_path, branch)
    combined = result.stdout + result.stderr
    error_lines = [l for l in combined.splitlines() if l.startswith("ERROR:")]

    assert len(error_lines) == 1, (
        f"{skill_md}: branch {branch!r} printed {len(error_lines)} ERROR "
        f"lines, expected exactly 1.\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


_SETTING_NAMING_BRANCHES = {
    "unsubstituted-placeholder": "skills.template_vars",
    "missing-script": "skills.external_dirs",
}


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
@pytest.mark.parametrize(
    "branch,setting",
    sorted(_SETTING_NAMING_BRANCHES.items()),
    ids=sorted(_SETTING_NAMING_BRANCHES),
)
def test_configuration_branches_name_the_hermes_setting(
    tmp_path, skill_md, branch, setting
):
    """The two branches doc 12 says name a setting must actually name it."""
    result = _run_branch(skill_md, tmp_path, branch)
    combined = result.stdout + result.stderr
    assert setting in combined, (
        f"{skill_md}: branch {branch!r} should point the operator at "
        f"{setting!r}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
def test_path_containing_the_placeholder_name_is_not_mistaken_for_it(
    tmp_path, skill_md
):
    """Item (d): a real path may contain the text HERMES_SKILL_DIR.

    The guard compares against the literal `${HERMES_SKILL_DIR}` placeholder,
    not a substring, so a checkout that happens to include that name resolves
    normally. An earlier revision matched `*HERMES_SKILL_DIR*` and would have
    misreported this as "template_vars is disabled" — a wrong diagnosis that
    sends the operator to change a setting that was never the problem.
    """
    skill_dir = _fake_checkout(tmp_path, "HERMES_SKILL_DIR_repo", skill_md)
    result = _run_as_dispatched(skill_md, skill_dir, cwd=tmp_path)

    assert result.returncode == 0, (
        f"{skill_md}: a legitimate path containing the placeholder's NAME was "
        f"rejected.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "unsubstituted" not in (result.stdout + result.stderr), (
        f"{skill_md}: misdiagnosed a real path as an unsubstituted "
        f"placeholder.\nstdout: {result.stdout}"
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


def _tracked_markdown() -> list[str]:
    """Repo-relative paths of every git-TRACKED markdown file.

    Deliberately `git ls-files` rather than a filesystem walk. A walk also
    picks up whatever untracked directories happen to sit in a developer's
    checkout — `.claude/`, `.worktrees/`, scratch copies of these very docs —
    each carrying its own stale paths, which turned this test red for reasons
    that had nothing to do with the repo's contents. Only tracked files are
    this repo's responsibility, so only tracked files are asserted on.
    """
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "--", "*.md"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git ls-files failed: {result.stderr}"
    return [p for p in result.stdout.split("\0") if p]


def _current_docs() -> list[Path]:
    """Every tracked markdown doc that is current instructions, not history."""
    docs = []
    for rel in sorted(_tracked_markdown()):
        # .specify/ holds per-feature spec history.
        if ".specify/" in rel:
            continue
        if rel in _DATED_RECORDS:
            continue
        docs.append(_REPO_ROOT / rel)
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


def test_dated_record_exemptions_still_exist():
    """Every exempt file must still exist, so the list cannot rot silently.

    Named for what it actually checks: existence only. Whether a record points
    readers at the current location is a separate property, asserted below for
    the records that carry a stale config snippet.
    """
    for rel in sorted(_DATED_RECORDS):
        assert (_REPO_ROOT / rel).is_file(), (
            f"_DATED_RECORDS lists {rel}, which no longer exists — remove it "
            f"from the exemption list."
        )


# Dated records whose stale text is a `skills.external_dirs` snippet a reader
# might copy, as opposed to a crontab transcript or an incident write-up.
# These must visibly redirect to the current location; the others need no
# pointer because there is nothing in them to copy.
_RECORDS_WITH_STALE_CONFIG_SNIPPETS = (
    "platform/docs/hermes/03-process-photos-skill.md",
    "platform/docs/hermes/04-check-approval-skill.md",
    "platform/docs/hermes/08-check-email-skill.md",
)

_CURRENT_LOCATION_DOC = "platform/docs/hermes/12-skill-path-resolution.md"


def test_stale_config_snippets_point_at_the_current_location():
    """A reader landing on a stale config snippet must be redirected."""
    assert (_REPO_ROOT / _CURRENT_LOCATION_DOC).is_file(), (
        f"{_CURRENT_LOCATION_DOC} is missing — it is the single current "
        f"location every stale snippet points at."
    )
    for rel in _RECORDS_WITH_STALE_CONFIG_SNIPPETS:
        assert rel in _DATED_RECORDS, f"{rel} should be a dated record"
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "12-skill-path-resolution.md" in text, (
            f"{rel} still shows a stale skills.external_dirs snippet but no "
            f"longer points readers at {_CURRENT_LOCATION_DOC} — a reader will "
            f"copy the stale paths."
        )


def test_doc12_names_only_tests_that_exist():
    """Doc 12's "which test pins which statement" table must not rot.

    That table is the durable form of this PR's claim audit: every behavioural
    statement in the doc names the test demonstrating it. A renamed or deleted
    test would silently turn an entry into a dangling reference, which is the
    same failure mode — a claim with nothing behind it — in a new disguise.
    """
    doc = _REPO_ROOT / _CURRENT_LOCATION_DOC
    named = set(re.findall(r"`(test_\w+)`", doc.read_text(encoding="utf-8")))
    assert named, f"{_CURRENT_LOCATION_DOC} names no tests — did the table move?"

    defined = set(re.findall(r"^def (test_\w+)", Path(__file__).read_text(encoding="utf-8"), re.M))
    dangling = named - defined
    assert not dangling, (
        f"{_CURRENT_LOCATION_DOC} points at tests that no longer exist here: "
        f"{sorted(dangling)}. Update the table, or restore the tests."
    )


# ---------------------------------------------------------------------------
# Integration: Hermes's own message builder (PR #75 round-3 review, suggestion)
# ---------------------------------------------------------------------------
# Everything above models Hermes's substitution with `str.replace()`, which is
# a faithful model of the primitive (`substitute_template_vars` performs a
# plain textual replacement of the bare token) and keeps the suite hermetic.
# This test closes the remaining gap: it drives the installed Hermes's real
# `_build_skill_message()` — the function `build_skill_invocation_message()`
# calls on the Telegram slash-command path — and executes what it produces, so
# live dispatch behaviour is pinned by the suite instead of by manual
# verification each review round.
#
# It runs inside Hermes's own venv as a subprocess, so Hermes's imports never
# touch the pytest process and the test does not depend on the test
# interpreter having Hermes's dependencies. Skipped when Hermes is not
# installed, which keeps the suite runnable on a dev machine.

_HERMES_AGENT_DIR = Path.home() / ".hermes" / "hermes-agent"
_HERMES_PYTHON = _HERMES_AGENT_DIR / "venv" / "bin" / "python"

# Printed around the block so the harness can find it in the subprocess output.
_BLOCK_START = "===FIELDKIT_BLOCK_START==="
_BLOCK_END = "===FIELDKIT_BLOCK_END==="

_EXTRACT_VIA_HERMES = '''
import re, sys
sys.path.insert(0, sys.argv[1])
from agent.skill_commands import _build_skill_message
from pathlib import Path

skill_dir = Path(sys.argv[2])
loaded = {"content": (skill_dir / "SKILL.md").read_text(), "name": skill_dir.name}
message = _build_skill_message(
    loaded, skill_dir, activation_note="[test]", session_id="test-session"
)
block = next(
    b for b in re.findall(r"```bash\\n(.*?)```", message, re.DOTALL)
    if re.search(r"python3 scripts/\\S+\\.py", b)
)
print(sys.argv[3])
print(block)
print(sys.argv[4])
'''


def _dispatch_block_via_hermes(skill_dir: Path) -> str:
    """The dispatch block Hermes's own builder produces for *skill_dir*.

    Reads `SKILL.md` from *skill_dir*, so it works for the real skills and for
    a fake checkout built by `_fake_checkout` alike. Requires Hermes; callers
    must skip when `_HERMES_PYTHON` is absent.
    """
    extract = subprocess.run(
        [
            str(_HERMES_PYTHON), "-c", _EXTRACT_VIA_HERMES,
            str(_HERMES_AGENT_DIR), str(skill_dir), _BLOCK_START, _BLOCK_END,
        ],
        capture_output=True,
        text=True,
    )
    assert extract.returncode == 0, (
        f"could not build the dispatch message via Hermes for {skill_dir}:\n"
        f"{extract.stderr}"
    )
    return extract.stdout.split(_BLOCK_START, 1)[1].split(_BLOCK_END, 1)[0].strip("\n")


@pytest.mark.skipif(
    not _HERMES_PYTHON.is_file(), reason="Hermes is not installed on this machine"
)
@pytest.mark.parametrize("skill_md", _SKILL_MDS, ids=_ids)
def test_real_hermes_builder_substitutes_and_resolves(skill_md):
    """Hermes's real builder must substitute the token, and the block it
    produces must execute and land on this checkout's agent directory."""
    block = _dispatch_block_via_hermes(skill_md.parent)

    # Hermes really did the substitution: no placeholder survives into the
    # content the agent receives.
    assert _SKILL_DIR_TOKEN not in block, (
        f"{skill_md}: Hermes did not substitute {_SKILL_DIR_TOKEN} — is "
        f"skills.template_vars disabled in the local Hermes config?"
    )
    assert str(skill_md.parent) in block, (
        f"{skill_md}: the substituted block does not contain this skill's own "
        f"directory."
    )

    result = _run(_resolution_only(block))
    assert result.returncode == 0, (
        f"{skill_md}: the block Hermes produced failed to resolve with exit "
        f"{result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    landed = Path(result.stdout.strip().splitlines()[-1]).resolve()
    assert landed == skill_md.resolve().parents[2], (
        f"{skill_md}: Hermes's own output resolved to {landed}, expected "
        f"{skill_md.resolve().parents[2]}"
    )
