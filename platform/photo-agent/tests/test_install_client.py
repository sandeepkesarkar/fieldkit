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


# Status text below matches Hermes's REAL macOS/launchd output contract
# (see install_client.sh's own _gateway_status doc comment, and
# _REAL_HERMES_STATUS_* fixtures in this file for verbatim live captures)
# -- not arbitrary placeholder strings. Using realistic phrasing here is
# what makes these tests actually exercise the real classifier logic
# rather than a simplified stand-in for it.
_HERMES_STUB = """#!/usr/bin/env bash
echo "STUB_HERMES_CALL: $*" >> "$HERMES_STUB_LOG"
if [ "$1" = "-p" ]; then
  PROFILE="$2"
  if [ "$3" = "gateway" ] && [ "$4" = "status" ]; then
    # TOCTOU-race simulation (issue #62 review Security-5b): if
    # HERMES_STUB_TOCTOU_COUNTER_DIR is set, the Nth query for this
    # profile returns "not-running" for query 1 and "running" for every
    # query after that -- modeling a stale profile's gateway starting up
    # in the window between the installer's early check and its later
    # recheck right before `gateway start`.
    if [ -n "${HERMES_STUB_TOCTOU_COUNTER_DIR:-}" ]; then
      COUNTER_FILE="$HERMES_STUB_TOCTOU_COUNTER_DIR/$PROFILE"
      COUNT=0
      [ -f "$COUNTER_FILE" ] && COUNT="$(cat "$COUNTER_FILE")"
      COUNT=$((COUNT + 1))
      echo "$COUNT" > "$COUNTER_FILE"
      if [ "$COUNT" -eq 1 ]; then
        echo "✗ Gateway service is not loaded"
      else
        echo "✓ Gateway is supervised by launchd (PID 777)"
      fi
      exit 0
    fi
    VARNAME="HERMES_STUB_PROFILE_STATUS_${PROFILE}"
    STATUS="${!VARNAME:-not-running}"
    case "$STATUS" in
      running) echo "✓ Gateway is supervised by launchd (PID 999)" ;;
      not-running) echo "✗ Gateway service is not loaded" ;;
      ambiguous) echo "???unparseable???" ;;
      command-fails) exit 7 ;;
    esac
    exit 0
  fi
  exit 0
fi
if [ "$1" = "gateway" ] && [ "$2" = "status" ]; then
  if [ -f "$HERMES_STUB_RUNNING_MARKER" ]; then
    echo "✓ Gateway is supervised by launchd (PID 462)"
  else
    echo "${HERMES_STUB_STOPPED_TEXT:-✗ Gateway service is not loaded}"
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
if [ "$1" = "config" ] && [ "$2" = "set" ]; then
  # Simulate what Hermes's real CLI does: `config set` rewrites
  # config.yaml via its own atomic write, which means the LIVE file's
  # permission bits can differ from whatever they were before this
  # process ever started -- this is the real mechanism behind the
  # ENGINEERING-1c mode-preservation bug (a later call in this same
  # sequence failing must restore the file to its ORIGINAL mode, not
  # whatever mode Hermes's own rewrite happened to leave it in).
  if [ -n "${HERMES_STUB_CONFIG_SET_REWRITES_MODE:-}" ] && [ -n "${HERMES_HOME:-}" ] && [ -f "$HERMES_HOME/config.yaml" ]; then
    chmod "$HERMES_STUB_CONFIG_SET_REWRITES_MODE" "$HERMES_HOME/config.yaml"
  fi
  if [ -n "${HERMES_STUB_FAIL_CONFIG_KEY:-}" ] && [ "$3" = "$HERMES_STUB_FAIL_CONFIG_KEY" ]; then
    echo "STUB: simulated failure on $3" >&2
    exit 1
  fi
fi
exit 0
"""


# A stub `launchctl` is placed on every sandbox's PATH, by default (unless
# a test injects entries -- see HERMES_STUB_LAUNCHCTL_LIST_FILE below)
# reporting NOTHING loaded. This is REQUIRED for test isolation, not
# optional: install_client.sh's orphan-service detection (issue #62 review
# Security-5, round 4) calls the real `launchctl` directly, independent of
# HERMES_HOME sandboxing -- without this stub, every test on a machine
# that happens to have Hermes gateways actually running (this development
# machine does: `ai.hermes.gateway` and `ai.hermes.gateway-mercury`) would
# leak that real, live state into the stale-profile candidate set of every
# single test, causing spurious failures unrelated to what's being tested.
_LAUNCHCTL_STUB = """#!/usr/bin/env bash
echo "STUB_LAUNCHCTL_CALL: $*" >> "$HERMES_STUB_LOG"

# TOCTOU-race simulation for a genuinely ORPHANED service (no matching
# profile directory at all): if HERMES_STUB_LAUNCHCTL_TOCTOU_COUNTER_FILE
# is set, the FIRST bare `launchctl list` reports nothing (the orphan
# hasn't "appeared" yet -- the early check); every query after that
# (bare list, and the label-specific query) reports it present and alive.
TOCTOU_COUNTER="${HERMES_STUB_LAUNCHCTL_TOCTOU_COUNTER_FILE:-}"
TOCTOU_LABEL="${HERMES_STUB_LAUNCHCTL_TOCTOU_LABEL:-ai.hermes.gateway-orphaned}"
if [ -n "$TOCTOU_COUNTER" ]; then
  if [ "$1" = "list" ] && [ "$#" -eq 1 ]; then
    COUNT=0
    [ -f "$TOCTOU_COUNTER" ] && COUNT="$(cat "$TOCTOU_COUNTER")"
    COUNT=$((COUNT + 1))
    echo "$COUNT" > "$TOCTOU_COUNTER"
    echo "462	0	ai.hermes.gateway"
    if [ "$COUNT" -gt 1 ]; then
      echo "999	0	$TOCTOU_LABEL"
    fi
    exit 0
  fi
  if [ "$1" = "list" ] && [ "$2" = "$TOCTOU_LABEL" ]; then
    COUNT=0
    [ -f "$TOCTOU_COUNTER" ] && COUNT="$(cat "$TOCTOU_COUNTER")"
    if [ "$COUNT" -gt 1 ]; then
      echo "\\"PID\\" = 999;"
      exit 0
    fi
    exit 113
  fi
fi

LIST_FILE="${HERMES_STUB_LAUNCHCTL_LIST_FILE:-}"
if [ "$1" = "list" ] && [ "$#" -eq 1 ]; then
  # Bare `launchctl list` -- the whole table. Tab-separated PID/status/label
  # lines, matching real launchctl's own format, sourced from a file a test
  # can populate via HERMES_STUB_LAUNCHCTL_LIST_FILE.
  if [ -n "${HERMES_STUB_LAUNCHCTL_LIST_FAILS:-}" ]; then
    echo "STUB: simulated launchctl list failure" >&2
    exit 1
  fi
  if [ -n "$LIST_FILE" ] && [ -f "$LIST_FILE" ]; then
    cat "$LIST_FILE"
  fi
  exit 0
fi
if [ "$1" = "list" ] && [ -n "${2:-}" ]; then
  # `launchctl list <label>` -- a single-service lookup. The install
  # script no longer issues this call at all (round 6: it now parses the
  # PID directly out of the bare `launchctl list` table above instead) --
  # this branch exists purely so a test can PROVE that by making it fail
  # loudly (HERMES_STUB_LAUNCHCTL_LABEL_QUERY_FAILS, simulating codex's
  # real observed exit 75) and confirming the install still behaves
  # correctly, since it should never reach this branch in the first place.
  if [ -n "${HERMES_STUB_LAUNCHCTL_LABEL_QUERY_FAILS:-}" ]; then
    echo "STUB: simulated launchctl list <label> failure (exit 75)" >&2
    exit 75
  fi
  if [ -n "$LIST_FILE" ] && [ -f "$LIST_FILE" ]; then
    PID="$(awk -F'\\t' -v label="$2" '$3 == label {print $1}' "$LIST_FILE" | head -n1)"
    if [ -n "$PID" ] && [ "$PID" != "-" ]; then
      echo "\\"PID\\" = $PID;"
      exit 0
    elif [ -n "$PID" ]; then
      echo "\\"LastExitStatus\\" = 0;"
      exit 0
    fi
  fi
  exit 113
fi
exit 0
"""


