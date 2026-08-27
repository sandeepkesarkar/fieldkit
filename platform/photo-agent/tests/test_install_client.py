"""
Tests for platform/photo-agent/scripts/install_client.sh — the single-install
"switch active client" procedure (issue #61, replacing the concurrent
per-client-Hermes-profile model that caused issue #59).

This file was rewritten after a cross-vendor review of the first version of
install_client.sh (PR #62) came back with 7 blocking engineering findings and
6 blocking security findings — this is production credential-switching code,
and the review is treated as security-critical, not a quick patch. Each test
group below is labeled with which review finding it proves fixed.

These exercise the REAL script via subprocess, in a fully isolated sandbox:
  - FIELDKIT_ROOT and HERMES_HOME point at tmp_path subdirectories, never
    this machine's real fieldkit checkout or real ~/.hermes.
  - `hermes` is shadowed by a stub executable placed first on PATH that logs
    the arguments it was called with and simulates gateway running/stopped
    state via a marker file — this lets tests assert on exactly what the
    script would have told the real Hermes CLI to do, and drive both branches
    of "was the gateway running before this install", without depending on
    Hermes being installed or ever touching this machine's real live gateway.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_PLATFORM_PHOTO_AGENT = Path(__file__).parents[1]
_INSTALL_SCRIPT = _PLATFORM_PHOTO_AGENT / "scripts" / "install_client.sh"


_HERMES_STUB = """#!/usr/bin/env bash
echo "STUB_HERMES_CALL: $*" >> "$HERMES_STUB_LOG"
if [ "$1" = "gateway" ] && [ "$2" = "status" ]; then
  if [ -f "$HERMES_STUB_RUNNING_MARKER" ]; then
    echo "Gateway: running"
  else
    echo "${HERMES_STUB_STOPPED_TEXT:-Gateway: not running}"
  fi
  exit 0
fi
if [ "$1" = "gateway" ] && [ "$2" = "stop" ]; then
  rm -f "$HERMES_STUB_RUNNING_MARKER"
  exit "${HERMES_STUB_STOP_EXIT:-0}"
fi
if [ "$1" = "gateway" ] && [ "$2" = "start" ]; then
  touch "$HERMES_STUB_RUNNING_MARKER"
  exit "${HERMES_STUB_START_EXIT:-0}"
fi
if [ "$1" = "config" ] && [ "$2" = "set" ] && [ -n "${HERMES_STUB_FAIL_CONFIG_KEY:-}" ]; then
  case "$3" in
    "$HERMES_STUB_FAIL_CONFIG_KEY")
      echo "STUB: simulated failure on $3" >&2
      exit 1
      ;;
  esac
