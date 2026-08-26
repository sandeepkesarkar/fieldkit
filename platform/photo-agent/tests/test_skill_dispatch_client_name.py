"""
Tests for issue #59 — CLIENT_NAME resolution under the live Hermes
skill-dispatch path (an admin typing /process_photos in a client's own
Telegram bot, routed through that client's Hermes profile), as opposed to
the cron/manual-invocation path issue #45/PR #57 already covers.

Root cause: platform/photo-agent/skills/*/SKILL.md shell out to the
photo-agent scripts with NO env-var prefix and no --client flag at all --
entirely unlike a crontab line's `env CLIENT_NAME=<client> python3 ...`
(test_client_name_override.py's test_preset_client_name_env_var_wins_over_root_env
covers that inline-override shape). These skills instead depend entirely on
CLIENT_NAME already being present in the Hermes gateway process's own
environment before it spawns the subprocess that runs the skill's bash
block. Verified directly against this machine's installed Hermes
(~/.hermes/hermes-agent):

  - hermes_cli/env_loader.py::load_hermes_dotenv() loads <HERMES_HOME>/.env
    (~/.hermes/profiles/<client>/.env for a named profile) into the gateway
    process's os.environ with override=True, at startup and again every
    turn for a non-multiplexed profile (this Mac Mini runs one gateway
    process per named profile, not a shared multiplexed one).
  - tools/environments/local.py builds a skill-invoked subprocess's
    environment as os.environ.copy(), stripping only a specific named
    blocklist of Hermes-internal/provider-credential keys. CLIENT_NAME is a
    plain custom key on neither list, so it passes through untouched.

So the fix is a per-profile .env entry (CLIENT_NAME=<client>), documented in
platform/docs/hermes/09-per-client-model-profiles.md -- a file outside this
repo (per-machine, gitignored, holds live secrets) that can't be asserted
on directly here. What this test file DOES assert, and is the actual gap
issue #59 found (PR #57's tests never covered it):

  1. The SKILL.md files really do contain no inline CLIENT_NAME override --
     confirming they depend entirely on that ambient, profile-managed
     value, rather than the crontab-style inline override #57 tested.
  2. When that ambient value is genuinely absent (the pre-fix state -- a
     profile whose .env has never had CLIENT_NAME added), running
     process_photos.py exactly as SKILL.md instructs -- real entrypoint,
     real argv, zero inline override -- silently resolves to the wrong
     client and writes its activity-log COMMAND line into THAT client's
     log directory. This is the literal mechanism behind the 5 live
     /process_photos invocations that wrote to
     clients/_demo/logs/photo-agent.log instead of mercury's.
  3. When that ambient value IS present (the post-fix state -- simulating a
     profile .env whose CLIENT_NAME was already loaded into the parent
     process's environment before the subprocess spawns, exactly matching
     the verified Hermes mechanism above), the identical invocation
     resolves to the correct client and writes to the correct log.

This is deliberately not a re-run of test_client_name_override.py's
test_preset_client_name_env_var_wins_over_root_env: that test only checks
the module's resolved _CLIENT attribute via a hand-written `python -c`
import snippet. This test drives the script's real entrypoint (the same
argv shape SKILL.md instructs) and checks the real side effect
(activity_log.log_command()'s file write) that a Telegram admin actually
observes -- proving the fix closes the gap end-to-end, not just at the
module-attribute level.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PLATFORM_PHOTO_AGENT = Path(__file__).parents[1]
_SKILLS_DIR = _PLATFORM_PHOTO_AGENT / "skills"

_SKILL_FILES = {
    "process-photos": _SKILLS_DIR / "process-photos" / "SKILL.md",
    "photo-approve": _SKILLS_DIR / "photo-approve" / "SKILL.md",
    "photo-reject": _SKILLS_DIR / "photo-reject" / "SKILL.md",
}


def _extract_bash_blocks(skill_md: Path) -> list[str]:
    """Return the contents of every ```bash fenced block in a SKILL.md file."""
    text = skill_md.read_text()
    blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    assert blocks, f"{skill_md}: no ```bash block found"
    return blocks


# ---------------------------------------------------------------------------
# Contract lock: the skills genuinely have no inline CLIENT_NAME mechanism
# of their own -- this is the assumption the profile-.env-based fix depends
# on. If a future change adds one here instead, this test should be updated
# deliberately, not silently left stale while the doc still claims otherwise.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("skill_name, skill_md", sorted(_SKILL_FILES.items()))
def test_skill_invocation_sets_no_inline_client_name(skill_name, skill_md):
    """Each SKILL.md's shell-out command(s) must set no CLIENT_NAME inline --
    confirming these skills rely entirely on CLIENT_NAME already being
    present in the ambient, Hermes-profile-managed environment."""
    for block in _extract_bash_blocks(skill_md):
        assert "CLIENT_NAME" not in block, (
            f"{skill_md} now sets CLIENT_NAME inline in a shell-out command -- "
            "if this is an intentional alternative fix for issue #59, update "
            "this test and the profile-.env documentation to match"
        )