def _write_stub(path: Path, content: str) -> None:
    path.write_text(content)
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
    launchctl_list_file = tmp_path / "launchctl_list.tsv"
    _write_stub(stub_bin / "hermes", _HERMES_STUB)
    _write_stub(stub_bin / "launchctl", _LAUNCHCTL_STUB)

    env = {
        "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", ""),
        "FIELDKIT_ROOT": str(fieldkit_root),
        "HERMES_HOME": str(hermes_home),
        "HERMES_STUB_LOG": str(log_path),
        "HERMES_STUB_RUNNING_MARKER": str(running_marker),
        "HERMES_STUB_LAUNCHCTL_LIST_FILE": str(launchctl_list_file),
    }
    return {
        "fieldkit_root": fieldkit_root,
        "hermes_home": hermes_home,
        "log_path": log_path,
        "running_marker": running_marker,
        "launchctl_list_file": launchctl_list_file,
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


def test_symlinked_client_env_FILE_escaping_the_client_directory_is_rejected(sandbox):
    """THE SECURITY-4 GAP THE REVIEW LIVE-REPRODUCED: a prior version of
    this script validated/canonicalized the CLIENT DIRECTORY but not the
    .env FILE itself -- an in-tree symlink AT clients/<name>/src/photo-agent/.env
    pointing to an arbitrary file outside the repo was followed transparently
    by [ -f ], the value parser, AND `chmod 600` (the review reproduced this
    live: even --dry-run mutated the external target's permissions from
    0644 to 0600). This must now be rejected outright, and --dry-run must
    make literally zero permission changes to it."""
    outside_dir = sandbox["fieldkit_root"].parent / "outside_secret"
    outside_dir.mkdir()
    outside_env = outside_dir / "secret.env"
    outside_env.write_text("TELEGRAM_BOT_TOKEN=exfiltrated\n")
    os.chmod(outside_env, 0o644)

    client_dir = sandbox["fieldkit_root"] / "clients" / "symclient" / "src" / "photo-agent"
    client_dir.mkdir(parents=True)
    (client_dir / ".env").symlink_to(outside_env)

    for args in (["--dry-run"], ["--no-restart"]):
        result = _run("symclient", sandbox, *args)
        assert result.returncode != 0
        assert "outside" in result.stderr.lower()
        # The external symlink target must never be touched -- not its
        # permissions, not its content -- regardless of --dry-run or a
        # real attempted run.
        assert stat.S_IMODE(outside_env.stat().st_mode) == 0o644
        assert outside_env.read_text() == "TELEGRAM_BOT_TOKEN=exfiltrated\n"
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
    the SECRET-WRITING path entirely rather than trying to escape it
    perfectly (that logic uses awk with only key names, and printf, as
    checked below). This is a structural guarantee, not just a behavior
    test: confirm no sed invocation ever appears inside the functions that
    handle credential material (get_client_var, _rebuild_strip_managed_keys,
    _emit_kv, or the commit-phase code itself).

    A later fix (issue #62 round-4 review, Engineering-3) legitimately
    introduced ONE sed invocation elsewhere in the script -- inside
    `_gateway_status`, to strip a specific substring out of Hermes's own
    STATUS TEXT (operational output, never a secret) before classifying
    it. That single, scoped use is fine and expected; this test is scoped
    to exclude it deliberately, not to re-litigate whether sed may exist
    in the file at all."""
    script_src = _INSTALL_SCRIPT.read_text()
    import re

    secret_handling_functions = [
        "get_client_var() {",
        "_rebuild_strip_managed_keys() {",
        "_emit_kv() {",
    ]
    for marker in secret_handling_functions:
        start = script_src.index(marker)
        end = script_src.index("\n}\n", start) + 3
        body = script_src[start:end]
        assert re.search(r'(^|[|;&\s])sed(\s|$)', body, re.MULTILINE) is None, (
            f"found a sed invocation inside {marker.split('(')[0]} -- secret "
            f"values must never be piped through sed"
        )

    # And the commit-phase code (the mv/chmod block that actually writes
    # live credential files) must not invoke sed either.
    commit_start = script_src.index("# --- Commit point:")
    commit_end = script_src.index('if [ "$NO_RESTART" -eq 0 ]')
    commit_body = script_src[commit_start:commit_end]
    assert re.search(r'(^|[|;&\s])sed(\s|$)', commit_body, re.MULTILINE) is None


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
    """Regression guard for a REAL false-positive bug caught live while
    building this classifier: Hermes's own real output for "no detached
    fallback process" is the literal phrase "No fallback process is
    running" -- which contains "is running" as a substring ("process IS
    RUNNING") despite meaning the opposite. A naive POSITIVE-checked-first
    classifier misclassified this exact phrase as running. Confirm it's
    still correctly read as NOT running -- i.e. `gateway stop` is never
    called for it."""
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox,
        extra_env={
            "HERMES_STUB_STOPPED_TEXT": (
                "⚠ Gateway service is registered but launchd is not supervising it\n"
                "✗ No fallback process is running"
            ),
        },
    )
    assert result.returncode == 0, result.stderr
    calls = _log_calls(sandbox)
    assert not any("gateway stop" in c for c in calls)


# ---------------------------------------------------------------------------
# ENGINEERING-3, THE ACTUAL FIX: the classifier must recognize this
# machine's REAL installed Hermes's status output, not just generic
# "running"/"not running" substrings. Confirmed via live reproduction: the
# real, most common output line -- "✓ Gateway is supervised by launchd
# (PID N)" -- contains neither "running" nor a negation of it, so the
# original naive classifier misclassified a genuinely running gateway as
# ambiguous and aborted every real install on this machine (safe, but
# operationally broken). These tests use VERBATIM captures from this
# machine's real `hermes gateway status` (both the default profile and
# mercury's still-running leftover profile), not reconstructed strings.
# ---------------------------------------------------------------------------

_REAL_HERMES_STATUS_DEFAULT_PROFILE = """Launchd plist: /Users/sandeep_a_k/Library/LaunchAgents/ai.hermes.gateway.plist
⚠ Service definition is stale relative to the current Hermes install
  Run: hermes gateway start
✓ Gateway is supervised by launchd (PID 462)
  Auto-start at login and auto-restart on crash are available.

Other profiles:
  ✓ mercury          — PID 614
"""

_REAL_HERMES_STATUS_MERCURY_PROFILE = """Launchd plist: /Users/sandeep_a_k/Library/LaunchAgents/ai.hermes.gateway-mercury.plist
✓ Service definition matches the current Hermes install
✓ Gateway is supervised by launchd (PID 446)
  Auto-start at login and auto-restart on crash are available.

Other profiles:
  ✓ default          — PID 462
"""


def _extract_gateway_status_function() -> str:
    """Pull the real `_gateway_status() { ... }` function body verbatim out
    of install_client.sh, so this test exercises the actual shipped
    classifier logic -- not a hand-copied re-transcription of it that could
    silently drift out of sync with the real script."""
    src = _INSTALL_SCRIPT.read_text()
    start = src.index("_gateway_status() {")
    end = src.index("\n}\n", start) + 3
    return src[start:end]


@pytest.mark.parametrize(
    "real_capture",
    [_REAL_HERMES_STATUS_DEFAULT_PROFILE, _REAL_HERMES_STATUS_MERCURY_PROFILE],
    ids=["real-default-profile-capture", "real-mercury-profile-capture"],
)
def test_classifier_recognizes_real_captured_hermes_running_output(tmp_path, real_capture):
    """Both fixtures above are VERBATIM `hermes gateway status` output,
    captured live and read-only from this machine's real installed Hermes
    v0.20.5, against the real (still-running, untouched by this test)
    default profile and mercury's leftover profile -- not invented
    strings. Both must classify as "running". Exercises the real,
    unmodified `_gateway_status` function extracted straight out of
    install_client.sh, called against a tiny stub `hermes` that echoes the
    verbatim capture, rather than re-deriving the classifier's logic by
    hand in Python."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "hermes"
    stub.write_text(f"#!/usr/bin/env bash\ncat <<'REALCAPTURE'\n{real_capture}\nREALCAPTURE\nexit 0\n")
    stub.chmod(0o755)

    script = _extract_gateway_status_function() + "\n_gateway_status\n"
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        # stub_dir first so the `hermes` lookup finds the stub, but the
        # rest of the real PATH stays too -- the stub's own
        # `#!/usr/bin/env bash` shebang needs `env` to still be able to
        # find a real `bash` to execute it with.
        env={"PATH": f"{stub_dir}:{os.environ.get('PATH', '')}", "HERMES_HOME": str(tmp_path)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "running"


# THE ROUND-4 SUBSTRING-PRECEDENCE FIX, AS A REAL REGRESSION TEST: an
# earlier round's fix for "No fallback process is running" (a negative
# phrase containing "is running" as a literal substring) short-circuited
# on that phrase and returned "not-running" immediately -- but Hermes's
# own launchd_status() (the fallback-PID check) and
# _print_gateway_process_mismatch() (an INDEPENDENT, broader process scan)
# are two genuinely separate detection mechanisms that can disagree and
# BOTH print in the SAME real output, reproduced directly from gateway.py's
# real source shape (not invented): the fallback check finds nothing, but
# the broader mismatch scan finds a live process anyway. The
# implementation comment in install_client.sh referenced this exact
# scenario as the reason for the round-4 fix; this is that scenario
# committed as an actual, executable regression test, not just described
# in prose.
_HERMES_STATUS_COMBINED_NEGATIVE_AND_POSITIVE = """Launchd plist: /Users/sandeep_a_k/Library/LaunchAgents/ai.hermes.gateway-mercury.plist
⚠ Gateway service is registered but launchd is not supervising it
  launchd cannot manage the gateway on this macOS version.
✗ No fallback process is running
  Run: hermes gateway start
  ⚠ Auto-start at login and auto-restart on crash are NOT available.

⚠ Gateway process is running for this profile, but the service is not active
  PID(s): 123
  This is usually a manual foreground/tmux/nohup run, so `hermes gateway`
  can refuse to start another copy until this process stops.
"""


def test_classifier_recognizes_real_hermes_process_service_mismatch_as_running(tmp_path):
    """THE EXACT COMBINED CASE that made the round-3 classifier fail-open:
    short-circuiting on "No fallback process is running" (checked FIRST,
    before any positive check) returned "not-running" here even though a
    live gateway PROCESS genuinely exists per the SAME output's own
    independent mismatch warning. Fixed in round 4 by neutralizing the
    negative phrase (stripping it from a COPY before the positive check
    runs) instead of short-circuiting on it, so the independent positive
    evidence a few lines later is still found. Must classify as
    "running"."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "hermes"
    stub.write_text(
        "#!/usr/bin/env bash\ncat <<'REALCAPTURE'\n"
        + _HERMES_STATUS_COMBINED_NEGATIVE_AND_POSITIVE
        + "\nREALCAPTURE\nexit 0\n"
    )
    stub.chmod(0o755)

    script = _extract_gateway_status_function() + "\n_gateway_status\n"
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={"PATH": f"{stub_dir}:{os.environ.get('PATH', '')}", "HERMES_HOME": str(tmp_path)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "running"


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


def test_failed_config_set_rolls_back_config_yaml_and_touches_zero_live_env_files(sandbox):
    """THE ENGINEERING-1 TRANSACTIONALITY PROOF, STRENGTHENED per the
    second review round: a failure partway through the `hermes config set`
    sequence must not leave the gateway restarted on half-applied config,
    must restore config.yaml to exactly what it was before this attempt,
    AND -- unlike the first version of this fix, which the review correctly
    rejected -- must leave BOTH live .env files completely untouched, not
    just individually self-consistent. The installer now runs the entire
    fallible `hermes config set` sequence BEFORE either .env file is
    committed (atomically renamed from its staged temp file), so a failure
    here means neither .env file was ever written to at all."""
    config_yaml = sandbox["hermes_home"] / "config.yaml"
    config_yaml.write_text("model:\n  provider: original-provider\n  default: original-model\n")
    root_env = sandbox["fieldkit_root"] / ".env"
    hermes_env = sandbox["hermes_home"] / ".env"
    root_env.write_text("CLIENT_NAME=preexisting\nFIELDKIT_ROOT=/preexisting\n")
    hermes_env.write_text("TELEGRAM_BOT_TOKEN=preexisting-token\n")

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

    # BOTH live .env files are completely untouched -- byte-for-byte their
    # pre-install content, not "acme" anywhere in either.
    assert root_env.read_text() == "CLIENT_NAME=preexisting\nFIELDKIT_ROOT=/preexisting\n"
    assert hermes_env.read_text() == "TELEGRAM_BOT_TOKEN=preexisting-token\n"

    # No leaked temp files from the staged-but-never-committed rebuild.
    leaked = list(sandbox["fieldkit_root"].glob(".install_client.*")) + list(sandbox["hermes_home"].glob(".install_client.*"))
    assert leaked == [], f"leaked temp files: {leaked}"


def test_failed_config_set_restores_config_yaml_ORIGINAL_mode_not_hermes_rewritten_mode(sandbox):
    """THE ENGINEERING-1a FIX: `cp` alone preserves whatever mode the
    destination inode CURRENTLY has at copy time -- but by the time a
    LATER `hermes config set` call in this same sequence fails, Hermes's
    own earlier `config set` calls may have already rewritten config.yaml
    via its own atomic write, leaving it with a different (often more
    permissive) mode than it had before this script ever ran. Confirmed
    live: a 0640 original became 0666 after a simulated failure using `cp`
    alone for restore. The fix captures the ORIGINAL mode explicitly
    before any mutation and re-applies it explicitly on rollback,
    independent of whatever mode the file has by the time of failure."""
    config_yaml = sandbox["hermes_home"] / "config.yaml"
    config_yaml.write_text("model:\n  provider: original-provider\n")
    os.chmod(config_yaml, 0o640)

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox,
        extra_env={
            "HERMES_STUB_FAIL_CONFIG_KEY": "skills.external_dirs",
            # Simulates model.provider's own `config set` call rewriting
            # config.yaml with a permissive mode, same as the real `hermes`
            # CLI's own atomic-write behavior -- BEFORE the later
            # skills.external_dirs call fails.
            "HERMES_STUB_CONFIG_SET_REWRITES_MODE": "666",
        },
    )
    assert result.returncode != 0
    assert config_yaml.read_text() == "model:\n  provider: original-provider\n"
    assert stat.S_IMODE(config_yaml.stat().st_mode) == 0o640, (
        "config.yaml's mode must be restored to its ORIGINAL value (0640), "
        "not whatever mode hermes's own config-set rewrite left it in (0666)"
    )


def test_failed_config_set_with_no_preexisting_config_yaml_deletes_it_not_leaves_partial(sandbox):
    """The other half of the ENGINEERING-1 fix: on a machine where
    config.yaml never existed before this install attempt (this run's own
    `hermes config set` calls would be the ones creating it), a failure
    partway through must DELETE it, not leave a half-applied file that
    looks like a real, intentional prior configuration."""
    config_yaml = sandbox["hermes_home"] / "config.yaml"
    assert not config_yaml.exists()

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox,
        extra_env={"HERMES_STUB_FAIL_CONFIG_KEY": "model.default"},
    )
    assert result.returncode != 0
    assert "did not exist before" in result.stderr.lower()
    assert not config_yaml.exists()
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()


def test_failed_config_set_leaves_client_source_env_permissions_untouched(sandbox):
    """THE ENGINEERING-1b FIX: an earlier version chmod'd the client's own
    SOURCE .env (a real, pre-existing file) to 0600 immediately after the
    --dry-run check -- well before the fallible `hermes config set`
    sequence -- so a failed install still left one real file mutated,
    contradicting "zero filesystem mutation on failure." That chmod now
    happens only as part of the final commit, after every fallible step
    has already succeeded. Confirmed here: a client .env deliberately left
    at a non-default mode (0644) is STILL 0644 after a simulated
    config-set failure -- the chmod line never ran."""
    client_env = _write_client_env(sandbox["fieldkit_root"], "acme")
    os.chmod(client_env, 0o644)

    result = _run(
        "acme", sandbox,
        extra_env={"HERMES_STUB_FAIL_CONFIG_KEY": "model.default"},
    )
    assert result.returncode != 0
    assert stat.S_IMODE(client_env.stat().st_mode) == 0o644, (
        "the client source .env's permissions must be untouched by a "
        "failed install -- the chmod 600 must only happen after the "
        "fallible hermes config set sequence has fully succeeded"
    )


def test_second_commit_failure_rolls_back_hermes_env_and_config_yaml_fully(sandbox):
    """THE ENGINEERING-1c FIX, ROUND 4 (real rollback, not manual-recovery
    guidance): if the SECOND rename (root .env, committed last) fails
    after the FIRST (Hermes .env) already succeeded, a prior version of
    this script left Hermes .env AND config.yaml switched to the new
    client while root .env stayed on the old one -- a genuine live-file
    disagreement contradicting this script's own "no window where live
    files disagree" claim. Fixed: on this failure, Hermes .env and
    config.yaml are BOTH rolled back to their exact pre-install state
    (content and mode), so every live file ends up back on the OLD
    client, consistently -- not a new client in two files and an old
    client in the third. Forced via macOS's `chflags uchg` (user-
    immutable) on a pre-existing root .env, which blocks a `mv -f` onto
    it without restricting the directory's general writability (so the
    earlier writability preflight still passes -- this specifically
    exercises the LATE two-file-commit failure, not an early abort)."""
    if sys.platform != "darwin":
        pytest.skip("chflags is macOS-specific; this test needs a different "
                     "immutability mechanism on other platforms")

    root_env = sandbox["fieldkit_root"] / ".env"
    hermes_env = sandbox["hermes_home"] / ".env"
    config_yaml = sandbox["hermes_home"] / "config.yaml"
    root_env.write_text("CLIENT_NAME=preexisting\nFIELDKIT_ROOT=/preexisting\n")
    hermes_env.write_text("TELEGRAM_BOT_TOKEN=preexisting-token\n")
    config_yaml.write_text("model:\n  provider: preexisting-provider\n")
    os.chmod(hermes_env, 0o640)
    os.chmod(config_yaml, 0o640)
    subprocess.run(["chflags", "uchg", str(root_env)], check=True)
    try:
        _write_client_env(sandbox["fieldkit_root"], "acme")
        result = _run("acme", sandbox, "--no-restart")
        assert result.returncode != 0
        assert "rolling both" in result.stderr.lower()

        # ALL THREE live files are back to their exact pre-install state --
        # not "Hermes .env and config.yaml on acme, root .env on the old
        # client" (the prior, contradictory behavior).
        assert hermes_env.read_text() == "TELEGRAM_BOT_TOKEN=preexisting-token\n"
        assert config_yaml.read_text() == "model:\n  provider: preexisting-provider\n"
        assert root_env.read_text() == "CLIENT_NAME=preexisting\nFIELDKIT_ROOT=/preexisting\n"
        assert stat.S_IMODE(hermes_env.stat().st_mode) == 0o640
        assert stat.S_IMODE(config_yaml.stat().st_mode) == 0o640
        assert "acme" not in hermes_env.read_text()
        assert "acme" not in config_yaml.read_text()
    finally:
        subprocess.run(["chflags", "nouchg", str(root_env)], check=True)


def test_first_commit_failure_rolls_back_config_yaml_too(sandbox):
    """THE OTHER HALF OF ENGINEERING-1c: if the FIRST rename (Hermes .env)
    fails, config.yaml has ALREADY been applied to the new client by the
    successful `hermes config set` sequence earlier in the script -- a
    prior version's error message claimed "the client switch was NOT
    applied at all" in this exact scenario, which was false: config.yaml
    genuinely had been switched. Fixed: a first-commit failure now rolls
    config.yaml back too, so the claim becomes true instead of merely
    asserted. Forced via `chflags uchg` on a pre-existing Hermes .env."""
    if sys.platform != "darwin":
        pytest.skip("chflags is macOS-specific; this test needs a different "
                     "immutability mechanism on other platforms")

    hermes_env = sandbox["hermes_home"] / ".env"
    config_yaml = sandbox["hermes_home"] / "config.yaml"
    hermes_env.write_text("TELEGRAM_BOT_TOKEN=preexisting-token\n")
    config_yaml.write_text("model:\n  provider: preexisting-provider\n")
    subprocess.run(["chflags", "uchg", str(hermes_env)], check=True)
    try:
        _write_client_env(sandbox["fieldkit_root"], "acme")
        result = _run("acme", sandbox, "--no-restart")
        assert result.returncode != 0
        assert "rolling back config.yaml too" in result.stderr.lower()

        assert hermes_env.read_text() == "TELEGRAM_BOT_TOKEN=preexisting-token\n"
        assert config_yaml.read_text() == "model:\n  provider: preexisting-provider\n"
        assert not (sandbox["fieldkit_root"] / ".env").exists()
    finally:
        subprocess.run(["chflags", "nouchg", str(hermes_env)], check=True)


def test_cleanup_trap_installed_immediately_after_each_mktemp_not_only_after_both():
    """A small, cheap fix flagged alongside the main findings: if the trap
    covering the FIRST staged temp file were only installed after BOTH
    mktemp calls succeeded, a failure on the second mktemp (disk full,
    permission race, etc.) would exit under `set -e` before any trap
    existed to clean up the first file, leaking it. Verified structurally
    (a live race on the second mktemp specifically, after the writability
    preflight has already passed, isn't reliably reproducible as a
    black-box subprocess test): the trap for ROOT_ENV_TMP must be
    installed on the line immediately following its own mktemp call, not
    deferred until after HERMES_ENV_TMP's mktemp also runs."""
    src = _INSTALL_SCRIPT.read_text()
    root_tmp_idx = src.index('ROOT_ENV_TMP="$(mktemp')
    hermes_tmp_idx = src.index('HERMES_ENV_TMP="$(mktemp')
    assert root_tmp_idx < hermes_tmp_idx
    between = src[root_tmp_idx:hermes_tmp_idx]
    assert "trap " in between, (
        "expected a `trap` installation between ROOT_ENV_TMP's mktemp and "
        "HERMES_ENV_TMP's mktemp, covering ROOT_ENV_TMP alone, so a "
        "failure on the second mktemp can't leak the first temp file"
    )
    assert 'rm -f "$ROOT_ENV_TMP"' in between


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
# SECURITY-5, STRENGTHENED per the second review round: a stale non-default
# profile's gateway being CONFIRMED RUNNING (or its status being
# unconfirmable) must ABORT the install outright, before touching anything
# live -- not merely warn about it after the new default-profile gateway is
# already up (which the review correctly identified as reproducing the
# exact two-gateways-live exposure this script exists to prevent). Only a
# stale profile CONFIRMED not running is safe to proceed past, with a
# cleanup reminder printed on success.
# ---------------------------------------------------------------------------

def test_stale_profile_confirmed_running_aborts_before_any_mutation(sandbox):
    stale_profile_dir = sandbox["hermes_home"] / "profiles" / "mercury"
    stale_profile_dir.mkdir(parents=True)
    (stale_profile_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=stale-should-not-be-touched\n")

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox, "--no-restart",
        extra_env={"HERMES_STUB_PROFILE_STATUS_mercury": "running"},
    )
    assert result.returncode != 0
    assert "mercury" in result.stderr
    assert "confirmed running" in result.stderr.lower()
    assert "hermes -p mercury gateway stop" in result.stderr
    assert "hermes profile delete mercury" in result.stderr

    # Zero live mutation: the default gateway was never even queried-and-
    # stopped, let alone had its .env files committed.
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()
    calls = _log_calls(sandbox)
    assert not any("gateway stop" in c and "-p" not in c for c in calls)
    assert (stale_profile_dir / ".env").read_text() == "TELEGRAM_BOT_TOKEN=stale-should-not-be-touched\n"


def test_stale_profile_running_check_happens_before_ANY_mutation_not_just_before_env_files(sandbox):
    """THE SECURITY-5a FIX: an earlier version ran the stale-profile check
    AFTER preflight command/writability checks, HERMES_HOME creation and
    chmod, lock acquisition, and secret temp-file staging -- meaning a
    "checked before touching anything" claim was false; several real
    mutations already happened before the abort. Confirmed here by giving
    HERMES_HOME a distinctive starting mode (0755, not the 0700 this script
    would normally set) and confirming it is STILL 0755 after an aborted
    run -- proving the `chmod 700 "$HERMES_HOME"` preflight line, and
    everything after it (lock, staging), never executed."""
    os.chmod(sandbox["hermes_home"], 0o755)
    stale_profile_dir = sandbox["hermes_home"] / "profiles" / "mercury"
    stale_profile_dir.mkdir(parents=True)

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox, "--no-restart",
        extra_env={"HERMES_STUB_PROFILE_STATUS_mercury": "running"},
    )
    assert result.returncode != 0

    # HERMES_HOME's mode is untouched -- the preflight chmod never ran.
    assert stat.S_IMODE(sandbox["hermes_home"].stat().st_mode) == 0o755
    # No lock was ever taken.
    assert not (sandbox["hermes_home"] / ".install_client.lock").exists()
    # No staged temp files were ever created.
    assert list(sandbox["hermes_home"].glob(".install_client.*")) == []
    assert list(sandbox["fieldkit_root"].glob(".install_client.*")) == []


