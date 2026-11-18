"""AMail admin backend.

Two HTTPS servers in one process:
  • the device API (port 8443): AMail PCs enroll and check in. Can be reachable
    from the internet; only enrolled, admin-approved PCs get anything.
  • the admin panel (port 8444): office network only, admin login.

Mailbox passwords are stored encrypted under a master key, and each one is sent
to a PC encrypted so that only that PC's private key can open it.
"""
__version__ = "1.0.0"
