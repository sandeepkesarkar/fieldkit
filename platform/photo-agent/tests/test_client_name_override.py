"""
Tests for issue #45/PR #57's CLIENT_NAME resolution mechanism, and its
status as of issue #61's architecture decision.

Original problem (#45): process_photos.py, check_approval.py,
upload_facebook.py, and run_e2e_test.py all resolve CLIENT_NAME via a
single shared root .env, with no per-invocation override — so a manual
test run against a client other than the one currently installed had no
way to avoid touching that shared file.

**Status after issue #61 (read before assuming this mechanism does more
than it does):** this project's architecture now runs exactly ONE client
at a time, installed via platform/photo-agent/scripts/install_client.sh —
see platform/docs/hermes/09-per-client-model-profiles.md. The inline
CLIENT_NAME= override this file tests is KEPT, deliberately, as a harmless
ad-hoc/single-invocation escape hatch for manual testing against a
non-installed client (e.g. `CLIENT_NAME=venus python3
run_e2e_test.py ...` without disturbing whatever's actually installed) —
it is explicitly NOT a supported mechanism for running two clients'
cron/gateway flows concurrently, which is the framing this file's tests
originally described and issue #61 retired. That concurrent-profile design
(each client getting its own Hermes profile so multiple clients' flows
could coexist) was the actual root cause of issue #59, not a gap in the
resolution order tested here — see test_install_client.py for the
regression coverage proving the single-install model closes that gap by
construction.

Mechanism (application-owned, not an implicit library default): each script
loads env vars in two steps —
  1. load_dotenv(_ROOT / ".env", override=False)   — the shared root file
  2. load_dotenv(.../clients/<client>/.../.env", override=True) — that
     client's own secrets

`override=False` on step 1 means it never clobbers a CLIENT_NAME already
present in the process environment, so `env CLIENT_NAME=<client>
python3 ...` prefixed on a crontab entry (or any invocation) takes
precedence over the shared root .env's CLIENT_NAME. This repo pins that
`override=False` explicitly rather than relying on python-dotenv's current
default (unpinned in requirements.txt) — a future dependency upgrade that
changed the library's default must not silently break this.

Step 2 loads with `override=True` so the client's own secrets win over the
root .env — but if a client .env ever defined its own CLIENT_NAME (nothing
shipped does; see test_no_shipped_client_env_example_sets_client_name
below), that same override=True would silently clobber the value step 1
resolved. Each script re-asserts os.environ["CLIENT_NAME"] = _CLIENT
immediately after step 2 to close that off unconditionally.

These guards fire at module import time, so tests run each script in a
clean subprocess with a controlled environment (same approach as
test_env_loading.py).
"""

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

_PLATFORM_PHOTO_AGENT = Path(__file__).parents[1]
_REPO_ROOT = _PLATFORM_PHOTO_AGENT.parents[1]

# The four scripts issue #45 names as resolving CLIENT_NAME via the shared
# root .env with no per-invocation override.
_AFFECTED_MODULES = [
    "process_photos",
    "check_approval",
    "upload_facebook",
    "run_e2e_test",
]