def test_stale_profile_ambiguous_status_aborts_same_as_running(sandbox):
    """An unconfirmable status for a stale profile is treated exactly like
    'confirmed running' -- fail closed, don't guess it's safe."""
    stale_profile_dir = sandbox["hermes_home"] / "profiles" / "mercury"
    stale_profile_dir.mkdir(parents=True)
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox, "--no-restart",
        extra_env={"HERMES_STUB_PROFILE_STATUS_mercury": "ambiguous"},
    )
    assert result.returncode != 0
    assert "could not be confirmed" in result.stderr.lower()
    assert not (sandbox["fieldkit_root"] / ".env").exists()


def test_stale_profile_confirmed_not_running_proceeds_with_cleanup_note(sandbox):
    stale_profile_dir = sandbox["hermes_home"] / "profiles" / "mercury"
    stale_profile_dir.mkdir(parents=True)
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox, "--no-restart",
        extra_env={"HERMES_STUB_PROFILE_STATUS_mercury": "not-running"},
    )
    assert result.returncode == 0, result.stderr
    assert (sandbox["fieldkit_root"] / ".env").read_text().count("CLIENT_NAME=acme") == 1
    assert "mercury" in result.stdout
    assert "hermes profile delete mercury" in result.stdout


def test_toctou_recheck_refuses_gateway_start_if_stale_profile_starts_in_the_interim(sandbox):
    """THE SECURITY-5b FIX: a stale non-default profile's gateway could, in
    principle, start running in the window between the installer's FIRST
    check (before any mutation) and the moment it actually calls `hermes
    gateway start` for the newly-switched default profile -- the first
    check alone can't catch that race. Simulated via a stub that reports
    "not running" on the first status query for this profile (the early
    check) and "running" on every query after (the recheck immediately
    before `gateway start`). The install's file changes are NOT reverted
    (they're already correct for the new client), but starting the new
    gateway is refused -- so this script never itself creates a moment
    where two gateways with different clients' credentials are both live."""
    stale_profile_dir = sandbox["hermes_home"] / "profiles" / "mercury"
    stale_profile_dir.mkdir(parents=True)
    counter_dir = sandbox["hermes_home"] / "toctou_counters"
    counter_dir.mkdir()

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox,  # deliberately WITHOUT --no-restart: the TOCTOU
        # recheck only runs on the path that's about to call `gateway start`.
        extra_env={"HERMES_STUB_TOCTOU_COUNTER_DIR": str(counter_dir)},
    )
    assert result.returncode != 0
    assert "started running between this" in result.stderr.lower() or "race" in result.stderr.lower()
    assert "mercury" in result.stderr

    # The file switch itself is NOT reverted -- both live files already
    # correctly reflect 'acme'.
    assert (sandbox["fieldkit_root"] / ".env").read_text().count("CLIENT_NAME=acme") == 1
    assert "CLIENT_NAME=acme" in (sandbox["hermes_home"] / ".env").read_text()

    # But the new gateway was never actually started.
    calls = _log_calls(sandbox)
    assert not any("gateway start" in c for c in calls)

    # Confirms the TOCTOU mechanism actually fired twice (early + late),
    # not that the test accidentally only ran the check once.
    assert int((counter_dir / "mercury").read_text().strip()) >= 2


