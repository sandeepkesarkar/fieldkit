"""
Self-tests for the photo-agent suite's isolation (tests/_isolation.py, tests/conftest.py):
no real credentials, no real client .env files, no network — in this process and in child
processes.

Every test checks FIRST that the guard it relies on is installed, and fails there if not,
before it attempts anything. That is deliberate: against a checkout without the guard these
tests fail without reading any real credential file and without opening any real
connection. Every network target is TEST-NET-1 (192.0.2.1, RFC 5737, reserved for
documentation and not routed) or a `.invalid` name (RFC 2606), so nothing real could be
reached even if a guard were missing.
"""

import builtins
import http.client
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_PHOTO_AGENT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[3]
# From the password database, not $HOME — the suite deliberately moves $HOME.
_REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()

_UNROUTABLE = "192.0.2.1"
_ISOLATION_MARKER = "__fieldkit_test_isolation__"
_NETWORK_MARKER = "__fieldkit_test_network_guard__"


def _under_real_home(path) -> bool:
    return Path(os.fspath(path)).expanduser().resolve(strict=False).is_relative_to(_REAL_HOME)


def _require_network_guard():
    assert getattr(socket.socket.connect, _NETWORK_MARKER, False), (
        "the process-wide network guard is not installed"
    )


def _require_credential_isolation():
    assert not _under_real_home(os.environ.get("HOME", "")), "HOME is the real home"
    creds = os.environ.get("GOOGLE_USER_CREDENTIALS_FILE", "")
    assert creds and not _under_real_home(creds), (
        "GOOGLE_USER_CREDENTIALS_FILE is not pointed away from the real home"
    )


def _require_dotenv_guard():
    import dotenv
    assert getattr(dotenv.load_dotenv, _ISOLATION_MARKER, False), (
        "python-dotenv is not guarded"
    )


def _blocked(exc: BaseException) -> bool:
    """True if the network guard is anywhere in exc's cause/context/reason chain."""
    seen, stack = set(), [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if "blocked in tests" in str(e) or type(e).__name__ == "RealNetworkBlocked":
            return True
        stack += [e.__cause__, e.__context__, getattr(e, "reason", None)]
        stack += [a for a in getattr(e, "args", ()) if isinstance(a, BaseException)]
    return False


# Recorded while THIS MODULE IS BEING IMPORTED — i.e. during collection, before any test
# runs — to prove the guard already covers import-time connections. Attempted only if the
# guard is present, so a checkout without it records that fact instead of connecting.
if getattr(socket.socket.connect, _NETWORK_MARKER, False):
    try:
        socket.create_connection((_UNROUTABLE, 80), timeout=1)
        _IMPORT_TIME_ATTEMPT = "connected"
    except Exception as _exc:  # noqa: BLE001
        _IMPORT_TIME_ATTEMPT = "blocked" if _blocked(_exc) else f"other: {_exc!r}"
else:
    _IMPORT_TIME_ATTEMPT = "guard absent at import time"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def test_home_is_not_the_real_home():
    _require_credential_isolation()
    assert not _under_real_home(Path.home())


def test_every_google_credential_path_resolves_outside_the_real_home():
    _require_credential_isolation()
    import scripts.setup_drive_auth as sda
    import tools.drive as drive
    paths = {
        "drive default": drive._DEFAULT_CREDS_FILE,
        "GOOGLE_USER_CREDENTIALS_FILE": os.environ["GOOGLE_USER_CREDENTIALS_FILE"],
        "GOOGLE_APPLICATION_CREDENTIALS": os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        "setup_drive_auth client secret": sda._CLIENT_SECRET_FILE,
        "setup_drive_auth user credentials": sda._USER_CREDENTIALS_FILE,
        "gws config dir": Path(os.environ["XDG_CONFIG_HOME"]) / "gws",
    }
    for label, path in paths.items():
        assert not _under_real_home(path), label
        assert not Path(path).exists(), label


def test_an_unmocked_credential_lookup_reads_nothing_real(monkeypatch):
    """The incident: a missing mock reached drive._get_access_token(), which read the real
    credential file. Now it fails with "not found", and opens nothing under the real home."""
    _require_credential_isolation()
    import tools.drive as drive
    assert not _under_real_home(drive._DEFAULT_CREDS_FILE)

    opened = []
    real_open, real_read_text = builtins.open, Path.read_text

    def spy_open(file, *args, **kwargs):
        opened.append(file)
        return real_open(file, *args, **kwargs)

    def spy_read_text(self, *args, **kwargs):
        opened.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    with pytest.raises(RuntimeError, match="credentials file not found"):
        drive._get_access_token()
    assert not [p for p in opened if isinstance(p, (str, os.PathLike)) and _under_real_home(p)]


def _child(code: str, env=None, flags=()) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *flags, "-c", code],
        cwd=str(_PHOTO_AGENT), env=env, capture_output=True, text=True, timeout=60,
    )


