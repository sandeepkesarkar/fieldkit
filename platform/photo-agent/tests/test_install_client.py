"""
Tests for platform/photo-agent/scripts/install_client.sh — the single-install
"switch active client" procedure (issue #61, replacing the concurrent
per-client-Hermes-profile model that caused issue #59).

These exercise the REAL script via subprocess, in a fully isolated sandbox:
  - FIELDKIT_ROOT and HERMES_HOME point at tmp_path subdirectories, never
    this machine's real fieldkit checkout or real ~/.hermes.
  - `hermes` and `launchctl` are shadowed by stub executables placed first
    on PATH that just log the arguments they were called with — this lets
    tests assert on exactly what the script would have told the real Hermes
    CLI to do, without depending on Hermes being installed, being fast, or
    (most importantly) ever touching this machine's real live gateway.

What these prove, concretely tied to #61's architecture:
  - The script refuses to run (fails closed, before writing anything) when
    a client's .env is missing any required Hermes-install field — no
    guessing, no partial install.
  - A successful install writes CLIENT_NAME into the root .env and the
    client's Telegram token/allowlist + provider API key into Hermes's
    DEFAULT profile .env, and drives `hermes config set` for
    model.provider/model.default/skills.external_dirs against that same
    default profile (never a named one) — this is the actual mechanism
    that makes CLIENT_NAME's fallback-to-root-.env behavior always correct:
    there is only ever one client's config installed system-wide.
  - Re-running the install (switching, or re-installing the same client)
    is idempotent — upserts, not duplicate lines — proving there's exactly
    one CLIENT_NAME value and one copy of each Hermes field at all times,
    never a growing file with stale/conflicting old values.
  - Switching FROM one client TO another correctly replaces the prior
    client's values rather than merging with them — this is the direct
    proof that issue #59's failure mode (two clients' config coexisting,
    with the "wrong" one winning silently) cannot occur under this model.
  - An unrecognized model provider fails loudly rather than silently
    installing no API key (which would have looked identical to a working
    install right up until the first live skill dispatch).
  - Leftover non-default Hermes profile directories (the pre-#61 mercury/
    venus setup) are detected and surfaced as human-run retirement
    commands, never touched by the script itself.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_PLATFORM_PHOTO_AGENT = Path(__file__).parents[1]
_INSTALL_SCRIPT = _PLATFORM_PHOTO_AGENT / "scripts" / "install_client.sh"


def _write_stub(path: Path, log_path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "{path.name.upper()}_CALL: $*" >> "{log_path}"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def sandbox(tmp_path):
    """A fully isolated FIELDKIT_ROOT/HERMES_HOME/PATH sandbox for one test."""
    fieldkit_root = tmp_path / "fieldkit"
    hermes_home = tmp_path / "hermes"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    fieldkit_root.mkdir()
    hermes_home.mkdir()

    log_path = tmp_path / "stub.log"
    _write_stub(stub_bin / "hermes", log_path)
    _write_stub(stub_bin / "launchctl", log_path)

    env = {
        "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", ""),
        "FIELDKIT_ROOT": str(fieldkit_root),
        "HERMES_HOME": str(hermes_home),
    }
    return {
        "fieldkit_root": fieldkit_root,
        "hermes_home": hermes_home,
        "log_path": log_path,
        "env": env,
    }


def _write_client_env(fieldkit_root: Path, client: str, overrides: dict | None = None) -> Path:
    fields = {
        "TELEGRAM_BOT_TOKEN": "1234:token",
        "TELEGRAM_ALLOWED_USERS": "999888777",
        "HERMES_MODEL_PROVIDER": "anthropic",
        "HERMES_MODEL_DEFAULT": "claude-sonnet-5",
        "HERMES_PROVIDER_API_KEY": "sk-ant-test-key",
        # Not read or validated by install_client.sh -- present so a
        # subprocess importing the real process_photos.py against this
        # client's .env doesn't raise on FIELDKIT_DATA_DIR/LOG_DIR being
        # unset (tools/state.py, tools/logger.py require them at import
        # time). See test_installed_client_resolves_via_real_process_photos.
        "FIELDKIT_DATA_DIR": str(fieldkit_root / "clients" / client / "data"),
        "FIELDKIT_LOG_DIR": str(fieldkit_root / "clients" / client / "logs"),
    }
    if overrides:
        fields.update(overrides)
    client_dir = fieldkit_root / "clients" / client / "src" / "photo-agent"
    client_dir.mkdir(parents=True, exist_ok=True)
    env_path = client_dir / ".env"
    env_path.write_text(
        "\n".join(f"{k}={v}" for k, v in fields.items() if v is not None) + "\n"
    )
    return env_path


def _run(client: str, sandbox: dict, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_INSTALL_SCRIPT), client, *extra_args],
        env=sandbox["env"],
        capture_output=True,
        text=True,
    )


def _log_calls(sandbox: dict) -> list[str]:
    if not sandbox["log_path"].exists():
        return []
    return sandbox["log_path"].read_text().splitlines()


def test_missing_client_env_fails_closed_before_writing_anything(sandbox):
    """No clients/<name>/.../.env at all: refuse, don't touch root .env or Hermes."""
    result = _run("nosuchclient", sandbox)
    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()