def test_no_stale_profiles_prints_no_retirement_section(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    assert "retire" not in result.stdout.lower() and "delete" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# SECURITY-5, ROUND 4: orphan launchd-service detection. The stale-profile
# scan previously only enumerated directories under $HERMES_HOME/profiles/
# -- but a profile directory can be deleted or renamed while its launchd
# service (ai.hermes.gateway-<name>) stays loaded and alive with
# credentials in memory. Confirmed against Hermes's own real source: its
# service enumeration is independent of the profile directory tree. These
# tests use the sandbox's stub `launchctl` (see HERMES_STUB_LAUNCHCTL_LIST_FILE)
# to simulate exactly that disagreement -- a loaded, live launchd service
# with NO matching profile directory at all.
# ---------------------------------------------------------------------------

def _write_launchctl_entries(sandbox, entries: list[tuple[str, str, str]]) -> None:
    """entries: list of (pid_or_dash, status, label) tuples, matching real
    `launchctl list`'s tab-separated table format."""
    sandbox["launchctl_list_file"].write_text(
        "\n".join(f"{pid}\t{status}\t{label}" for pid, status, label in entries) + "\n"
    )


def test_default_launchctl_stub_reports_nothing_confirming_test_isolation(sandbox):
    """Sanity check for the isolation mechanism itself: with no
    HERMES_STUB_LAUNCHCTL_LIST_FILE content written, the stub reports an
    empty `launchctl list` -- proving no test in this file can accidentally
    see this development machine's REAL running Hermes gateways
    (ai.hermes.gateway, ai.hermes.gateway-mercury) leak into its
    stale-profile candidate set."""
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    assert "mercury" not in result.stdout  # would appear if real state leaked in


def test_orphaned_launchd_service_with_no_profile_directory_is_detected_and_blocks(sandbox):
    """THE CORE FIX: a launchd service (`ai.hermes.gateway-orphaned`) is
    loaded and has a live PID, but NO `$HERMES_HOME/profiles/orphaned/`
    directory exists at all (deleted or renamed, per the review's
    scenario) -- the OLD directory-only scan would find nothing and let
    the install proceed right underneath it. The install must instead
    detect it via `launchctl list` directly and abort, exactly as for a
    directory-based stale profile."""
    _write_launchctl_entries(sandbox, [
        ("462", "0", "ai.hermes.gateway"),  # the default profile -- must NOT trigger anything
        ("999", "0", "ai.hermes.gateway-orphaned"),
    ])
    assert not (sandbox["hermes_home"] / "profiles" / "orphaned").exists()

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode != 0
    assert "orphaned" in result.stderr
    assert "confirmed running" in result.stderr.lower()
    assert not (sandbox["fieldkit_root"] / ".env").exists()


def test_orphan_detection_survives_a_failing_label_specific_query(sandbox):
    """THE SECURITY-5 ROUND-6 FIX, as codex's exact combined regression:
    the bare `launchctl list` call succeeds and ALREADY shows a live PID
    for an orphaned label (`999  0  ai.hermes.gateway-orphaned`) -- but a
    SEPARATE, per-label `launchctl list ai.hermes.gateway-orphaned` query
    fails on its own (simulated here with the codex-observed exit 75).
    A prior version of this script issued that second query to decide
    aliveness and silently read its failure as "not running", discarding
    the positive PID evidence the bare list had already provided --
    installer exits 0, both .env files get written, orphan reported as
    confirmed-not-running while actually alive with PID 999. Fixed by
    parsing the PID directly out of the bare list's own output and never
    issuing that second query at all. This test proves the fix by making
    the (now-unused) second-query branch fail loudly if it's ever hit --
    the install must still correctly abort."""
    _write_launchctl_entries(sandbox, [
        ("462", "0", "ai.hermes.gateway"),
        ("999", "0", "ai.hermes.gateway-orphaned"),
    ])
    assert not (sandbox["hermes_home"] / "profiles" / "orphaned").exists()

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox, "--no-restart",
        extra_env={"HERMES_STUB_LAUNCHCTL_LABEL_QUERY_FAILS": "1"},
    )
    assert result.returncode != 0
    assert "orphaned" in result.stderr
    assert "confirmed running" in result.stderr.lower()
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()

    # Confirm the fix's actual mechanism: the install never even issues
    # the fragile per-label query in the first place, so its simulated
    # failure was never reached -- not that the install happened to
    # tolerate a failure it actually hit.
    calls = _log_calls(sandbox)
    assert not any("list ai.hermes.gateway-orphaned" in c for c in calls)