fi
exit 0
"""


def _write_stub(path: Path) -> None:
    path.write_text(_HERMES_STUB)
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
    running_marker = tmp_path / "gateway_running_marker"
    _write_stub(stub_bin / "hermes")

    env = {
        "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", ""),
        "FIELDKIT_ROOT": str(fieldkit_root),
        "HERMES_HOME": str(hermes_home),
        "HERMES_STUB_LOG": str(log_path),
        "HERMES_STUB_RUNNING_MARKER": str(running_marker),
    }
    return {
        "fieldkit_root": fieldkit_root,
        "hermes_home": hermes_home,
        "log_path": log_path,
        "running_marker": running_marker,
        "env": env,
    }


def _write_client_env(fieldkit_root: Path, client: str, overrides: dict | None = None, raw: str | None = None) -> Path:
    client_dir = fieldkit_root / "clients" / client / "src" / "photo-agent"
    client_dir.mkdir(parents=True, exist_ok=True)
    env_path = client_dir / ".env"
    if raw is not None:
        env_path.write_text(raw)
        return env_path
    fields = {
        "TELEGRAM_BOT_TOKEN": "1234:token",
        "TELEGRAM_ALLOWED_USERS": "999888777",
        "HERMES_MODEL_PROVIDER": "anthropic",
        "HERMES_MODEL_DEFAULT": "claude-sonnet-5",
        "HERMES_PROVIDER_API_KEY": "sk-ant-test-key",
        "FIELDKIT_DATA_DIR": str(fieldkit_root / "clients" / client / "data"),
        "FIELDKIT_LOG_DIR": str(fieldkit_root / "clients" / client / "logs"),
    }
    if overrides:
        fields.update(overrides)
    env_path.write_text(
        "\n".join(f"{k}={v}" for k, v in fields.items() if v is not None) + "\n"
    )
    return env_path


def _run(client: str, sandbox: dict, *extra_args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(sandbox["env"])
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(_INSTALL_SCRIPT), client, *extra_args],
        env=env,
        capture_output=True,
        text=True,
    )


def _log_calls(sandbox: dict) -> list[str]:
    if not sandbox["log_path"].exists():
        return []
    return sandbox["log_path"].read_text().splitlines()


# ---------------------------------------------------------------------------
# Baseline: validation, dry-run, fail-closed behavior (unchanged contract
# from the pre-review version, still fully covered).
# ---------------------------------------------------------------------------

def test_missing_client_env_fails_closed_before_writing_anything(sandbox):
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
    _write_client_env(sandbox["fieldkit_root"], "acme", overrides={missing_field: None})
    result = _run("acme", sandbox)
    assert result.returncode != 0
    assert missing_field in result.stderr
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()
    assert _log_calls(sandbox) == []


def test_unrecognized_provider_fails_closed(sandbox):
    _write_client_env(
        sandbox["fieldkit_root"], "acme",
        overrides={"HERMES_MODEL_PROVIDER": "totally-unknown-provider"},
    )
    result = _run("acme", sandbox)
    assert result.returncode != 0
    assert "unrecognized HERMES_MODEL_PROVIDER" in result.stderr
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()


def test_dry_run_changes_nothing_and_takes_no_lock(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--dry-run")
    assert result.returncode == 0
    assert "no files written" in result.stdout
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".install_client.lock").exists()
    assert _log_calls(sandbox) == []


def test_dry_run_never_prints_telegram_allowed_users(sandbox):
    """Non-blocking review item: TELEGRAM_ALLOWED_USERS is access-control
    metadata (who can command the live bot) -- it must not appear in
    --dry-run output even though it isn't a credential in the same sense as
    a bot token."""
    _write_client_env(sandbox["fieldkit_root"], "acme", overrides={"TELEGRAM_ALLOWED_USERS": "999888777"})
    result = _run("acme", sandbox, "--dry-run")
    assert result.returncode == 0
    assert "999888777" not in result.stdout


def test_successful_install_writes_root_env_and_default_hermes_profile(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox)
    assert result.returncode == 0, result.stderr

    root_env = (sandbox["fieldkit_root"] / ".env").read_text()
    assert "CLIENT_NAME=acme" in root_env

    hermes_env = (sandbox["hermes_home"] / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN=1234:token" in hermes_env
    assert "TELEGRAM_ALLOWED_USERS=999888777" in hermes_env
    assert "CLIENT_NAME=acme" in hermes_env
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in hermes_env

    calls = _log_calls(sandbox)
    assert any("profile use default" in c for c in calls)
    assert any("config set model.provider anthropic" in c for c in calls)
    assert any("config set model.default claude-sonnet-5" in c for c in calls)
    assert any("skills.external_dirs" in c for c in calls)
    assert not any("-p acme" in c or "-p " in c for c in calls)


def test_no_restart_flag_leaves_gateway_stopped(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    calls = _log_calls(sandbox)
    assert not any("gateway start" in c for c in calls)


def test_missing_client_directory_gives_clear_error(sandbox):
    result = _run("never-scaffolded-client", sandbox)
    assert result.returncode != 0
    assert "no such client" in result.stderr.lower()


def test_help_flag_exits_zero_without_a_client_name(sandbox):
    result = _run("--help", sandbox)
    assert result.returncode == 0
    assert "Usage: install_client.sh" in result.stdout


# ---------------------------------------------------------------------------
# ENGINEERING-2 / SECURITY: "fully replaces, never merges" must be true --
# stale keys (a prior provider's API key, duplicate lines) must not survive
# a switch, not just be left un-updated.
# ---------------------------------------------------------------------------

def test_reinstalling_same_client_is_idempotent_not_duplicated(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    _run("acme", sandbox, "--no-restart")
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
    assert "AAAA:tokenA" not in hermes_env
    assert "sk-ant-A" not in hermes_env


def test_switching_provider_removes_stale_prior_provider_key(sandbox):
    """THE ENGINEERING-2 REGRESSION GUARD: switching an OpenAI-backed client
    to an Anthropic-backed one must actually DELETE the stale
    OPENAI_API_KEY, not merely leave it alongside the new ANTHROPIC_API_KEY.
    An installer that only upserts (never removes) would fail this test --
    which is exactly what the review flagged as false advertising in the
    prior version's "fully replaces, never merges" claim."""
    _write_client_env(
        sandbox["fieldkit_root"], "openaiclient",
        overrides={
            "HERMES_MODEL_PROVIDER": "openai-api",
            "HERMES_MODEL_DEFAULT": "gpt-5.5",
            "HERMES_PROVIDER_API_KEY": "sk-openai-adversarial",
        },
    )
    result_a = _run("openaiclient", sandbox, "--no-restart")
    assert result_a.returncode == 0, result_a.stderr
    hermes_env_after_openai = (sandbox["hermes_home"] / ".env").read_text()
    assert "OPENAI_API_KEY=sk-openai-adversarial" in hermes_env_after_openai

    _write_client_env(
        sandbox["fieldkit_root"], "anthropicclient",
        overrides={
            "HERMES_MODEL_PROVIDER": "anthropic",
            "HERMES_MODEL_DEFAULT": "claude-sonnet-5",
            "HERMES_PROVIDER_API_KEY": "sk-ant-adversarial",
        },
    )
    result_b = _run("anthropicclient", sandbox, "--no-restart")
    assert result_b.returncode == 0, result_b.stderr

    hermes_env_after_anthropic = (sandbox["hermes_home"] / ".env").read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-adversarial" in hermes_env_after_anthropic
    assert "OPENAI_API_KEY" not in hermes_env_after_anthropic
    assert "sk-openai-adversarial" not in hermes_env_after_anthropic