def test_a_child_process_resolves_credentials_inside_the_sandbox():
    _require_credential_isolation()
    result = _child(
        "import json, os, sys; sys.path.insert(0, '.')\n"
        "import tools.drive as d\n"
        "try:\n"
        "    d._load_credentials(); err = 'LOADED'\n"
        "except RuntimeError as e:\n"
        "    err = str(e).splitlines()[0]\n"
        "print(json.dumps({'home': os.path.expanduser('~'),\n"
        "  'default': str(d._DEFAULT_CREDS_FILE),\n"
        "  'env': os.environ.get('GOOGLE_USER_CREDENTIALS_FILE'),\n"
        "  'hermes_home': os.environ.get('HERMES_HOME'), 'err': err}))\n"
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    for key in ("home", "default", "env", "hermes_home"):
        assert not _under_real_home(seen[key]), key
    assert "not found" in seen["err"]


def test_a_child_given_a_hand_built_env_still_gets_the_isolated_home():
    """Several existing tests pass only PATH and HOME copied from os.environ."""
    _require_credential_isolation()
    result = _child(
        "import os; print(os.path.expanduser('~/.config/gws/user_credentials.json'))",
        env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
    )
    assert result.returncode == 0, result.stderr
    assert not _under_real_home(result.stdout.strip())


# ---------------------------------------------------------------------------
# Client .env files
# ---------------------------------------------------------------------------

_REAL_ENV_PATHS = [
    _REPO_ROOT / ".env",
    _REPO_ROOT / "clients" / "_demo" / "src" / "photo-agent" / ".env",
    _REPO_ROOT / "clients" / "mercury" / "src" / "photo-agent" / ".env",
    _REPO_ROOT / "clients" / "_construction_co" / "src" / "photo-agent" / ".env",
    _REAL_HOME / ".hermes" / ".env",
]


def test_real_env_files_are_never_opened_by_dotenv(monkeypatch):
    """Every script loads <root>/.env and clients/<name>/src/photo-agent/.env at import.
    Paths outside the sandbox behave as missing, without the real loader running at all."""
    _require_dotenv_guard()
    import dotenv
    from tests import _isolation

    def must_not_run(*args, **kwargs):
        raise AssertionError("the real python-dotenv loader was reached")

    for name in ("load_dotenv", "dotenv_values", "set_key", "unset_key"):
        monkeypatch.setitem(_isolation._real, name, must_not_run)
    for path in _REAL_ENV_PATHS:
        assert dotenv.load_dotenv(path, override=True) is False, path
        assert dotenv.dotenv_values(path) == {}, path
        with pytest.raises(_isolation.RealCredentialAccessBlocked):
            dotenv.set_key(path, "X", "Y")
        with pytest.raises(_isolation.RealCredentialAccessBlocked):
            dotenv.unset_key(path, "X")
    assert dotenv.load_dotenv() is False          # the upward search is refused too
    assert dotenv.dotenv_values() == {}


@pytest.mark.parametrize("module", [
    "upload_facebook", "upload_instagram", "check_approval", "process_photos",
    "resolve_instagram_quarantine", "resolve_facebook_quarantine", "generate_auth_link",
])
def test_scripts_bound_the_guarded_loader(module):
    """Scripts do `from dotenv import load_dotenv` at import; they must have bound the
    guarded one, which proves the guard was in place before they were imported."""
    _require_dotenv_guard()
    import importlib
    mod = importlib.import_module(f"scripts.{module}")
    assert getattr(mod.load_dotenv, _ISOLATION_MARKER, False)


def test_a_tests_own_env_file_still_loads(tmp_path):
    """The guard must not break the tests that write their own .env under tmp_path."""
    _require_dotenv_guard()
    import dotenv
    env_file = tmp_path / ".env"
    env_file.write_text("FIELDKIT_ISOLATION_PROBE=ok\n")
    assert dotenv.dotenv_values(env_file) == {"FIELDKIT_ISOLATION_PROBE": "ok"}


# ---------------------------------------------------------------------------
# Network — in this process
# ---------------------------------------------------------------------------

def test_connections_attempted_while_test_modules_import_are_refused():
    assert _IMPORT_TIME_ATTEMPT == "blocked"


def test_requests_is_refused():
    _require_network_guard()
    import requests
    with pytest.raises(Exception) as exc:
        requests.get(f"http://{_UNROUTABLE}/", timeout=1)
    assert _blocked(exc.value)


def test_requests_transport_adapter_is_refused_below_the_session():
    """HTTPAdapter.send() bypasses Session.request; the socket layer still refuses."""
    _require_network_guard()
    import requests
    prepared = requests.Request("GET", f"http://{_UNROUTABLE}/").prepare()
    with pytest.raises(Exception) as exc:
        requests.adapters.HTTPAdapter(max_retries=0).send(prepared, timeout=1)
    assert _blocked(exc.value)


def test_urllib3_is_refused():
    _require_network_guard()
    import urllib3
    with pytest.raises(Exception) as exc:
        urllib3.PoolManager().request("GET", f"http://{_UNROUTABLE}/", retries=False, timeout=1)
    assert _blocked(exc.value)


@pytest.mark.parametrize("use_proxy_env", [True, False])
def test_urllib_is_refused(use_proxy_env):
    _require_network_guard()
    handlers = [] if use_proxy_env else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    with pytest.raises(urllib.error.URLError) as exc:
        opener.open(f"http://{_UNROUTABLE}/", timeout=1)
    assert _blocked(exc.value)


@pytest.mark.parametrize("cls", [http.client.HTTPConnection, http.client.HTTPSConnection])
def test_http_client_is_refused(cls):
    _require_network_guard()
    conn = cls(_UNROUTABLE, timeout=1)
    with pytest.raises(Exception) as exc:
        conn.request("GET", "/")
    assert _blocked(exc.value)


@pytest.mark.parametrize("address", [(_UNROUTABLE, 80), ("127.0.0.1", 9), ("::1", 9)])
def test_raw_sockets_are_refused(address):
    """Loopback included: nothing in this suite needs TCP, so nothing is allowed."""
    _require_network_guard()
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        with pytest.raises(OSError) as exc:
            s.connect(address)
        assert _blocked(exc.value)
        with pytest.raises(OSError):
            s.connect_ex(address)
    with pytest.raises(OSError) as exc:
        socket.create_connection((_UNROUTABLE, 80), timeout=1)
    assert _blocked(exc.value)


def test_datagrams_are_refused():
    _require_network_guard()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(OSError) as exc:
            s.sendto(b"x", (_UNROUTABLE, 53))
    assert _blocked(exc.value)


@pytest.mark.parametrize("lookup", ["getaddrinfo", "gethostbyname", "gethostbyname_ex"])
def test_name_lookups_are_refused(lookup):
    _require_network_guard()
    args = ("fieldkit-test.invalid", 443) if lookup == "getaddrinfo" else ("fieldkit-test.invalid",)
    with pytest.raises(OSError) as exc:
        getattr(socket, lookup)(*args)
    assert _blocked(exc.value)


def test_unix_sockets_still_work():
    """The guard is about the network, not local IPC."""
    _require_network_guard()
    a, b = socket.socketpair()
    with a, b:
        a.sendall(b"ok")
        assert b.recv(2) == b"ok"


# ---------------------------------------------------------------------------
# Network — in child processes
# ---------------------------------------------------------------------------

def _require_child_network_isolation():
    from urllib.parse import urlsplit
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        proxy = urlsplit(os.environ.get(var, ""))
        assert proxy.hostname in ("127.0.0.1", "localhost"), var
    assert os.environ.get("NO_PROXY", "x") == "" and os.environ.get("no_proxy", "x") == ""
    assert os.environ.get("FIELDKIT_TEST_NETWORK_BLOCKED") == "1"
    assert any(p.endswith("_sandbox") for p in os.environ.get("PYTHONPATH", "").split(os.pathsep))


_CHILD_PROBE = (
    "import socket, urllib.request\n"
    "for name, attempt in [\n"
    f"    ('urllib', lambda: urllib.request.urlopen('http://{_UNROUTABLE}/', timeout=2)),\n"
    f"    ('socket', lambda: socket.create_connection(('{_UNROUTABLE}', 80), timeout=2)),\n"
    "]:\n"
    "    try:\n"
    "        attempt(); print(name, 'CONNECTED')\n"
    "    except Exception as e:\n"
    "        print(name, 'REFUSED', type(e).__name__, e)\n"
)


def test_a_child_python_process_is_refused_even_when_it_ignores_proxies():
    """The inherited PYTHONPATH installs the same socket guard in the child at startup."""
    _require_child_network_isolation()
    result = _child(_CHILD_PROBE)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert any(l.startswith("urllib REFUSED") for l in lines), result.stdout
    assert any(l.startswith("socket REFUSED") and "blocked in tests" in l for l in lines), (
        result.stdout
    )


def test_a_proxy_aware_child_without_the_guard_is_refused_by_the_closed_proxy():
    """With the guard removed from the child — no PYTHONPATH sitecustomize, and -s so the
    isolated user site's guard .pth is not processed — the proxy variables alone stop it."""
    _require_child_network_isolation()
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    result = _child(
        "import socket, urllib.request\n"
        f"assert not getattr(socket.socket.connect, {_NETWORK_MARKER!r}, False), 'guard present'\n"
        "try:\n"
        f"    urllib.request.urlopen('http://{_UNROUTABLE}/', timeout=5); print('CONNECTED')\n"
        "except Exception as e:\n"
        "    print('REFUSED', type(e).__name__, e)\n",
        env=env, flags=("-s",),
    )
    assert result.stdout.startswith("REFUSED"), result.stdout + result.stderr
    assert "timed out" not in result.stdout  # refused at the proxy, not a direct attempt


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
@pytest.mark.parametrize("url", [f"http://{_UNROUTABLE}/", "https://fieldkit-test.invalid/"])
def test_a_proxy_aware_non_python_child_is_refused(url):
    """curl (and, the same way, gws and other clients that honour proxy variables)."""
    _require_child_network_isolation()
    result = subprocess.run(
        ["curl", "-sS", "--max-time", "5", "-o", "/dev/null", url],
        capture_output=True, text=True, timeout=30,
    )
    # 5 = couldn't resolve proxy, 7 = couldn't connect (to the proxy), 97 = proxy handshake.
    # 28 (a timeout) would mean it went direct instead, so it is not accepted.
    assert result.returncode in (5, 7, 97), (result.returncode, result.stderr)


# ---------------------------------------------------------------------------
# Child Python start-up: .pth files run before sitecustomize (round 5)
# ---------------------------------------------------------------------------
#
# Python executes the import lines of .pth files in its site directories during start-up,
# before `sitecustomize` is imported. So a child Python must never see the developer's real
# per-user site (whose .pth files would run before any sitecustomize guard), and the .pth
# files it does see must run after the guard.

def _real_user_base() -> Path:
    """Where Python puts the per-user base for the REAL home. -I -S: runs no site/.pth."""
    probe = subprocess.run(
        [sys.executable, "-I", "-S", "-c", "import site; print(site.getuserbase())"],
        env={"HOME": str(_REAL_HOME), "PATH": os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )
    return Path(probe.stdout.strip()).resolve()


_SITE_PROBE = (
    "import json, site, sys\n"
    "print(json.dumps({'user_site': site.getusersitepackages(),\n"
    "                  'enabled': bool(site.ENABLE_USER_SITE), 'path': sys.path}))\n"
)

_CHILD_ENVS = {
    "inherited env": lambda: None,
    "hand-built PATH/HOME env": lambda: {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
}


def _child_user_site(env) -> dict:
    result = _child(_SITE_PROBE, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("env_kind", list(_CHILD_ENVS))
def test_a_child_python_never_sees_the_real_user_site(env_kind):
    real_base = _real_user_base()
    seen = _child_user_site(_CHILD_ENVS[env_kind]())
    assert not Path(seen["user_site"]).resolve().is_relative_to(real_base)
    leaked = [p for p in seen["path"] if p and Path(p).resolve().is_relative_to(real_base)]
    assert not leaked, leaked
    assert seen["enabled"]  # the isolated user site is processed — that is where the guard is


def test_the_isolated_user_site_holds_only_the_guard_and_approved_packages():
    from tests import _isolation
    site_dir = getattr(_isolation, "ISOLATED_USER_SITE", None)
    assert site_dir is not None, "no isolated per-user site is built"
    names = {p.name for p in site_dir.iterdir()}
    assert {n for n in names if n.endswith(".pth")} == {_isolation.GUARD_PTH_NAME}
    # __pycache__ holds the guard module's own bytecode, written when children import it.
    allowed = {_isolation.GUARD_PTH_NAME, f"{_isolation.GUARD_MODULE_NAME}.py", "__pycache__"}
    others = names - allowed
    assert all(
        n.split(".")[0] in _isolation.APPROVED_USER_SITE_PACKAGES for n in others
    ), others


_PLANTED_LINE = (
    "import os, socket; p = os.environ.get('FIELDKIT_PTH_PROBE'); "
    "p and open(p + '.{tag}', 'w').write(str(bool(getattr("
    f"socket.socket.connect, {_NETWORK_MARKER!r}, False))))\n"
)


@pytest.mark.parametrize("env_kind", list(_CHILD_ENVS))
def test_a_pth_line_in_a_childs_site_runs_only_after_the_network_guard(env_kind, tmp_path):
    """Plant .pth files with an executable line — one named to sort early, one late — in
    the per-user site a child Python processes, and record whether the guard was already
    installed when each ran. Refuses to plant anything unless that site is the suite's own
    (never the developer's real one)."""
    base_env = _CHILD_ENVS[env_kind]()
    user_site = Path(_child_user_site(base_env)["user_site"])
    assert not user_site.resolve().is_relative_to(_real_user_base()), (
        "the child's per-user site is the developer's real one — not planting there"
    )
    planted = [user_site / "0-planted-probe.pth", user_site / "zz-planted-probe.pth"]
    probe = tmp_path / "probe"
    env = dict(base_env if base_env is not None else os.environ, FIELDKIT_PTH_PROBE=str(probe))
    try:
        for path in planted:
            path.write_text(_PLANTED_LINE.format(tag=path.stem))
        result = _child("pass", env=env)
        assert result.returncode == 0, result.stderr
    finally:
        for path in planted:
            path.unlink(missing_ok=True)
    for path in planted:
        record = Path(f"{probe}.{path.stem}")
        assert record.exists(), f"{path.name} did not run"   # it ran...
        assert record.read_text() == "True", path.name        # ...with the guard in place


def test_a_hand_built_child_env_gets_the_network_guard_too():
    """Children given only PATH/HOME carry no PYTHONPATH or proxy variables; the isolated
    user site's .pth still installs the guard in them."""
    result = _child(
        "import socket\n"
        f"if not getattr(socket.socket.connect, {_NETWORK_MARKER!r}, False):\n"
        "    print('NO GUARD')  # and do not attempt anything\n"
        "else:\n"
        "    try:\n"
        f"        socket.create_connection(('{_UNROUTABLE}', 80), timeout=2); print('CONNECTED')\n"
        "    except Exception as e:\n"
        "        print('REFUSED', e)\n",
        env=_CHILD_ENVS["hand-built PATH/HOME env"](),
    )
    assert result.stdout.startswith("REFUSED") and "blocked in tests" in result.stdout, (
        result.stdout + result.stderr
    )


def test_an_approved_user_site_package_still_imports_in_a_child():
    """PyYAML, which install_client.sh needs, is reachable — as a package, not via .pth."""
    import site
    if not (Path(site.getusersitepackages()) / "yaml").exists():
        pytest.skip("PyYAML is not in this developer's per-user site")
    result = _child(
        "import yaml; print(yaml.safe_load('a: 1'))",
        env=_CHILD_ENVS["hand-built PATH/HOME env"](),
    )
    assert result.stdout.strip() == "{'a': 1}", result.stdout + result.stderr