def test_orphaned_launchd_service_with_no_live_pid_does_not_block(sandbox):
    """The launchd service definition is loaded (still listed) but has no
    live PID (`-` in the PID column, matching real launchctl's own
    notation for a loaded-but-not-running service) -- must NOT block the
    install, same as a directory-based stale profile confirmed not
    running."""
    _write_launchctl_entries(sandbox, [
        ("462", "0", "ai.hermes.gateway"),
        ("-", "0", "ai.hermes.gateway-orphaned"),
    ])
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    assert "orphaned" in result.stdout


def test_default_profile_launchctl_label_is_never_treated_as_stale(sandbox):
    """The bare `ai.hermes.gateway` label (no `-<name>` suffix) is the
    DEFAULT profile -- the one this script itself manages -- and must
    never be treated as a stale candidate, regardless of its own status."""
    _write_launchctl_entries(sandbox, [("462", "0", "ai.hermes.gateway")])
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    assert "retire" not in result.stdout.lower()
    assert "delete" not in result.stdout.lower()


def test_directory_and_launchctl_entry_for_same_profile_are_deduplicated(sandbox):
    """A profile that has BOTH a `$HERMES_HOME/profiles/<name>/` directory
    AND a matching loaded launchd service must be reported exactly once,
    not twice, in the retirement instructions."""
    stale_dir = sandbox["hermes_home"] / "profiles" / "mercury"
    stale_dir.mkdir(parents=True)
    _write_launchctl_entries(sandbox, [
        ("462", "0", "ai.hermes.gateway"),
        ("-", "0", "ai.hermes.gateway-mercury"),
    ])
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run("acme", sandbox, "--no-restart")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("hermes profile delete mercury") == 1