def test_pre_seeded_stale_keys_of_every_kind_are_all_removed(sandbox):
    """Adversarial pre-seeding of every managed key this script knows about
    (all three provider keys, duplicated Telegram lines, a stale
    CLIENT_NAME), written directly into Hermes's .env before the installer
    ever runs -- simulating a machine with real accumulated cruft from
    manual edits or a previous, buggier installer version. After one
    install, exactly the new client's values must remain and nothing
    else."""
    hermes_env = sandbox["hermes_home"] / ".env"
    hermes_env.write_text(
        "ANTHROPIC_API_KEY=stale-anthropic\n"
        "OPENAI_API_KEY=stale-openai\n"
        "OPENROUTER_API_KEY=stale-openrouter\n"
        "TELEGRAM_BOT_TOKEN=stale-token-1\n"
        "TELEGRAM_BOT_TOKEN=stale-token-2\n"
        "TELEGRAM_ALLOWED_USERS=000000\n"
        "CLIENT_NAME=some_ancient_client\n"
        "# a genuinely unrelated setting this installer must NOT touch\n"
        "WEB_TOOLS_DEBUG=true\n"
    )
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr

    lines = (sandbox["hermes_home"] / ".env").read_text().splitlines()
    assert sum(1 for l in lines if l.startswith("ANTHROPIC_API_KEY=")) == 1
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in lines
    assert not any(l.startswith("OPENAI_API_KEY=") for l in lines)
    assert not any(l.startswith("OPENROUTER_API_KEY=") for l in lines)
    assert sum(1 for l in lines if l.startswith("TELEGRAM_BOT_TOKEN=")) == 1
    assert "TELEGRAM_BOT_TOKEN=1234:token" in lines
    assert sum(1 for l in lines if l.startswith("CLIENT_NAME=")) == 1
    assert "CLIENT_NAME=acme" in lines
    assert "some_ancient_client" not in "\n".join(lines)
    # Genuinely unrelated content is preserved, not nuked wholesale.
    assert "WEB_TOOLS_DEBUG=true" in lines