@pytest.mark.parametrize(
    "missing_field",
    [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USERS",
        "HERMES_MODEL_PROVIDER",
        "HERMES_MODEL_DEFAULT",
        "HERMES_PROVIDER_API_KEY",
    ],
)
def test_missing_required_field_fails_closed(sandbox, missing_field):
    """Any single required field missing refuses the whole install — no
    partial config ever gets written (e.g. CLIENT_NAME flipped but the
    Hermes side left stale/wrong, which would be its own silent-misdirection
    bug in the same shape as #59)."""
    _write_client_env(sandbox["fieldkit_root"], "acme", overrides={missing_field: None})
    result = _run("acme", sandbox)
    assert result.returncode != 0
    assert missing_field in result.stderr
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()
    assert _log_calls(sandbox) == []


def test_unrecognized_provider_fails_closed(sandbox):
    """An unmapped HERMES_MODEL_PROVIDER must not silently skip the API-key
    write or guess a variable name — it must refuse the whole install."""
    _write_client_env(
        sandbox["fieldkit_root"], "acme",
        overrides={"HERMES_MODEL_PROVIDER": "totally-unknown-provider"},
    )
    result = _run("acme", sandbox)
    assert result.returncode != 0
    assert "unrecognized HERMES_MODEL_PROVIDER" in result.stderr
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()