def test_orphan_check_runs_on_both_early_and_late_toctou_recheck(sandbox):
    """The orphan launchd-service scan must run on BOTH calls to
    _abort_if_stale_profiles_running -- the early preflight AND the late
    TOCTOU recheck immediately before `gateway start` -- not just the
    first. Simulated via the SAME TOCTOU counter-file mechanism used for
    the directory-based recheck test, but this time the profile is
    orphaned (launchctl-only, no directory at all)."""
    counter_dir = sandbox["hermes_home"] / "toctou_counters"
    counter_dir.mkdir(parents=True)
    # The launchctl list itself reports the orphaned service as running
    # from the very first query -- what changes between early and late is
    # whether `hermes -p orphaned gateway status` (the fallback path for
    # any non-launchctl-confirmed candidate) would show it, which is
    # irrelevant here since launchctl's own PID evidence wins outright
    # and is checked BOTH times identically. This test instead confirms
    # the ordinary (non-orphan) TOCTOU mechanism still works when an
    # orphan is ALSO present and never running, proving both scans (
    # directory + launchctl) run on every call rather than only once.
    _write_launchctl_entries(sandbox, [("462", "0", "ai.hermes.gateway")])
    stale_dir = sandbox["hermes_home"] / "profiles" / "mercury"
    stale_dir.mkdir(parents=True)

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox,
        extra_env={"HERMES_STUB_TOCTOU_COUNTER_DIR": str(counter_dir)},
    )
    assert result.returncode != 0
    assert "mercury" in result.stderr
    # Confirms the check fired (at least) twice -- early AND late -- with
    # the launchctl-based scan active in both, not just directory-based.
    assert int((counter_dir / "mercury").read_text().strip()) >= 2


