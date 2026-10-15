"""Shared HTTP helpers for talking to the AMail server.

Why this exists: amail.app publishes both A and AAAA records. On a PC whose
network advertises IPv6 but cannot actually route it (very common on consumer
Windows setups), the OS hands out the AAAA address first and the connection
just hangs until our timeout — so every server call fails with "could not reach
the server" while browsers on the same PC work fine, because they implement
Happy Eyeballs (race IPv6 against IPv4). Python's urllib does not.

`urlopen_ipv4` is the retry we do after a failed call: same request, but pinned
to the IPv4 address.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.request

# Sent on every server call. Shared hosting WAFs treat the default
# "Python-urllib/3.x" as a bot signature; a real product name avoids that.
USER_AGENT = "AMail"


def ssl_context() -> ssl.SSLContext:
    """TLS context for our own server calls, trusting certifi's CA bundle.

    Windows PCs whose root store is stale (automatic root updates switched off,
    an old image, a wrong clock) reject the server's perfectly valid Let's
    Encrypt chain with "certificate has expired" — the expiry belongs to a root
    THEY still carry, not to the server. Shipping our own current CA bundle
    takes the PC's store out of the equation. Falls back to the system store if
    certifi is somehow unavailable.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def user_agent() -> str:
    try:
        from . import __version__
        return f"{USER_AGENT}/{__version__}"
    except Exception:  # noqa: BLE001
        return USER_AGENT


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that only ever resolves the host to an IPv4 address."""

    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET,
                                   socket.SOCK_STREAM)
        last = None
        for family, socktype, proto, _canon, addr in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                if self.timeout is not None:
                    sock.settimeout(self.timeout)
                sock.connect(addr)
            except OSError as e:
                sock.close()
                last = e
                continue
            self.sock = self._context.wrap_socket(sock,
                                                  server_hostname=self.host)
            return
        raise last or OSError(f"no IPv4 address for {self.host}")


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(self._connection, req)

    def _connection(self, host, **kwargs):
        kwargs.pop("context", None)
        kwargs.pop("check_hostname", None)
        return _IPv4HTTPSConnection(host, context=ssl_context(), **kwargs)


def urlopen_ipv4(req, timeout: float = 15):
    """Same as urllib.request.urlopen, but forced onto IPv4."""
    opener = urllib.request.build_opener(_IPv4HTTPSHandler())
    return opener.open(req, timeout=timeout)