def test_dry_run_changes_nothing(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--dry-run")
    assert result.returncode == 0
    assert "no files written" in result.stdout
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()
    assert _log_calls(sandbox) == []


def test_successful_install_writes_root_env_and_default_hermes_profile(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox)
    assert result.returncode == 0, result.stderr

    root_env = (sandbox["fieldkit_root"] / ".env").read_text()
    assert "CLIENT_NAME=acme" in root_env

    hermes_env = (sandbox["hermes_home"] / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN=1234:token" in hermes_env
    assert "TELEGRAM_ALLOWED_USERS=999888777" in hermes_env
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in hermes_env

    calls = _log_calls(sandbox)
    assert any("profile use default" in c for c in calls)
    assert any("config set model.provider anthropic" in c for c in calls)
    assert any("config set model.default claude-sonnet-5" in c for c in calls)
    assert any("skills.external_dirs" in c for c in calls)
    # Never targets a named profile -- the whole point of the single-install
    # model is that config set always lands on the one default profile.
    assert not any("-p acme" in c or "-p " in c for c in calls)


def test_gateway_restart_uses_default_profile_launchd_label(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox)
    assert result.returncode == 0, result.stderr
    calls = _log_calls(sandbox)
    restart_calls = [c for c in calls if "LAUNCHCTL" in c]
    assert restart_calls, calls
    assert "ai.hermes.gateway" in restart_calls[0]
    # Must be the bare default-profile label, never a per-client suffix.
    assert "ai.hermes.gateway-" not in restart_calls[0]


def test_no_restart_flag_skips_gateway_restart(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    calls = _log_calls(sandbox)
    assert not any("LAUNCHCTL" in c for c in calls)


def test_reinstalling_same_client_is_idempotent_not_duplicated(sandbox):
    """Running the install twice for the same client must not grow the file
    with duplicate/conflicting lines -- exactly one CLIENT_NAME, exactly one
    copy of each Hermes field, always."""
    _write_client_env(sandbox["fieldkit_root"], "acme")
    _run("acme", sandbox)
    _write_client_env(
        sandbox["fieldkit_root"], "acme",
        overrides={"TELEGRAM_ALLOWED_USERS": "111222333"},
    )
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr

    root_env_lines = (sandbox["fieldkit_root"] / ".env").read_text().splitlines()
    assert sum(1 for line in root_env_lines if line.startswith("CLIENT_NAME=")) == 1
    assert "CLIENT_NAME=acme" in root_env_lines

    hermes_env_lines = (sandbox["hermes_home"] / ".env").read_text().splitlines()
    assert sum(1 for line in hermes_env_lines if line.startswith("TELEGRAM_ALLOWED_USERS=")) == 1
    assert "TELEGRAM_ALLOWED_USERS=111222333" in hermes_env_lines


def test_switching_clients_replaces_not_merges_prior_config(sandbox):
    """THE CORE #59 REGRESSION GUARD: installing client B after client A must
    fully replace A's values in both the root .env and Hermes's default
    profile .env -- never leave a mix where e.g. CLIENT_NAME says B but the
    Telegram token is still A's (which is exactly the shape of #59's live
    failure: one piece of config pointing at the wrong client)."""
    _write_client_env(
        sandbox["fieldkit_root"], "clienta",
        overrides={
            "TELEGRAM_BOT_TOKEN": "AAAA:tokenA",
            "TELEGRAM_ALLOWED_USERS": "111",
            "HERMES_PROVIDER_API_KEY": "sk-ant-A",
        },
    )
    result_a = _run("clienta", sandbox, "--no-restart")
    assert result_a.returncode == 0, result_a.stderr

    _write_client_env(
        sandbox["fieldkit_root"], "clientb",
        overrides={
            "TELEGRAM_BOT_TOKEN": "BBBB:tokenB",
            "TELEGRAM_ALLOWED_USERS": "222",
            "HERMES_PROVIDER_API_KEY": "sk-ant-B",
        },
    )
    result_b = _run("clientb", sandbox, "--no-restart")
    assert result_b.returncode == 0, result_b.stderr

    root_env = (sandbox["fieldkit_root"] / ".env").read_text()
    assert "CLIENT_NAME=clientb" in root_env
    assert "clienta" not in root_env

    hermes_env = (sandbox["hermes_home"] / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN=BBBB:tokenB" in hermes_env
    assert "TELEGRAM_ALLOWED_USERS=222" in hermes_env
    assert "ANTHROPIC_API_KEY=sk-ant-B" in hermes_env
    # No trace of client A's secrets survives the switch.
    assert "AAAA:tokenA" not in hermes_env
    assert "111" not in hermes_env.replace("BBBB", "")  # crude but sufficient: "111" isn't a substring of anything B wrote
    assert "sk-ant-A" not in hermes_env


def test_stale_non_default_profiles_are_surfaced_not_touched(sandbox):
    """A leftover ~/.hermes/profiles/<name> directory (pre-#61 per-client
    profile setup) must be reported as a human-run retirement command, and
    the script must never write into or delete that directory itself."""
    stale_profile_dir = sandbox["hermes_home"] / "profiles" / "mercury"
    stale_profile_dir.mkdir(parents=True)
    (stale_profile_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=stale-should-not-be-touched\n")

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr

    assert "mercury" in result.stdout
    assert "hermes -p mercury gateway stop" in result.stdout
    assert "hermes profile delete mercury" in result.stdout

    # Never touched.
    assert (stale_profile_dir / ".env").read_text() == "TELEGRAM_BOT_TOKEN=stale-should-not-be-touched\n"


def test_no_stale_profiles_prints_no_retirement_section(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    assert "retire" not in result.stdout.lower()


def test_commented_template_line_is_not_treated_as_set(sandbox):
    """A field left as a commented-out example (# HERMES_MODEL_PROVIDER=...,
    straight from .env.example) must still fail the required-value check --
    it must not be read as an empty-but-present value that slips past
    validation."""
    client_env = _write_client_env(sandbox["fieldkit_root"], "acme")
    lines = client_env.read_text().splitlines()
    commented = [
        f"# {line}" if line.startswith("HERMES_MODEL_PROVIDER=") else line
        for line in lines
    ]
    client_env.write_text("\n".join(commented) + "\n")

    result = _run("acme", sandbox)
    assert result.returncode != 0
    assert "HERMES_MODEL_PROVIDER" in result.stderr


def test_missing_client_directory_gives_clear_error(sandbox):
    result = _run("never-scaffolded-client", sandbox)
    assert result.returncode != 0
    assert "no such client" in result.stderr.lower()


def test_help_flag_exits_zero_without_a_client_name(sandbox):
    result = _run("--help", sandbox)
    assert result.returncode == 0
    assert "Usage: install_client.sh" in result.stdout


# ---------------------------------------------------------------------------
# THE DIRECT #59 CLOSURE PROOF: install_client.sh's root .env write is what
# process_photos.py's real CLIENT_NAME resolution actually reads, with NO
# CLIENT_NAME environment override present -- exactly matching a live Hermes
# skill dispatch (SKILL.md never sets CLIENT_NAME; see
# test_skill_dispatch_client_name.py's contract-lock test in the pre-#61
# history of this repo). Under the old concurrent-per-client-profile
# architecture, this exact invocation shape (no override, ambient env only)
# is what silently resolved to the wrong client -- issue #59's actual bug.
# Proving process_photos.py's REAL module-level resolution -- not a
# reimplementation, not a mock -- reads back exactly what install_client.sh
# just wrote is the strongest evidence the single-install model closes that
# gap by construction, not by a runtime guard that could itself be wrong.
# ---------------------------------------------------------------------------

def _resolve_via_real_process_photos(fieldkit_root: Path) -> subprocess.CompletedProcess:
    """Import the real scripts.process_photos module (unmodified production
    code) against the given FIELDKIT_ROOT, with NO CLIENT_NAME override in
    the environment -- the exact shape a live Hermes-dispatched subprocess
    would see."""
    snippet = (
        "import sys, os; sys.path.insert(0, '.'); "
        "from scripts import process_photos as m; "
        "print('RESOLVED_CLIENT=' + m._CLIENT)"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "FIELDKIT_ROOT": str(fieldkit_root),
        # Deliberately no CLIENT_NAME here -- that's the entire point.
    }
    return subprocess.run(
        [sys.executable, "-c", snippet],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_PLATFORM_PHOTO_AGENT),
    )


def test_installed_client_resolves_via_real_process_photos_with_no_env_override(sandbox):
    """After install_client.sh installs 'acme', process_photos.py's real,
    unmodified module-level CLIENT_NAME resolution -- invoked exactly as a
    live Hermes skill dispatch would (no CLIENT_NAME env var at all) --
    must resolve to 'acme'. This is the single-canonical-config-source
    proof issue #61 asked for: there is nothing else CLIENT_NAME could come
    from under this architecture except the file install_client.sh just
    wrote."""
    _write_client_env(sandbox["fieldkit_root"], "acme")
    install_result = _run("acme", sandbox, "--no-restart")
    assert install_result.returncode == 0, install_result.stderr

    resolve_result = _resolve_via_real_process_photos(sandbox["fieldkit_root"])
    assert resolve_result.returncode == 0, resolve_result.stderr
    assert "RESOLVED_CLIENT=acme" in resolve_result.stdout


def test_switching_installed_client_changes_what_process_photos_resolves(sandbox):
    """The direct #59 regression guard, end to end: install 'clienta',
    confirm process_photos.py resolves it with no override; install
    'clientb' next; confirm the SAME no-override invocation now resolves
    'clientb', not a stale mix of both. Two clients' config can never be
    simultaneously live under this architecture, so there is nothing left
    for a live skill dispatch to resolve incorrectly."""
    _write_client_env(sandbox["fieldkit_root"], "clienta")
    assert _run("clienta", sandbox, "--no-restart").returncode == 0

    first = _resolve_via_real_process_photos(sandbox["fieldkit_root"])
    assert "RESOLVED_CLIENT=clienta" in first.stdout, first.stderr

    _write_client_env(sandbox["fieldkit_root"], "clientb")
    assert _run("clientb", sandbox, "--no-restart").returncode == 0

    second = _resolve_via_real_process_photos(sandbox["fieldkit_root"])
    assert "RESOLVED_CLIENT=clientb" in second.stdout, second.stderr
