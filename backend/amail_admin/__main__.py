"""Run the AMail admin backend, or set it up.

    python -m amail_admin init --url https://amail.example.com:8443
    python -m amail_admin create-admin <username>
    python -m amail_admin run
    python -m amail_admin setup-code
"""
from __future__ import annotations

import argparse
import getpass
import logging
import socket
import sys
import threading

from . import crypto
from .config import from_env
from .store import Store


class App:
    """Everything the servers share, loaded once."""

    def __init__(self, extra_names: list[str] | None = None):
        self.cfg = from_env()
        self.store = Store(self.cfg.db_path)
        self.master = crypto.load_or_create_master_key(self.cfg.master_key_path)
        self.session_key = crypto.load_or_create_random(self.cfg.session_key_path)
        self.signing_key = crypto.load_or_create_signing_key(
            self.cfg.signing_key_path)
        self.signing_pub = crypto.signing_public(self.signing_key)
        crypto.ensure_certificate(self.cfg.cert_path, self.cfg.cert_key_path,
                                  extra_names)
        if not self.store.get_setting("enroll_code"):
            self.store.set_setting("enroll_code", crypto.new_enroll_code())


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def cmd_init(args) -> int:
    names = [n for n in (args.url_host, _lan_ip()) if n]
    app = App(extra_names=names)
    if args.url:
        app.store.set_setting("public_url", args.url.rstrip("/"))
    print(f"Data folder:  {app.cfg.data_dir}")
    print(f"Certificate:  {crypto.certificate_fingerprint(app.cfg.cert_path)}")
    print(f"PC address:   {app.store.get_setting('public_url') or '(not set)'}")
    print(f"Admin panel:  https://{_lan_ip()}:{app.cfg.admin_port}")
    if app.store.admin_count() == 0:
        print("\nNo admin yet. Run:  python -m amail_admin create-admin <username>")
    return 0


def cmd_create_admin(args) -> int:
    app = App()
    pw = getpass.getpass("New admin password (12+ characters): ")
    if len(pw) < 12:
        print("Too short.")
        return 1
    if getpass.getpass("Again: ") != pw:
        print("They don't match.")
        return 1
    app.store.set_admin(args.username, crypto.hash_password(pw))
    if args.reset_2fa:
        app.store.set_totp(args.username, "", 0)
    app.store.audit("console", "admin_set",
                    args.username + (" (two-step reset)" if args.reset_2fa else ""))
    print(f"Admin {args.username!r} saved. Their open sessions were ended.")
    row = app.store.get_admin(args.username)
    if not row["totp_secret"]:
        print("They set up two-step sign-in (an authenticator app) at their "
              "next sign-in.")
    return 0


def cmd_setup_code(_args) -> int:
    app = App()
    url = app.store.get_setting("public_url")
    if not url:
        print("Set the PC address first: python -m amail_admin init --url https://...:8443")
        return 1
    fp = (crypto.certificate_fingerprint(app.cfg.cert_path)
          if app.store.get_setting("pin_cert", "1") == "1" else "")
    print(crypto.make_setup_code(url, fp, app.store.get_setting("enroll_code"),
                                 app.signing_pub))
    return 0


def cmd_export_key(_args) -> int:
    """Print the master key so it can be kept somewhere safe (a password
    manager). On Windows the key file only opens on this machine; this is the
    way to restore the database on a replacement server."""
    app = App()
    print("This prints the key that, with the database, unlocks every mailbox\n"
          "password. Make sure nobody can see your screen, and clear the window\n"
          "afterwards.")
    if input('Type "show" to print it: ').strip().lower() != "show":
        return 1
    from cryptography.hazmat.primitives import serialization
    sign_raw = app.signing_key.private_bytes(serialization.Encoding.Raw,
                                             serialization.PrivateFormat.Raw,
                                             serialization.NoEncryption())
    # Both machine-locked keys in one line: the master key, then the key that
    # signs check-in answers (without it PCs must be reconnected).
    print(crypto.b64e(app.master) + "." + crypto.b64e(sign_raw))
    app.store.audit("console", "master_key_exported")
    return 0


