"""
Tests for issue #45 — per-client CLIENT_NAME resolution under concurrent
multi-client cron/gateway operation.

Root problem: process_photos.py, check_approval.py, upload_facebook.py, and
run_e2e_test.py all resolve CLIENT_NAME via a single shared root .env, with
no per-invocation override — so two clients' cron-driven flows could not
correctly coexist on one machine.

Empirical finding (verified below, not assumed): load_dotenv()'s default
`override=False` means it never clobbers a CLIENT_NAME already present in
the process environment. So `env CLIENT_NAME=<client> python3 ...` prefixed
on a crontab entry (or any invocation) already takes precedence over the
shared root .env's CLIENT_NAME, with zero code changes required — this is
the mechanism these tests lock in as a guarded contract, since the two
`load_dotenv()` calls are easy to reorder or accidentally flip `override=`
on without any of this being obviously wrong at a glance.

These guards fire at module import time, so tests run each script in a
clean subprocess with a controlled environment (same approach as
test_env_loading.py).
"""

import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PLATFORM_PHOTO_AGENT = Path(__file__).parents[1]

# The four scripts issue #45 names as resolving CLIENT_NAME via the shared
# root .env with no per-invocation override.
_AFFECTED_MODULES = [
    "process_photos",
    "check_approval",
    "upload_facebook",
    "run_e2e_test",
]


def _run(script_snippet: str, env: dict) -> subprocess.CompletedProcess:
    """Run a Python snippet in a subprocess under platform/photo-agent/."""
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        **env,
    }
    return subprocess.run(
        [sys.executable, "-c", script_snippet],
        env=clean_env,
        capture_output=True,
        text=True,
        cwd=str(_PLATFORM_PHOTO_AGENT),
    )


def _import_and_print_client_snippet(module: str) -> str:
    return (
        "import sys; sys.path.insert(0, '.'); "
        f"from scripts import {module} as m; "
        "print('RESOLVED_CLIENT=' + m._CLIENT)"
    )


def _base_env(tmp_path: Path) -> dict:
    return {
        "FIELDKIT_ROOT": str(tmp_path),
        "FIELDKIT_DATA_DIR": str(tmp_path / "data"),
        "FIELDKIT_LOG_DIR": str(tmp_path / "logs"),
    }


# ---------------------------------------------------------------------------
# An env-var CLIENT_NAME already set on the process wins over the shared
# root .env — the mechanism that makes per-cron-entry / per-invocation
# overrides safe.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", _AFFECTED_MODULES)
def test_preset_client_name_env_var_wins_over_root_env(tmp_path, module):
    """A CLIENT_NAME already in the environment overrides the root .env's value."""
    root_env = tmp_path / ".env"
    root_env.write_text("CLIENT_NAME=_demo\n")

    env = _base_env(tmp_path)
    env["CLIENT_NAME"] = "venus"  # simulates `env CLIENT_NAME=venus python3 ...`

    result = _run(_import_and_print_client_snippet(module), env=env)

    assert result.returncode == 0, result.stderr
    assert "RESOLVED_CLIENT=venus" in result.stdout
    # The shared root .env itself must never be touched by an inline override.
    assert root_env.read_text() == "CLIENT_NAME=_demo\n"


@pytest.mark.parametrize("module", _AFFECTED_MODULES)
def test_no_override_falls_back_to_root_env(tmp_path, module):
    """With no inline override, today's single-client posture is unaffected:
    CLIENT_NAME comes from the shared root .env exactly as before."""
    root_env = tmp_path / ".env"
    root_env.write_text("CLIENT_NAME=_demo\n")

    env = _base_env(tmp_path)  # no CLIENT_NAME pre-set

    result = _run(_import_and_print_client_snippet(module), env=env)

    assert result.returncode == 0, result.stderr
    assert "RESOLVED_CLIENT=_demo" in result.stdout


# ---------------------------------------------------------------------------
# Real concurrency: two clients' processes, launched at the same time against
# the *same* shared root .env, each resolve to their own CLIENT_NAME with no
# cross-contamination and no write to the shared file.
# ---------------------------------------------------------------------------

def test_two_clients_resolve_correctly_when_run_concurrently(tmp_path):
    """Two overlapping subprocess invocations, each with its own inline
    CLIENT_NAME override, against one shared root .env whose own CLIENT_NAME
    matches neither — reproduces two cron entries firing in the same minute
    for different clients."""
    root_env = tmp_path / ".env"
    root_env.write_text("CLIENT_NAME=_demo\n")
    root_env_before = root_env.read_text()

    env_a = _base_env(tmp_path)
    env_a["CLIENT_NAME"] = "venus"
    env_b = _base_env(tmp_path)
    env_b["CLIENT_NAME"] = "mercury"

    snippet = _import_and_print_client_snippet("upload_facebook")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(_run, snippet, env_a)
        future_b = pool.submit(_run, snippet, env_b)
        result_a = future_a.result()
        result_b = future_b.result()

    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode == 0, result_b.stderr
    assert "RESOLVED_CLIENT=venus" in result_a.stdout
    assert "RESOLVED_CLIENT=mercury" in result_b.stdout
    # Neither concurrent invocation repointed the shared root .env.
    assert root_env.read_text() == root_env_before


# ---------------------------------------------------------------------------
# Guard against silently regressing the mechanism itself: the root .env
# load must keep override=False, or an inline CLIENT_NAME override would
# stop working without any test above being able to tell the difference
# between "override works" and "root .env happened to already agree".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "module",
    [
        "process_photos",
        "check_approval",
        "upload_facebook",
        "run_e2e_test",
        "e2e_stage1_generate_frames",
        "e2e_stage2_upload_drive",
        "e2e_stage3_process",
        "e2e_stage4_await_approval",
        "e2e_stage5_await_facebook",
        "generate_auth_link",
    ],
)
def test_root_env_load_does_not_override(module):
    """The first load_dotenv() call (root .env) must be override=False so a
    pre-set CLIENT_NAME env var always wins — this is issue #45's whole
    mechanism, and it is easy to break by accidentally passing
    override=True here (matching the second, client-secrets call just below
    it) without anything else visibly changing."""
    src = (_PLATFORM_PHOTO_AGENT / "scripts" / f"{module}.py").read_text()
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith('load_dotenv(_ROOT / ".env")'):
            assert "override" not in stripped, (
                f'{module}.py: root .env load_dotenv() must not pass '
                f"override=True, or CLIENT_NAME env-var overrides silently "
                f"stop working: {stripped!r}"
            )
            return
    pytest.fail(f"{module}.py: expected a `load_dotenv(_ROOT / \".env\")` line, found none")
