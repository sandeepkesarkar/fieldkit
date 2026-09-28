"""
Process-wide isolation for the photo-agent test suite: no real credentials, no real client
.env files, no network. Installed by tests/conftest.py at import — see there for why that
is before any test module or production module is imported.

Why this exists: during PR #95 a newly added Drive function was not yet mocked in an older
fixture, and the real one ran. drive._get_access_token() READ the developer's real
~/.config/gws/user_credentials.json, refreshed a real OAuth token, and called the real Drive
API. Per-test mocks are still the primary mechanism; this makes a missing one fail closed.

WHAT IT COVERS

Credentials — in this process, and in every child process that inherits os.environ:
  - HOME is an empty private temp directory, so every "~/..." path resolves inside it:
    tools/drive.py's default ~/.config/gws/user_credentials.json, and
    scripts/setup_drive_auth.py's ~/.config/gws/client_secret.json and
    user_credentials.json (all three are resolved at IMPORT, which is why this must run
    before those modules load). XDG_CONFIG_HOME and HERMES_HOME point inside it too.
  - GOOGLE_USER_CREDENTIALS_FILE — the only credential-path variable photo-agent code
    reads — and GOOGLE_APPLICATION_CREDENTIALS / CLOUDSDK_CONFIG point at nonexistent paths
    inside it, so a credential lookup fails with "not found" instead of reading anything.
  - The real home is found with pwd (independent of $HOME) and exposed as REAL_HOME for the
    self-tests. HERMES_AGENT_DIR, if unset, is pinned to REAL_HOME/.hermes/hermes-agent
    first: it is a code checkout the Hermes dispatch tests run, and moving HOME would
    otherwise make them silently skip. Those tests run with HERMES_HOME isolated.

Client .env files — in this process:
  - python-dotenv's load_dotenv / dotenv_values refuse any path outside the temp tree
    (tempfile.gettempdir()) and outside the isolated HOME: load_dotenv returns False and
    dotenv_values returns {} WITHOUT opening the file, which is exactly how a missing file
    behaves. A call with no path (find_dotenv's upward search) is refused the same way.
    set_key / unset_key outside those roots raise. Every photo-agent script loads
    <FIELDKIT_ROOT or repo root>/.env and clients/<CLIENT_NAME>/src/photo-agent/.env at
    import; this is what stops those loads reading a real client's tokens when the suite
    runs in a checkout that has them. Tests' own .env files under tmp_path still load.

Network — in this process (tests/_sandbox/sitecustomize.py):
  - every AF_INET/AF_INET6 connect, connect_ex and sendto is refused, loopback included;
    every name lookup except loopback names is refused. requests, urllib3 (and so
    requests' adapters), urllib, http.client, ssl and raw sockets all go through these.
  - requests.Session.request is also refused, only for a clearer message.
  It is installed before any test module is imported, so it covers connections attempted
  while test modules (and the production modules they import) are being imported.

Network — in child processes that inherit os.environ:
  - tests/_sandbox is prepended to PYTHONPATH, so a child Python process installs the same
    socket guard at startup (via sitecustomize) — including one that ignores proxy settings.
  - HTTP_PROXY / HTTPS_PROXY / ALL_PROXY (both cases) point at a closed loopback port and
    NO_PROXY is empty, so proxy-aware non-Python tools fail to connect. curl is tested;
    no test runs gws, so whether it honours these variables is not verified here.

WHAT IT DOES NOT COVER
  - A child process started with a hand-built env that leaves out PYTHONPATH and the proxy
    variables (several existing tests pass only PATH/HOME). It still gets the isolated HOME
    (they copy HOME from os.environ) but not the network guard. Those tests were audited:
    they run local scripts and stubs and point FIELDKIT_ROOT at tmp_path.
  - A non-Python child that ignores proxy variables and opens its own sockets.
  - Native extension code that opens sockets without Python's socket module.
  - python-dotenv inside child processes (only the in-process loader is guarded). The
    subprocess tests that import scripts pass FIELDKIT_ROOT=tmp_path, so they load tmp .env
    files; with the isolated HOME none of them can reach ~/.hermes/.env.
  - google-auth: not installed and not used by photo-agent; if it were, its requests/urllib3
    transport would hit the socket guard, but that route is not separately tested.
  - Filesystem writes into a real checkout's clients/ data, and the email-agent suite (this
    conftest is photo-agent only) — the general "tests can never touch real data" guard is
    issue #77.
"""

import functools
import importlib.util
import os
import pwd
import tempfile
from pathlib import Path

_SANDBOX_DIR = Path(__file__).resolve().parent / "_sandbox"

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
ISOLATED_HOME = Path(tempfile.mkdtemp(prefix="fieldkit-test-home-")).resolve()
CLOSED_PROXY = "http://127.0.0.1:9"  # discard port on loopback; nothing listens there
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_ALLOWED_DOTENV_ROOTS = (Path(tempfile.gettempdir()).resolve(), ISOLATED_HOME)

MARKER = "__fieldkit_test_isolation__"
_installed = False
_real = {}


class RealCredentialAccessBlocked(PermissionError):
    """Raised for an attempt to WRITE a .env outside the test sandbox."""


