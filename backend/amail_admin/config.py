"""Where the backend keeps its files, and which ports it listens on.

Everything is overridable with environment variables so the Windows service,
tests and a developer's machine can each point it somewhere else.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

# Who may open the admin panel when nothing narrower is configured: private
# and loopback addresses. Narrow it to the admins' own PCs or subnet with
# AMAIL_ADMIN_NETWORKS (e.g. "192.168.10.0/28,192.168.1.50").
DEFAULT_ADMIN_NETWORKS = ("127.0.0.0/8", "::1/128", "10.0.0.0/8",
                          "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    device_host: str = "0.0.0.0"
    device_port: int = 8443
    admin_host: str = "0.0.0.0"
    admin_port: int = 8444
    # The admin panel answers only these networks.
    admin_networks: tuple = field(default_factory=lambda: tuple(
        ipaddress.ip_network(n) for n in DEFAULT_ADMIN_NETWORKS))
    # Addresses of a tunnel / reverse proxy in front of the device API. Only
    # requests from these may say who the real client is (X-Forwarded-For /
    # CF-Connecting-IP); everyone else's headers are ignored.
    trusted_proxies: tuple = ()
    # The one header a trusted proxy puts the real client address in.
    # X-Forwarded-For (nginx, Caddy, most tunnels; the last address is the
    # one our proxy added) or CF-Connecting-IP (Cloudflare). Believing any
    # header the client might send itself would let it pick its own address
    # and so dodge every rate limit.
    proxy_header: str = "X-Forwarded-For"
    # How often PCs check in (seconds). A PC is "online" if it checked in within
    # ONLINE_FACTOR intervals.
    checkin_interval: int = 30
    # How long the record of what users did with company mailboxes is kept.
    event_days: int = 90

    @property
    def db_path(self) -> Path:
        return self.data_dir / "amail_admin.db"

    @property
    def master_key_path(self) -> Path:
        return self.data_dir / "master.key"

    @property
    def session_key_path(self) -> Path:
        return self.data_dir / "session.key"

    @property
    def signing_key_path(self) -> Path:
        return self.data_dir / "signing.key"

    @property
    def cert_path(self) -> Path:
        return self.data_dir / "server.crt"

    @property
    def cert_key_path(self) -> Path:
        return self.data_dir / "server.key"


ONLINE_FACTOR = 3


def _networks(v: str | None, default) -> tuple:
    items = [x.strip() for x in (v or "").split(",") if x.strip()] or list(default)
    return tuple(ipaddress.ip_network(x, strict=False) for x in items)


def from_env() -> Config:
    default_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir = Path(os.environ.get("AMAIL_ADMIN_DATA", default_dir))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        data_dir=data_dir,
        device_host=os.environ.get("AMAIL_DEVICE_HOST", "0.0.0.0"),
        device_port=int(os.environ.get("AMAIL_DEVICE_PORT", "8443")),
        admin_host=os.environ.get("AMAIL_ADMIN_HOST", "0.0.0.0"),
        admin_port=int(os.environ.get("AMAIL_ADMIN_PORT", "8444")),
        admin_networks=_networks(os.environ.get("AMAIL_ADMIN_NETWORKS"),
                                 DEFAULT_ADMIN_NETWORKS),
        trusted_proxies=_networks(os.environ.get("AMAIL_TRUSTED_PROXIES"), ())
        if os.environ.get("AMAIL_TRUSTED_PROXIES") else (),
        proxy_header=(os.environ.get("AMAIL_PROXY_HEADER") or "X-Forwarded-For").strip(),
        checkin_interval=int(os.environ.get("AMAIL_CHECKIN_INTERVAL", "30")),
        event_days=int(os.environ.get("AMAIL_EVENT_DAYS", "90")),
    )