def cmd_import_key(_args) -> int:
    cfg = from_env()
    for path in (cfg.master_key_path, cfg.signing_key_path):
        if path.exists():
            print(f"{path} already exists. Move it away first if you really "
                  "mean to replace it.")
            return 1
    parts = getpass.getpass("Saved key (from export-key): ").strip().split(".")
    try:
        master = crypto.b64d(parts[0])
        sign = crypto.b64d(parts[1]) if len(parts) > 1 else b""
    except Exception:
        master = sign = b""
    if len(master) != 32 or len(sign) not in (0, 32):
        print("That isn't a saved AMail key.")
        return 1
    crypto.save_master_key(cfg.master_key_path, master)
    if sign:
        crypto._write_protected(cfg.signing_key_path, sign)
    print("Keys restored." if sign else
          "Master key restored. (No signing key in it: PCs must be reconnected "
          "with a new setup code.)")
    return 0


def cmd_run(_args) -> int:
    from cheroot import wsgi
    from cheroot.ssl.builtin import BuiltinSSLAdapter

    from .device_api import create_device_app
    from .panel import create_panel_app, oauth_clients

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = App()
    cfg = app.cfg
    if app.store.admin_count() == 0:
        print("No admin account. Run: python -m amail_admin create-admin <username>")
        return 1

    import time as _time
    app.store.prune_audit(_time.time() - 400 * 86400)   # keep about 13 months
    app.store.prune_events(_time.time() - cfg.event_days * 86400)
    servers = []
    for host, port, wsgi_app, threads in (
            (cfg.device_host, cfg.device_port,
             create_device_app(app.store, app.master, cfg.checkin_interval,
                               app.signing_key,
                               trusted_proxies=cfg.trusted_proxies,
                               oauth_clients=lambda: oauth_clients(app.store),
                               event_retention=cfg.event_days * 86400), 40),
            (cfg.admin_host, cfg.admin_port,
             create_panel_app(cfg, app.store, app.master, app.session_key,
                              app.signing_pub), 10)):
        # Short socket timeouts and a deeper queue: a client that opens
        # connections and then sends nothing (slow-loris) holds a thread for
        # seconds, not minutes.
        srv = wsgi.Server((host, port), wsgi_app, numthreads=threads,
                          server_name="amail-admin", timeout=8,
                          request_queue_size=64, accepted_queue_timeout=5)
        srv.max_request_header_size = 16 * 1024
        srv.max_request_body_size = 256 * 1024
        srv.ssl_adapter = BuiltinSSLAdapter(str(cfg.cert_path),
                                            str(cfg.cert_key_path))
        servers.append(srv)

    threads = [threading.Thread(target=s.start, daemon=True) for s in servers]
    for t in threads:
        t.start()
    logging.info("Device API on https://%s:%s, admin panel on https://%s:%s",
                 cfg.device_host, cfg.device_port, cfg.admin_host, cfg.admin_port)
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.stop()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="amail_admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create keys and certificate, set the PC address")
    i.add_argument("--url", help="address PCs use, e.g. https://amail.example.com:8443")
    i.set_defaults(fn=cmd_init)
    a = sub.add_parser("create-admin", help="create an admin or reset their password")
    a.add_argument("username")
    a.add_argument("--reset-2fa", action="store_true",
                   help="make them set up their authenticator app again "
                        "(lost phone)")
    a.set_defaults(fn=cmd_create_admin)
    sub.add_parser("setup-code", help="print the setup code for a new PC").set_defaults(
        fn=cmd_setup_code)
    sub.add_parser("run", help="run both servers").set_defaults(fn=cmd_run)
    sub.add_parser("export-key", help="print the master key for safekeeping").set_defaults(
        fn=cmd_export_key)
    sub.add_parser("import-key", help="restore the master key on a new server").set_defaults(
        fn=cmd_import_key)
    args = p.parse_args(argv)
    if args.cmd == "init":
        from urllib.parse import urlparse
        args.url_host = urlparse(args.url).hostname if args.url else None
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