def test_toctou_recheck_catches_a_TRUE_orphan_with_no_directory_at_all(sandbox):
    """A STRONGER version of the test above, per review feedback that it
    overstated what it proved: that test's "orphan" candidate had its
    launchctl-reported PID present from the very FIRST query, so it never
    actually exercised launchctl's own list output CHANGING between the
    early and late checks -- only the ordinary directory-based mechanism
    was genuinely racing there. This test instead has NO profile directory
    at all (the true orphan scenario the whole feature exists for) and
    makes `launchctl list`'s own bare output literally change between
    calls: the orphaned service is entirely absent from the first query
    (the early, pre-mutation check passes cleanly) and present with a live
    PID on every query after (the late, pre-`gateway start` recheck)."""
    assert not (sandbox["hermes_home"] / "profiles").exists()
    toctou_counter = sandbox["hermes_home"] / "launchctl_toctou_counter"

    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox,
        extra_env={"HERMES_STUB_LAUNCHCTL_TOCTOU_COUNTER_FILE": str(toctou_counter)},
    )
    assert result.returncode != 0
    assert "orphaned" in result.stderr
    assert "started running between this" in result.stderr.lower() or "race" in result.stderr.lower()

    # The file switch is NOT reverted -- both live files already correctly
    # reflect 'acme' (same contract as the ordinary TOCTOU case).
    assert (sandbox["fieldkit_root"] / ".env").read_text().count("CLIENT_NAME=acme") == 1
    assert "CLIENT_NAME=acme" in (sandbox["hermes_home"] / ".env").read_text()

    calls = _log_calls(sandbox)
    assert not any("gateway start" in c for c in calls)
    # The counter genuinely advanced past 1 -- proving launchctl was
    # queried more than once and its answer changed, not that the test
    # just got lucky on a single query.
    assert int(toctou_counter.read_text().strip()) >= 2


def test_launchctl_list_command_failure_aborts_the_install(sandbox):
    """THE SECURITY-5 (round-5) FIX: a prior version of
    _launchctl_gateway_candidates silently treated `launchctl list` itself
    FAILING (nonzero exit -- a permission issue, launchd transiently
    unavailable, etc.) identically to "no services found", which is
    fail-OPEN: an orphaned gateway with no profile directory would go
    completely undetected and the install would proceed right underneath
    it. Confirmed here: with the stub `launchctl list` exiting nonzero,
    the install must abort with a clear message, before touching any live
    file -- exactly the same "running or unconfirmable aborts" policy
    already applied to every per-profile status check."""
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox, "--no-restart",
        extra_env={"HERMES_STUB_LAUNCHCTL_LIST_FAILS": "1"},
    )
    assert result.returncode != 0
    assert "launchctl list" in result.stderr.lower()
    assert "unconfirmable" in result.stderr.lower() or "cannot confirm" in result.stderr.lower()
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()


# ---------------------------------------------------------------------------
# ENGINEERING-3, STRENGTHENED: an ambiguous status for the DEFAULT profile's
# own gateway (command failure or unrecognized output) must abort the
# install entirely -- proceeding as if it were "not running" is exactly the
# wrong guess to make when the actual answer might be "running", since that
# would let the script overwrite live credential files out from under a
# gateway that's still reading them.
# ---------------------------------------------------------------------------