def _isolate_credentials() -> None:
    # The Hermes dispatch tests run the real Hermes CODE checkout at
    # ~/.hermes/hermes-agent (some via HERMES_AGENT_DIR, some via Path.home()). Moving HOME
    # would make them silently skip, so that one code directory is linked into the isolated
    # HOME. Nothing else from the real ~/.hermes is — in particular not ~/.hermes/.env —
    # and HERMES_HOME points at the isolated ~/.hermes.
    real_agent = REAL_HOME / ".hermes" / "hermes-agent"
    os.environ.setdefault("HERMES_AGENT_DIR", str(real_agent))
    if real_agent.is_dir():
        link = ISOLATED_HOME / ".hermes" / "hermes-agent"
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(real_agent, target_is_directory=True)
    # Python's per-user site-packages (where e.g. PyYAML may be installed) is located from
    # $HOME. Child processes — including those given a hand-built env with only PATH/HOME —
    # must still find installed packages, so the real per-user base directory (a package
    # directory, not a credential store) is symlinked to the same relative place inside the
    # isolated HOME, and nothing else from the real home is.
    import site
    user_base = Path(site.getuserbase())
    if user_base.is_relative_to(REAL_HOME) and user_base.is_dir():
        link = ISOLATED_HOME / user_base.relative_to(REAL_HOME)
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(user_base, target_is_directory=True)
    os.environ["HOME"] = str(ISOLATED_HOME)
    os.environ["XDG_CONFIG_HOME"] = str(ISOLATED_HOME / ".config")
    os.environ["HERMES_HOME"] = str(ISOLATED_HOME / ".hermes")
    missing = ISOLATED_HOME / "no-credentials-in-tests"
    os.environ["GOOGLE_USER_CREDENTIALS_FILE"] = str(missing / "user_credentials.json")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(missing / "application_default.json")
    os.environ["CLOUDSDK_CONFIG"] = str(missing / "gcloud")


def _isolate_child_network() -> None:
    for var in _PROXY_VARS:
        os.environ[var] = CLOSED_PROXY
    os.environ["NO_PROXY"] = ""
    os.environ["no_proxy"] = ""
    os.environ["FIELDKIT_TEST_NETWORK_BLOCKED"] = "1"
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = str(_SANDBOX_DIR) + (os.pathsep + existing if existing else "")


def load_network_guard():
    """Load tests/_sandbox/sitecustomize.py under its own name and return the module."""
    spec = importlib.util.spec_from_file_location(
        "fieldkit_test_network_guard", _SANDBOX_DIR / "sitecustomize.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dotenv_path_allowed(path) -> bool:
    """True if a dotenv file at `path` is inside the test sandbox. Never opens the file."""
    if path is None:
        return False
    resolved = Path(os.fspath(path)).expanduser().resolve(strict=False)
    return any(resolved.is_relative_to(root) for root in _ALLOWED_DOTENV_ROOTS)


def _guard_dotenv() -> None:
    import dotenv
    import dotenv.main

    _real["load_dotenv"] = dotenv.main.load_dotenv
    _real["dotenv_values"] = dotenv.main.dotenv_values
    _real["set_key"] = dotenv.main.set_key
    _real["unset_key"] = dotenv.main.unset_key

    # functools.wraps keeps each library signature visible to inspect.signature(), which
    # test_client_name_override.py reads to check python-dotenv's defaults.
    @functools.wraps(_real["load_dotenv"])
    def load_dotenv(dotenv_path=None, stream=None, *args, **kwargs):
        if stream is None and not dotenv_path_allowed(dotenv_path):
            return False
        return _real["load_dotenv"](dotenv_path, stream, *args, **kwargs)

    @functools.wraps(_real["dotenv_values"])
    def dotenv_values(dotenv_path=None, stream=None, *args, **kwargs):
        if stream is None and not dotenv_path_allowed(dotenv_path):
            return {}
        return _real["dotenv_values"](dotenv_path, stream, *args, **kwargs)

    def _writer(name):
        @functools.wraps(_real[name])
        def guarded(dotenv_path, *args, **kwargs):
            if not dotenv_path_allowed(dotenv_path):
                raise RealCredentialAccessBlocked(
                    f"{name} outside the test sandbox refused: {dotenv_path}"
                )
            return _real[name](dotenv_path, *args, **kwargs)
        return guarded

    guarded = {
        "load_dotenv": load_dotenv,
        "dotenv_values": dotenv_values,
        "set_key": _writer("set_key"),
        "unset_key": _writer("unset_key"),
    }
    for name, fn in guarded.items():
        setattr(fn, MARKER, True)
        setattr(dotenv, name, fn)
        setattr(dotenv.main, name, fn)


def _guard_requests() -> None:
    import requests

    def request(self, method, url, *args, **kwargs):
        raise NETWORK_GUARD.RealNetworkBlocked(
            f"real HTTP request attempted in a test: {method} {str(url).split('?')[0]} — "
            "mock the edge this code path reaches"
        )

    setattr(request, MARKER, True)
    requests.Session.request = request


NETWORK_GUARD = None


def install() -> None:
    """Install everything above in this process. Idempotent."""
    global _installed, NETWORK_GUARD
    if _installed:
        return
    _isolate_credentials()
    _isolate_child_network()
    NETWORK_GUARD = load_network_guard()
    if not NETWORK_GUARD.install():
        raise RuntimeError("test network guard failed to install")
    _guard_dotenv()
    _guard_requests()
    _installed = True