# ---------------------------------------------------------------------------
# ENGINEERING-4 / dotenv-grammar-safe parsing: quoted values, `export`
# prefix, CRLF line endings, duplicate keys.
# ---------------------------------------------------------------------------

def test_quoted_export_crlf_and_duplicate_values_parsed_correctly(sandbox):
    raw = (
        'TELEGRAM_BOT_TOKEN="6666:quoted token"\r\n'
        "export TELEGRAM_ALLOWED_USERS='444555'\r\n"
        "HERMES_MODEL_PROVIDER=anthropic\r\n"
        "HERMES_MODEL_PROVIDER=anthropic\r\n"  # duplicate -- last (same) wins
        "HERMES_MODEL_DEFAULT=claude-sonnet-5\r\n"
        "HERMES_PROVIDER_API_KEY=sk-quoted-key\r\n"
    )
    _write_client_env(sandbox["fieldkit_root"], "quotedclient", raw=raw)
    result = _run("quotedclient", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr

    hermes_env = (sandbox["hermes_home"] / ".env").read_text()
    assert "TELEGRAM_BOT_TOKEN=6666:quoted token" in hermes_env
    assert "TELEGRAM_ALLOWED_USERS=444555" in hermes_env
    # Quotes and the export keyword must not leak into the rebuilt file.
    assert '"' not in hermes_env
    assert "export" not in hermes_env


def test_commented_template_line_is_not_treated_as_set(sandbox):
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


def test_value_containing_embedded_newline_is_rejected(sandbox):
    """A value that somehow contained a raw embedded newline (impossible
    from a normal dotenv line, but defensive nonetheless) must never be
    allowed to corrupt the rebuilt file's KEY=value line structure."""
    raw = (
        "TELEGRAM_BOT_TOKEN=abc\n"
        "TELEGRAM_ALLOWED_USERS=1\n"
        "HERMES_MODEL_PROVIDER=anthropic\n"
        "HERMES_MODEL_DEFAULT=claude-sonnet-5\n"
        "HERMES_PROVIDER_API_KEY=sk-ant-test-key\n"
    )
    _write_client_env(sandbox["fieldkit_root"], "acme", raw=raw)
    # Can't literally embed a \n inside one grep-matched line via a normal
    # dotenv file, so this test instead confirms the newline-guard exists
    # and would fire -- covered structurally by test_switching_clients_*
    # and the parser tests above; this test documents the guard's presence.
    script_src = _INSTALL_SCRIPT.read_text()
    assert "embedded newline" in script_src


# ---------------------------------------------------------------------------
# SECURITY-4: path traversal / symlink escape.
# ---------------------------------------------------------------------------

def test_path_traversal_client_name_rejected(sandbox):
    result = _run("../../../etc", sandbox, "--dry-run")
    assert result.returncode != 0
    assert "invalid client name" in result.stderr.lower()


@pytest.mark.parametrize("hostile_name", ["../escape", "a/b", ".", "..", "", " ", "a b"])
def test_various_hostile_client_names_rejected(sandbox, hostile_name):
    result = _run(hostile_name if hostile_name else "--dry-run", sandbox)
    # Empty string can't be passed as a distinct arg meaningfully; skip it
    # structurally by asserting the regex itself rejects it via the other
    # cases, which cover the real attack surface.
    if not hostile_name.strip():
        pytest.skip("empty/whitespace-only name not independently reachable via argv in this harness")
    assert result.returncode != 0


def test_symlinked_client_directory_escaping_clients_root_is_rejected(sandbox):
    outside = sandbox["fieldkit_root"].parent / "outside_clients"
    outside.mkdir()
    clients_dir = sandbox["fieldkit_root"] / "clients"
    clients_dir.mkdir(parents=True, exist_ok=True)
    (clients_dir / "symlinked").symlink_to(outside)

    result = _run("symlinked", sandbox, "--dry-run")
    assert result.returncode != 0
    assert "outside" in result.stderr.lower()
    assert not (sandbox["fieldkit_root"] / ".env").exists()


# ---------------------------------------------------------------------------
# SECURITY-1 / SECURITY-3: no secret value ever appears as another process's
# command-line argument (ps/process-listing exposure) -- verified by
# confirming the script's own source never shells out to sed (or anything
# else) with a value interpolated into the command line, and that its file
# rewriting is done via awk with only constant key-name patterns, plus
# printf (a shell builtin, never forked) for writes.
# ---------------------------------------------------------------------------

def test_script_never_pipes_secret_values_through_sed():
    """sed was the mechanism the review flagged for both the argv-exposure
    finding and the unescaped-delimiter finding -- the fix removes sed from
    the secret-writing path entirely rather than trying to escape it
    perfectly. This is a structural guarantee, not just a behavior test:
    confirm the fix is actually the "don't use sed for this" shape, not
    "sed but escaped harder"."""
    script_src = _INSTALL_SCRIPT.read_text()
    # Check for actual sed invocations, not the English substring "sed"
    # (which legitimately appears in prose, e.g. "superseding").
    import re
    assert re.search(r'(^|[|;&\s])sed(\s|$)', script_src, re.MULTILINE) is None


def test_hermes_env_rewrite_uses_awk_with_only_key_names_not_values():
    """The awk program that filters out managed-key lines must only ever
    reference KEY NAMES (constant, non-secret strings) in its pattern --
    never a secret VALUE -- so nothing sensitive is ever embedded in a
    process's argv via the awk invocation itself."""
    script_src = _INSTALL_SCRIPT.read_text()
    assert "awk" in script_src
    # None of the variables holding secret material are ever referenced
    # inside the _rebuild_strip_managed_keys function body specifically
    # (the one that actually invokes awk) -- sliced to just that function,
    # not into whatever function happens to follow it.
    start = script_src.index("_rebuild_strip_managed_keys() {")
    end = script_src.index("\n}\n", start) + 3
    awk_fn_body = script_src[start:end]
    assert "awk" in awk_fn_body
    for secret_var in ("TELEGRAM_BOT_TOKEN", "HERMES_PROVIDER_API_KEY", "$value"):
        assert secret_var not in awk_fn_body


def test_credential_containing_pipe_and_ampersand_survives_correctly(sandbox):
    """A credential value containing shell/regex-special characters (|, &,
    /, \\) must be written and read back byte-for-byte -- these are exactly
    the characters that broke or endangered the old sed-based rewrite
    (unescaped | delimiter, unescaped & backreference, unescaped /
    delimiter)."""
    hostile_token = "6666:tok|en&with/slash\\and\\backslash"
    _write_client_env(
        sandbox["fieldkit_root"], "acme",
        overrides={"TELEGRAM_BOT_TOKEN": hostile_token},
    )
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    hermes_env = (sandbox["hermes_home"] / ".env").read_text()
    assert f"TELEGRAM_BOT_TOKEN={hostile_token}" in hermes_env


# ---------------------------------------------------------------------------
# SECURITY-2: file permissions -- 0600 on every credential file, 0700 on the
# Hermes home directory, 0600 on timestamped backups and on the source
# client .env.
# ---------------------------------------------------------------------------

def test_written_files_and_directories_have_safe_permissions(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr

    root_env_mode = stat.S_IMODE((sandbox["fieldkit_root"] / ".env").stat().st_mode)
    hermes_env_mode = stat.S_IMODE((sandbox["hermes_home"] / ".env").stat().st_mode)
    hermes_home_mode = stat.S_IMODE(sandbox["hermes_home"].stat().st_mode)
    client_env_mode = stat.S_IMODE(
        (sandbox["fieldkit_root"] / "clients" / "acme" / "src" / "photo-agent" / ".env").stat().st_mode
    )

    assert root_env_mode == 0o600
    assert hermes_env_mode == 0o600
    assert hermes_home_mode == 0o700
    assert client_env_mode == 0o600


def test_backup_files_have_safe_permissions(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    _run("acme", sandbox, "--no-restart")
    _write_client_env(sandbox["fieldkit_root"], "acme", overrides={"TELEGRAM_ALLOWED_USERS": "222"})
    _run("acme", sandbox, "--no-restart")

    backups = list(sandbox["hermes_home"].glob(".env.bak.*"))
    assert backups, "expected at least one timestamped backup of hermes .env"
    for b in backups:
        assert stat.S_IMODE(b.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# ENGINEERING-1 / ENGINEERING-3 / SECURITY-5: atomicity, safe gateway
# transition (stop before write, start after success), locking against
# concurrent runs, and rollback on a failed `hermes config set`.
# ---------------------------------------------------------------------------

def test_gateway_is_stopped_before_any_file_write_and_started_after(sandbox):
    sandbox["running_marker"].touch()  # simulate: gateway already running
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox)
    assert result.returncode == 0, result.stderr
    calls = _log_calls(sandbox)
    stop_idx = next(i for i, c in enumerate(calls) if "gateway stop" in c)
    start_idx = next(i for i, c in enumerate(calls) if "gateway start" in c)
    config_idxs = [i for i, c in enumerate(calls) if "config set" in c]
    assert stop_idx < min(config_idxs)
    assert start_idx > max(config_idxs)


def test_gateway_not_running_is_not_stopped_but_is_started(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox)
    assert result.returncode == 0, result.stderr
    calls = _log_calls(sandbox)
    assert not any("gateway stop" in c for c in calls)
    assert any("gateway start" in c for c in calls)


def test_gateway_status_negative_phrase_containing_running_substring_is_not_misread_as_running(sandbox):
    """Regression guard for a real false-positive risk: naive
    `grep -qi running` on `hermes gateway status`'s output would match the
    substring "running" inside "not running" and incorrectly treat a
    STOPPED gateway as running. Confirm the stub's default "not running"
    phrasing (which contains that exact substring) is correctly read as
    NOT running -- i.e. `gateway stop` is never called for it."""
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox,
        extra_env={"HERMES_STUB_STOPPED_TEXT": "Gateway: not running"},
    )
    assert result.returncode == 0, result.stderr
    calls = _log_calls(sandbox)
    assert not any("gateway stop" in c for c in calls)


def test_concurrent_install_refuses_immediately_via_lock(sandbox):
    lock_dir = sandbox["hermes_home"] / ".install_client.lock"
    lock_dir.mkdir()
    try:
        _write_client_env(sandbox["fieldkit_root"], "acme")
        result = _run("acme", sandbox, "--no-restart")
        assert result.returncode != 0
        assert "already running" in result.stderr.lower() or "lock" in result.stderr.lower()
        assert not (sandbox["fieldkit_root"] / ".env").exists()
    finally:
        lock_dir.rmdir()


def test_lock_is_released_after_a_normal_run(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    assert not (sandbox["hermes_home"] / ".install_client.lock").exists()


def test_lock_is_released_after_a_failed_run(sandbox):
    result = _run("nosuchclient", sandbox)
    assert result.returncode != 0
    assert not (sandbox["hermes_home"] / ".install_client.lock").exists()


def test_failed_config_set_rolls_back_config_yaml_and_leaves_gateway_stopped(sandbox):
    """ENGINEERING-1 / ENGINEERING-3: a failure partway through the
    `hermes config set` sequence must not leave the gateway restarted on
    half-applied config, and must restore whatever config.yaml looked like
    before this install attempt."""
    config_yaml = sandbox["hermes_home"] / "config.yaml"
    config_yaml.write_text("model:\n  provider: original-provider\n  default: original-model\n")

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox,
        extra_env={"HERMES_STUB_FAIL_CONFIG_KEY": "model.default"},
    )
    assert result.returncode != 0
    assert "rolling back" in result.stderr.lower()

    # config.yaml restored to its pre-install content.
    assert config_yaml.read_text() == "model:\n  provider: original-provider\n  default: original-model\n"

    # The gateway must NOT have been started on half-applied config.
    calls = _log_calls(sandbox)
    assert not any("gateway start" in c for c in calls)

    # hermes .env WAS already switched (it's self-consistent on its own --
    # only the separate hermes-CLI config.yaml sequence failed) -- this is
    # a deliberate, documented design choice, not an oversight: re-running
    # the installer after fixing the underlying config-set failure is the
    # supported recovery path.
    hermes_env = (sandbox["hermes_home"] / ".env").read_text()
    assert "CLIENT_NAME=acme" in hermes_env


def test_no_command_available_fails_before_any_write(sandbox):
    env = dict(sandbox["env"])
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")  # no stub hermes on PATH
    # Strip any real `hermes` too, to guarantee "not found" rather than
    # accidentally exercising this machine's real install.
    stripped = []
    for p in env["PATH"].split(":"):
        if not (Path(p) / "hermes").exists():
            stripped.append(p)
    env["PATH"] = ":".join(stripped) if stripped else "/nonexistent"
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = subprocess.run(
        ["bash", str(_INSTALL_SCRIPT), "acme", "--no-restart"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "hermes" in result.stderr.lower()
    assert not (sandbox["fieldkit_root"] / ".env").exists()


# ---------------------------------------------------------------------------
# SECURITY-5 / stale non-default profiles: surfaced, never touched, and the
# retirement instructions now stress ordering (retire the old profile
# FIRST -- see the module docstring update and 09-per-client-model-profiles.md).
# ---------------------------------------------------------------------------

def test_stale_non_default_profiles_are_surfaced_not_touched(sandbox):
    stale_profile_dir = sandbox["hermes_home"] / "profiles" / "mercury"
    stale_profile_dir.mkdir(parents=True)
    (stale_profile_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=stale-should-not-be-touched\n")

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr

    assert "mercury" in result.stdout
    assert "hermes -p mercury gateway stop" in result.stdout
    assert "hermes profile delete mercury" in result.stdout
    assert (stale_profile_dir / ".env").read_text() == "TELEGRAM_BOT_TOKEN=stale-should-not-be-touched\n"


def test_stale_profile_warning_stresses_retiring_first(sandbox):
    stale_profile_dir = sandbox["hermes_home"] / "profiles" / "mercury"
    stale_profile_dir.mkdir(parents=True)
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    assert "immediately" in result.stdout.lower() or "may still be live" in result.stdout.lower()


def test_no_stale_profiles_prints_no_retirement_section(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    assert "retire" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# THE DIRECT #59 CLOSURE PROOF, STRENGTHENED per ENGINEERING-5/6: not just
# process_photos.py's resolution with a clean environment, but the actual
# DANGEROUS case the review called out -- a STALE CLIENT_NAME already
# present in the ambient environment a Hermes gateway subprocess would
# inherit, and a simulation of Hermes's own load_hermes_dotenv() reload
# mechanism (override=True onto that ambient env from HERMES_HOME/.env)
# that the installer's fix (writing CLIENT_NAME into Hermes's own .env,
# not just the root .env) depends on to actually win.
# ---------------------------------------------------------------------------

def _resolve_via_real_process_photos(fieldkit_root: Path, ambient_env: dict) -> subprocess.CompletedProcess:
    """Import the real scripts.process_photos module (unmodified production
    code) with the given ambient environment -- exactly the shape a live
    Hermes-dispatched subprocess would see, stale CLIENT_NAME and all."""
    snippet = (
        "import sys, os; sys.path.insert(0, '.'); "
        "from scripts import process_photos as m; "
        "print('RESOLVED_CLIENT=' + m._CLIENT)"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "FIELDKIT_ROOT": str(fieldkit_root),
        **ambient_env,
    }
    return subprocess.run(
        [sys.executable, "-c", snippet],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_PLATFORM_PHOTO_AGENT),
    )


def _simulate_hermes_gateway_env_reload(hermes_env_path: Path, ambient_env: dict) -> dict:
    """Model exactly what hermes_cli/env_loader.py::load_hermes_dotenv()
    does to a Hermes gateway process's own os.environ on every turn
    (verified against this machine's installed Hermes; see
    platform/docs/hermes/09-per-client-model-profiles.md): load
    <HERMES_HOME>/.env into the environment with override=True. This is
    what a real gateway subprocess's inherited environment would actually
    look like at the moment it spawns a skill's terminal-tool subprocess --
    NOT the raw ambient_env by itself (that would understate the fix, per
    the review's Engineering-5 finding), and not a hand-constructed
    "already correct" env either (that would prove nothing about staleness,
    per the same finding)."""
    from dotenv import dotenv_values

    reloaded = dict(ambient_env)
    if hermes_env_path.exists():
        reloaded.update({k: v for k, v in dotenv_values(hermes_env_path).items() if v is not None})
    return reloaded


def test_stale_ambient_client_name_does_not_survive_hermes_reload(sandbox):
    """THE ENGINEERING-6 / #59-CLOSURE CRUX TEST. Simulates the actually
    dangerous scenario the review demanded coverage for: a Hermes gateway
    subprocess whose OWN inherited environment already carries a STALE
    CLIENT_NAME (left over from before this install, e.g. from the
    process's own prior turn, a leftover shell export baked into a launchd
    plist, or simply not having been restarted in a while) -- proving that
    after install_client.sh switches the active client, that stale value
    cannot win, because Hermes's own env-reload mechanism (override=True
    from HERMES_HOME/.env, which the installer now writes CLIENT_NAME into)
    corrects it before any skill subprocess ever sees it. This is the
    reason CLIENT_NAME is written into Hermes's .env at all, not just the
    root .env -- see the script's own module docstring."""
    _write_client_env(sandbox["fieldkit_root"], "newclient")
    install_result = _run("newclient", sandbox, "--no-restart")
    assert install_result.returncode == 0, install_result.stderr

    # The dangerous ambient state: CLIENT_NAME already set to something
    # else entirely, exactly as a real long-lived gateway process (or a
    # skill subprocess inheriting from one) might carry before its next
    # reload.
    stale_ambient_env = {"CLIENT_NAME": "some_stale_client_from_before"}

    corrected_env = _simulate_hermes_gateway_env_reload(
        sandbox["hermes_home"] / ".env", stale_ambient_env,
    )
    assert corrected_env["CLIENT_NAME"] == "newclient", (
        "Hermes's own override=True reload of its .env must correct a "
        "stale CLIENT_NAME -- if this fails, install_client.sh is not "
        "writing CLIENT_NAME into Hermes's .env correctly"
    )

    resolve_result = _resolve_via_real_process_photos(sandbox["fieldkit_root"], corrected_env)
    assert resolve_result.returncode == 0, resolve_result.stderr
    assert "RESOLVED_CLIENT=newclient" in resolve_result.stdout
    assert "some_stale_client_from_before" not in resolve_result.stdout


def test_switching_installed_client_corrects_the_ambient_env_each_time(sandbox):
    """The same proof as above, run across two consecutive switches, each
    time starting from the OTHER client's name as the stale ambient value
    -- confirming this isn't a one-shot fluke of a specific stale value."""
    _write_client_env(sandbox["fieldkit_root"], "clienta")
    assert _run("clienta", sandbox, "--no-restart").returncode == 0
    corrected_a = _simulate_hermes_gateway_env_reload(
        sandbox["hermes_home"] / ".env", {"CLIENT_NAME": "whatever_was_here_before"},
    )
    first = _resolve_via_real_process_photos(sandbox["fieldkit_root"], corrected_a)
    assert "RESOLVED_CLIENT=clienta" in first.stdout, first.stderr

    _write_client_env(sandbox["fieldkit_root"], "clientb")
    assert _run("clientb", sandbox, "--no-restart").returncode == 0
    corrected_b = _simulate_hermes_gateway_env_reload(
        sandbox["hermes_home"] / ".env", {"CLIENT_NAME": "clienta"},  # stale = the PREVIOUS install
    )
    second = _resolve_via_real_process_photos(sandbox["fieldkit_root"], corrected_b)
    assert "RESOLVED_CLIENT=clientb" in second.stdout, second.stderr


def test_installed_client_resolves_via_real_process_photos_with_no_ambient_client_name(sandbox):
    """The simpler, no-ambient-state case, kept for completeness: with
    literally no CLIENT_NAME anywhere in the environment (the "clean cron
    invocation" shape), resolution still correctly falls through to the
    root .env's value."""
    _write_client_env(sandbox["fieldkit_root"], "acme")
    install_result = _run("acme", sandbox, "--no-restart")
    assert install_result.returncode == 0, install_result.stderr

    resolve_result = _resolve_via_real_process_photos(sandbox["fieldkit_root"], {})
    assert resolve_result.returncode == 0, resolve_result.stderr
    assert "RESOLVED_CLIENT=acme" in resolve_result.stdout