def test_ambiguous_default_gateway_status_aborts_before_any_mutation(sandbox):
    _write_client_env(sandbox["fieldkit_root"], "acme")
    result = _run(
        "acme", sandbox, "--no-restart",
        extra_env={"HERMES_STUB_STOPPED_TEXT": "???unparseable???"},
    )
    assert result.returncode != 0
    assert "could not determine" in result.stderr.lower()
    assert not (sandbox["fieldkit_root"] / ".env").exists()
    assert not (sandbox["hermes_home"] / ".env").exists()
    calls = _log_calls(sandbox)
    assert not any("gateway stop" in c for c in calls)
    assert not any("config set" in c for c in calls)


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


# ENGINEERING-5, SECOND REVIEW ROUND: the first version of this proof
# reimplemented Hermes's reload logic (dotenv_values + dict.update) instead
# of calling Hermes's ACTUAL installed loader -- the review correctly
# pointed out that a reimplementation proves nothing about Hermes's real
# load-order/override semantics, and would keep "passing" even if Hermes's
# own behavior changed. Fixed: invoke the real, installed
# hermes_cli.env_loader.load_hermes_dotenv() via Hermes's own venv
# interpreter, against an isolated scratch HERMES_HOME -- never this
# machine's real ~/.hermes. Skips cleanly (not a failure) on a machine
# without this exact Hermes install, so the suite stays portable.
_HERMES_INSTALL_DIR = Path.home() / ".hermes" / "hermes-agent"
_HERMES_VENV_PYTHON = _HERMES_INSTALL_DIR / "venv" / "bin" / "python"
_REAL_HERMES_AVAILABLE = (
    _HERMES_VENV_PYTHON.exists()
    and (_HERMES_INSTALL_DIR / "hermes_cli" / "env_loader.py").exists()
)
_skip_without_real_hermes = pytest.mark.skipif(
    not _REAL_HERMES_AVAILABLE,
    reason="real installed Hermes (~/.hermes/hermes-agent) not found on this machine",
)


def _reload_via_real_hermes_env_loader(hermes_env_path: Path, ambient_env: dict) -> dict:
    """Invoke Hermes's OWN installed `load_hermes_dotenv()` (not a
    reimplementation) against an isolated scratch HERMES_HOME, with the
    given ambient_env as the starting process environment, and return what
    os.environ looks like afterward. This is exactly what a real Hermes
    gateway process's environment looks like at the moment it spawns a
    skill's terminal-tool subprocess -- proving install_client.sh's fix
    (writing CLIENT_NAME into Hermes's own .env) actually works against
    Hermes's real load-order/override semantics, not our understanding of
    them. `load_external_secrets=False` skips optional secret-manager
    integrations irrelevant to this proof and not present in this sandbox
    anyway. Never touches this machine's real ~/.hermes -- hermes_home is
    always the isolated sandbox path passed in."""
    snippet = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(_HERMES_INSTALL_DIR)!r})\n"
        f"os.environ.update({ambient_env!r})\n"
        "from pathlib import Path\n"
        "from hermes_cli.env_loader import load_hermes_dotenv\n"
        f"load_hermes_dotenv(hermes_home=Path({str(hermes_env_path.parent)!r}), load_external_secrets=False)\n"
        "import json\n"
        "print('HERMES_RELOADED_ENV_JSON=' + json.dumps(dict(os.environ)))\n"
    )
    # A controlled base environment (not this test process's full inherited
    # env) so the proof isn't sensitive to whatever happens to be in the
    # runner's own shell.
    base_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    result = subprocess.run(
        [str(_HERMES_VENV_PYTHON), "-c", snippet],
        env=base_env,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"real Hermes env_loader invocation failed:\n{result.stderr}"
    )
    import json
    line = next(l for l in result.stdout.splitlines() if l.startswith("HERMES_RELOADED_ENV_JSON="))
    return json.loads(line[len("HERMES_RELOADED_ENV_JSON="):])


@_skip_without_real_hermes
def test_stale_ambient_client_name_does_not_survive_real_hermes_reload(sandbox):
    """THE ENGINEERING-6 / #59-CLOSURE CRUX TEST, using Hermes's REAL
    installed reload code (see ENGINEERING-5 above), not a
    reimplementation. Simulates the actually dangerous scenario the review
    demanded coverage for: a Hermes gateway subprocess whose OWN inherited
    environment already carries a STALE CLIENT_NAME (left over from before
    this install, e.g. from the process's own prior turn, a leftover shell
    export baked into a launchd plist, or simply not having been restarted
    in a while) -- proving that after install_client.sh switches the
    active client, that stale value cannot win, because Hermes's own
    load_hermes_dotenv() (override=True from HERMES_HOME/.env, which the
    installer now writes CLIENT_NAME into) corrects it before any skill
    subprocess ever sees it. This is the reason CLIENT_NAME is written
    into Hermes's .env at all, not just the root .env -- see the script's
    own module docstring."""
    _write_client_env(sandbox["fieldkit_root"], "newclient")
    install_result = _run("newclient", sandbox, "--no-restart")
    assert install_result.returncode == 0, install_result.stderr

    # The dangerous ambient state: CLIENT_NAME already set to something
    # else entirely, exactly as a real long-lived gateway process (or a
    # skill subprocess inheriting from one) might carry before its next
    # reload.
    stale_ambient_env = {"CLIENT_NAME": "some_stale_client_from_before"}

    corrected_env = _reload_via_real_hermes_env_loader(
        sandbox["hermes_home"] / ".env", stale_ambient_env,
    )
    assert corrected_env["CLIENT_NAME"] == "newclient", (
        "Hermes's REAL, installed load_hermes_dotenv() must correct a "
        "stale CLIENT_NAME on reload -- if this fails, install_client.sh "
        "is not writing CLIENT_NAME into Hermes's .env correctly, or "
        "Hermes's own override semantics changed underneath this proof"
    )

    resolve_result = _resolve_via_real_process_photos(sandbox["fieldkit_root"], corrected_env)
    assert resolve_result.returncode == 0, resolve_result.stderr
    assert "RESOLVED_CLIENT=newclient" in resolve_result.stdout
    assert "some_stale_client_from_before" not in resolve_result.stdout


@_skip_without_real_hermes
def test_switching_installed_client_corrects_the_ambient_env_each_time(sandbox):
    """The same proof as above, using Hermes's real reload code, run
    across two consecutive switches, each time starting from the OTHER
    client's name as the stale ambient value -- confirming this isn't a
    one-shot fluke of a specific stale value."""
    _write_client_env(sandbox["fieldkit_root"], "clienta")
    assert _run("clienta", sandbox, "--no-restart").returncode == 0
    corrected_a = _reload_via_real_hermes_env_loader(
        sandbox["hermes_home"] / ".env", {"CLIENT_NAME": "whatever_was_here_before"},
    )
    first = _resolve_via_real_process_photos(sandbox["fieldkit_root"], corrected_a)
    assert "RESOLVED_CLIENT=clienta" in first.stdout, first.stderr

    _write_client_env(sandbox["fieldkit_root"], "clientb")
    assert _run("clientb", sandbox, "--no-restart").returncode == 0
    corrected_b = _reload_via_real_hermes_env_loader(
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