# All scripts that share the exact two-step env-loading pattern (the four
# above, plus the e2e stage scripts run_e2e_test.py can also invoke
# independently, plus generate_auth_link.py).
_ALL_TWO_STEP_MODULES = _AFFECTED_MODULES + [
    "e2e_stage1_generate_frames",
    "e2e_stage2_upload_drive",
    "e2e_stage3_process",
    "e2e_stage4_await_approval",
    "e2e_stage5_await_facebook",
    "generate_auth_link",
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


def _import_and_print_snippet(module: str) -> str:
    """Prints both the module's resolved _CLIENT and the environment's
    CLIENT_NAME *after* both load_dotenv() calls have completed, so tests
    can catch the second (client-secrets) load silently clobbering it."""
    return (
        "import sys, os; sys.path.insert(0, '.'); "
        f"from scripts import {module} as m; "
        "print('RESOLVED_CLIENT=' + m._CLIENT); "
        "print('ENV_CLIENT_NAME=' + os.environ.get('CLIENT_NAME', '<unset>'))"
    )


def _base_env(tmp_path: Path) -> dict:
    return {
        "FIELDKIT_ROOT": str(tmp_path),
        "FIELDKIT_DATA_DIR": str(tmp_path / "data"),
        "FIELDKIT_LOG_DIR": str(tmp_path / "logs"),
    }


def _write_client_env(tmp_path: Path, client: str, contents: str) -> Path:
    client_dir = tmp_path / "clients" / client / "src" / "photo-agent"
    client_dir.mkdir(parents=True, exist_ok=True)
    client_env = client_dir / ".env"
    client_env.write_text(contents)
    return client_env


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

    result = _run(_import_and_print_snippet(module), env=env)

    assert result.returncode == 0, result.stderr
    assert "RESOLVED_CLIENT=venus" in result.stdout
    assert "ENV_CLIENT_NAME=venus" in result.stdout
    # The shared root .env itself must never be touched by an inline override.
    assert root_env.read_text() == "CLIENT_NAME=_demo\n"


@pytest.mark.parametrize("module", _AFFECTED_MODULES)
def test_no_override_falls_back_to_root_env(tmp_path, module):
    """With no inline override, today's single-client posture is unaffected:
    CLIENT_NAME comes from the shared root .env exactly as before."""
    root_env = tmp_path / ".env"
    root_env.write_text("CLIENT_NAME=_demo\n")

    env = _base_env(tmp_path)  # no CLIENT_NAME pre-set

    result = _run(_import_and_print_snippet(module), env=env)

    assert result.returncode == 0, result.stderr
    assert "RESOLVED_CLIENT=_demo" in result.stdout
    assert "ENV_CLIENT_NAME=_demo" in result.stdout


# ---------------------------------------------------------------------------
# The critical gap a prior review found: nothing checked CLIENT_NAME
# *after* the second load_dotenv() call (the client .env, override=True).
# If a client .env ever defined its own CLIENT_NAME, that override=True load
# would silently clobber the value the first load resolved — undetectable by
# tests that only inspect the value before the second load runs.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", _ALL_TWO_STEP_MODULES)
def test_client_name_survives_second_load_even_if_client_env_defines_it(tmp_path, module):
    """A client .env that (against the documented convention) defines its own
    CLIENT_NAME must not be able to override the value already resolved —
    the script re-asserts CLIENT_NAME immediately after loading it."""
    root_env = tmp_path / ".env"
    root_env.write_text("CLIENT_NAME=_demo\n")

    _write_client_env(tmp_path, "venus", "CLIENT_NAME=some_other_client\nFB_PAGE_ID=123\n")

    env = _base_env(tmp_path)
    env["CLIENT_NAME"] = "venus"  # inline override selects the client whose .env we poisoned

    result = _run(_import_and_print_snippet(module), env=env)

    assert result.returncode == 0, result.stderr
    assert "RESOLVED_CLIENT=venus" in result.stdout
    assert "ENV_CLIENT_NAME=venus" in result.stdout
    assert "some_other_client" not in result.stdout


def test_no_shipped_client_env_example_sets_client_name():
    """No .env.example template in the repo should document CLIENT_NAME as an
    active, settable key inside a client-scoped .env — it has no effect
    there (see test above), and platform/photo-agent/.env.example documents
    that explicitly. This guards against that documentation drifting back
    out of sync with the code's actual behavior."""
    candidates = list(_REPO_ROOT.glob("clients/*/src/photo-agent/.env.example"))
    candidates.append(_PLATFORM_PHOTO_AGENT / ".env.example")
    assert len(candidates) >= 2, "expected to find client .env.example templates"

    offenders = []
    for path in candidates:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("CLIENT_NAME=") and not stripped.startswith("#"):
                offenders.append(str(path))
    assert offenders == [], (
        f"these client .env.example templates set an active CLIENT_NAME=, "
        f"which has no effect and contradicts the documented convention: {offenders}"
    )


# ---------------------------------------------------------------------------
# NOTE (issue #61): a "two clients' processes running concurrently, each
# resolving their own CLIENT_NAME via inline override" test used to live
# here. It's removed, not just left passing — that scenario (two clients'
# flows genuinely coexisting on one machine) is exactly the configuration
# this project's architecture now prohibits (see the module docstring and
# platform/docs/hermes/09-per-client-model-profiles.md). Keeping a passing
# test for an explicitly unsupported configuration would read as tacit
# endorsement to a future contributor skimming this file. The inline
# override mechanism itself is still fully covered above (single-invocation
# use, not concurrent use) and in test_install_client.py's
# test_switching_clients_replaces_not_merges_prior_config, which is the
# actual #59 regression guard under the current architecture.
# ---------------------------------------------------------------------------
# Guard against silently regressing the mechanism itself: the root .env
# load must keep an EXPLICIT override=False, or an inline CLIENT_NAME
# override would stop working without any test above being able to tell the
# difference between "override works" and "root .env happened to already
# agree".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", _ALL_TWO_STEP_MODULES)
def test_root_env_load_is_explicitly_override_false(module):
    """The first load_dotenv() call (root .env) must pass override=False
    EXPLICITLY — this is issue #45's whole mechanism, made an
    application-owned contract rather than a reliance on python-dotenv's
    current (unpinned) default. Easy to break by accidentally passing
    override=True here (matching the second, client-secrets call just below
    it), or by reverting to the implicit default, without anything else
    visibly changing."""
    src = (_PLATFORM_PHOTO_AGENT / "scripts" / f"{module}.py").read_text()
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith('load_dotenv(_ROOT / ".env"'):
            assert stripped == 'load_dotenv(_ROOT / ".env", override=False)', (
                f"{module}.py: root .env load_dotenv() must be exactly "
                f"`load_dotenv(_ROOT / \".env\", override=False)`, found: {stripped!r}"
            )
            return
    pytest.fail(f"{module}.py: expected a `load_dotenv(_ROOT / \".env\")` line, found none")


@pytest.mark.parametrize("module", _ALL_TWO_STEP_MODULES)
def test_client_name_reasserted_after_second_load(module):
    """Each script must re-assert os.environ["CLIENT_NAME"] = _CLIENT
    immediately after the second (client .env, override=True) load_dotenv()
    call — belt-and-suspenders against that load ever clobbering CLIENT_NAME,
    independent of what any client .env currently contains."""
    src = (_PLATFORM_PHOTO_AGENT / "scripts" / f"{module}.py").read_text()
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if 'clients" / _CLIENT / "src" / "photo-agent" / ".env", override=True)' in line:
            following = "\n".join(lines[i + 1 : i + 9])
            assert 'os.environ["CLIENT_NAME"] = _CLIENT' in following, (
                f"{module}.py: expected os.environ[\"CLIENT_NAME\"] = _CLIENT "
                f"shortly after the client .env load, found none in:\n{following}"
            )
            return
    pytest.fail(f"{module}.py: expected the client .env load_dotenv() line, found none")


def test_load_dotenv_library_default_is_still_false():
    """Defense-in-depth, not a correctness dependency (the scripts now pin
    override=False explicitly): documents what python-dotenv's own default
    is today, so a future contributor changing the pin can see at a glance
    whether they'd be diverging from the library default or converging with
    it. requirements.txt does not pin python-dotenv's version."""
    default = inspect.signature(load_dotenv).parameters["override"].default
    assert default is False, (
        f"python-dotenv's load_dotenv() override default changed to {default!r} — "
        f"harmless since this repo's scripts pin override=False explicitly, but "
        f"worth knowing when reviewing them."
    )