# ---------------------------------------------------------------------------
# End-to-end: run process_photos.py exactly as process-photos/SKILL.md
# instructs (real entrypoint, real argv, no env-var prefix), under two
# environments -- CLIENT_NAME ambiently absent (bug) vs. ambiently present
# (fix) -- and check which client's activity log actually receives the
# COMMAND line. No live Drive/Telegram/ffmpeg access is needed: the script
# calls activity_log.log_command() immediately after argument validation,
# then exits non-zero at the very next line (a missing
# ADMIN_TELEGRAM_CHAT_ID) before ever touching Drive.
# ---------------------------------------------------------------------------

def _skill_dispatch_argv(project_name: str) -> list[str]:
    """The exact argv process-photos/SKILL.md instructs (minus `timeout N`,
    a shell-level wrapper with no bearing on env/argv resolution) -- parsed
    from the SKILL.md file itself rather than hand-written, so a change to
    the real invocation shape breaks this test loudly instead of silently
    testing a stale command."""
    blocks = _extract_bash_blocks(_SKILL_FILES["process-photos"])
    run_block = next(b for b in blocks if "process_photos.py" in b)
    run_line = next(
        line for line in run_block.splitlines() if "process_photos.py" in line
    )
    assert run_line.strip().startswith("timeout "), (
        f"unexpected process-photos SKILL.md invocation line: {run_line!r}"
    )
    assert "CLIENT_NAME" not in run_line
    return [sys.executable, "scripts/process_photos.py", "--project", project_name]


def _write_client_env(root: Path, client: str, *, data_dir: Path, log_dir: Path) -> None:
    client_dir = root / "clients" / client / "src" / "photo-agent"
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / ".env").write_text(
        f"FIELDKIT_DATA_DIR={data_dir}\nFIELDKIT_LOG_DIR={log_dir}\n"
    )


def _run_skill_dispatch(tmp_path: Path, *, ambient_client_name: str | None) -> subprocess.CompletedProcess:
    """Simulate a live skill-dispatch invocation of process_photos.py: the
    subprocess env is built exactly like Hermes's terminal-tool spawn path
    (tools/environments/local.py: os.environ.copy(), no per-invocation
    override) -- CLIENT_NAME is either already present (post-fix profile
    .env) or absent (pre-fix, nothing sets it) in that copied env, and the
    invoked command itself carries no CLIENT_NAME of its own, matching
    SKILL.md exactly."""
    root_env = tmp_path / ".env"
    root_env.write_text("CLIENT_NAME=_demo\n")

    _write_client_env(
        tmp_path, "_demo",
        data_dir=tmp_path / "data" / "_demo", log_dir=tmp_path / "logs" / "_demo",
    )
    _write_client_env(
        tmp_path, "mercury",
        data_dir=tmp_path / "data" / "mercury", log_dir=tmp_path / "logs" / "mercury",
    )

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "FIELDKIT_ROOT": str(tmp_path),
    }
    if ambient_client_name is not None:
        env["CLIENT_NAME"] = ambient_client_name

    argv = _skill_dispatch_argv("manual-e2e-regression-test")
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, cwd=str(_PLATFORM_PHOTO_AGENT),
    )


def test_skill_dispatch_with_no_ambient_client_name_silently_hits_wrong_client(tmp_path):
    """THE BUG: a live skill dispatch for mercury, with mercury's profile
    .env never having set CLIENT_NAME (today's pre-fix state on this
    machine, per issue #59), silently resolves to the root .env's client
    (_demo) and writes the COMMAND activity-log line into _demo's log
    directory -- reproducing exactly what happened live on 2026-08-26."""
    result = _run_skill_dispatch(tmp_path, ambient_client_name=None)

    demo_log = tmp_path / "logs" / "_demo" / "photo-agent.log"
    mercury_log = tmp_path / "logs" / "mercury" / "photo-agent.log"

    assert demo_log.exists(), result.stderr
    assert "COMMAND" in demo_log.read_text()
    assert "manual-e2e-regression-test" in demo_log.read_text()
    assert not mercury_log.exists()


def test_skill_dispatch_with_ambient_client_name_resolves_correct_client(tmp_path):
    """THE FIX, verified: with CLIENT_NAME=mercury already present in the
    ambient environment before the subprocess spawns -- simulating a
    profile .env's CLIENT_NAME having been loaded into the gateway
    process's os.environ, exactly as env_loader.py/local.py do on this
    machine's real Hermes install -- the identical skill-dispatch
    invocation (same argv, no inline override) resolves to the correct
    client and writes to the correct log, not _demo's."""
    result = _run_skill_dispatch(tmp_path, ambient_client_name="mercury")

    demo_log = tmp_path / "logs" / "_demo" / "photo-agent.log"
    mercury_log = tmp_path / "logs" / "mercury" / "photo-agent.log"

    assert mercury_log.exists(), result.stderr
    assert "COMMAND" in mercury_log.read_text()
    assert "manual-e2e-regression-test" in mercury_log.read_text()
    assert not demo_log.exists()
