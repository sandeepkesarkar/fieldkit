"""
Tests for 2-step env loading guards in platform/photo-agent scripts.

Verifies that:
  T034 — scripts exit with an error when CLIENT_NAME is missing from fieldkit/.env
  T035 — state.py raises RuntimeError when FIELDKIT_DATA_DIR is unset
  T036 — logger.py raises RuntimeError when FIELDKIT_LOG_DIR is unset

Because these guards fire at module import time, tests run the scripts in
clean subprocesses with a controlled environment.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_PLATFORM_PHOTO_AGENT = Path(__file__).parents[1]


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


# ---------------------------------------------------------------------------
# T034 — missing CLIENT_NAME causes sys.exit
# ---------------------------------------------------------------------------

def test_missing_client_name_exits_nonzero(tmp_path):
    """When CLIENT_NAME is absent from the root .env, scripts exit non-zero."""
    root_env = tmp_path / ".env"
    root_env.write_text("# empty — no CLIENT_NAME\n")

    result = _run(
        "import sys; sys.path.insert(0, '.'); "
        "import os; os.environ['FIELDKIT_ROOT'] = '"
        + str(tmp_path)
        + "'; from scripts import process_photos",
        env={"FIELDKIT_ROOT": str(tmp_path)},
    )
    assert result.returncode != 0


def test_missing_client_name_shows_helpful_message(tmp_path):
    """Error message when CLIENT_NAME is missing names the missing variable."""
    root_env = tmp_path / ".env"
    root_env.write_text("# empty — no CLIENT_NAME\n")

    result = _run(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "import os\n"
        f"os.environ['FIELDKIT_ROOT'] = {str(tmp_path)!r}\n"
        "from scripts import process_photos\n",
        env={"FIELDKIT_ROOT": str(tmp_path)},
    )
    assert "CLIENT_NAME" in result.stderr or "CLIENT_NAME" in result.stdout


# ---------------------------------------------------------------------------
# T035 — missing FIELDKIT_DATA_DIR causes RuntimeError in state.py
# ---------------------------------------------------------------------------

def test_missing_fieldkit_data_dir_raises(tmp_path):
    """Importing tools.state without FIELDKIT_DATA_DIR set raises RuntimeError."""
    result = _run(
        "import tools.state",
        env={},  # no FIELDKIT_DATA_DIR
    )
    assert result.returncode != 0
    assert "FIELDKIT_DATA_DIR" in result.stderr


def test_missing_fieldkit_data_dir_in_facebook_state_raises(tmp_path):
    """Importing tools.facebook_state without FIELDKIT_DATA_DIR set raises RuntimeError."""
    result = _run(
        "import tools.facebook_state",
        env={},  # no FIELDKIT_DATA_DIR
    )
    assert result.returncode != 0
    assert "FIELDKIT_DATA_DIR" in result.stderr


# ---------------------------------------------------------------------------
# T036 — missing FIELDKIT_LOG_DIR causes RuntimeError in logger.py
# ---------------------------------------------------------------------------

def test_missing_fieldkit_log_dir_raises(tmp_path):
    """Importing tools.logger without FIELDKIT_LOG_DIR set raises RuntimeError."""
    result = _run(
        "import tools.logger",
        env={},  # no FIELDKIT_LOG_DIR
    )
    assert result.returncode != 0
    assert "FIELDKIT_LOG_DIR" in result.stderr


def test_missing_fieldkit_log_dir_in_facebook_logger_raises(tmp_path):
    """Importing tools.facebook_logger without FIELDKIT_LOG_DIR set raises RuntimeError."""
    result = _run(
        "import tools.facebook_logger",
        env={},  # no FIELDKIT_LOG_DIR
    )
    assert result.returncode != 0
    assert "FIELDKIT_LOG_DIR" in result.stderr
