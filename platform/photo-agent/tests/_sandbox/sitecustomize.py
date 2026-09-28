"""
Network guard for the photo-agent test suite — stdlib only, installed in three ways:

  - in the pytest process, by tests/_isolation.py (loaded from tests/conftest.py at import);
  - in child Python processes whose per-user site is the suite's isolated one, by a .pth
    file there that sorts first (tests/_isolation.py builds it). User-site .pth files are
    processed before system site-packages ones, so this runs before any other .pth line;
  - as a second layer, in child processes that inherit the suite's environment, because
    tests/_isolation.py prepends this directory to PYTHONPATH and Python imports a
    `sitecustomize` module from sys.path at startup — AFTER all .pth files have run.

It refuses every AF_INET / AF_INET6 connection and datagram, loopback included, and every
name lookup except for loopback names. AF_UNIX sockets are untouched. It patches the
Python-level socket.socket class and module functions, which is what requests, urllib3,
urllib, http.client, ssl and asyncio all go through.

Active only while FIELDKIT_TEST_NETWORK_BLOCKED=1, or when forced by the isolated user
site's .pth (which exists only inside the suite's temporary HOME), so this directory being
on a path by accident outside the suite does nothing.
"""

import ipaddress
import os
import socket

MARKER = "__fieldkit_test_network_guard__"


class RealNetworkBlocked(ConnectionRefusedError):
    """Raised for any attempt to use the network from inside the photo-agent test suite."""


def _is_loopback_name(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").split("%")[0]
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _refuse_inet(sock, address, what):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        host = address[0] if isinstance(address, tuple) and address else address
        raise RealNetworkBlocked(f"network {what} blocked in tests: {host}")


def install(force: bool = False) -> bool:
    """Install the guard in this process. Idempotent. Returns True if it is active."""
    if not force and os.environ.get("FIELDKIT_TEST_NETWORK_BLOCKED") != "1":
        return False
    if getattr(socket.socket.connect, MARKER, False):
        return True

    real = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "sendto": socket.socket.sendto,
        "getaddrinfo": socket.getaddrinfo,
        "gethostbyname": socket.gethostbyname,
        "gethostbyname_ex": socket.gethostbyname_ex,
        "gethostbyaddr": socket.gethostbyaddr,
    }

    def connect(self, address):
        _refuse_inet(self, address, "connection")
        return real["connect"](self, address)

    def connect_ex(self, address):
        _refuse_inet(self, address, "connection")
        return real["connect_ex"](self, address)

    def sendto(self, data, *args):
        _refuse_inet(self, args[-1] if args else None, "datagram")
        return real["sendto"](self, data, *args)

    def _lookup(name):
        def guarded(host, *args, **kwargs):
            if not _is_loopback_name(host):
                raise RealNetworkBlocked(f"network name lookup blocked in tests: {host}")
            return real[name](host, *args, **kwargs)
        setattr(guarded, MARKER, True)
        return guarded

    for fn in (connect, connect_ex, sendto):
        setattr(fn, MARKER, True)
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        setattr(socket, name, _lookup(name))
    return True


install()
