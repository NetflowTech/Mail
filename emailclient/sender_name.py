"""The name to show for an email's sender, without letting it impersonate.

Anyone can put any display name on an email: ``"security@paypal.com"
<attacker@evil.com>`` would show as ``security@paypal.com`` in a list that
shows names only. A display name that contains an email address other than
the real one is therefore replaced by the real address.
"""
from __future__ import annotations

import re

_ADDR_IN_NAME = re.compile(r"[^\s<>@\"']+@[^\s<>@\"']+\.[^\s<>@\"']+")
# Invisible and direction-changing characters that can disguise a name.
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁩﻿]")


def safe_display_name(name: str, addr: str) -> str:
    """*name* when it's harmless, otherwise *addr*. Never empty when either is set."""
    name = _INVISIBLE.sub("", name or "").strip()
    addr = (addr or "").strip()
    if not name:
        return addr
    found = _ADDR_IN_NAME.findall(name)
    if found and addr and any(f.lower().strip(".,;:()[]") != addr.lower()
                              for f in found):
        return addr
    return name
