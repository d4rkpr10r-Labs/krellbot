"""Read-only local security posture snapshot.

The first-run / Exchange wizard and the workstation both need to tell the
user "here is what is true about this machine right now" without ever
touching a credential. This module exposes the only view of the local
posture that is allowed into the UI layer.

Everything in the returned dict is presentation-only:

- home / home_mode: where state lives, and how private the directory is.
- keychain_backend / keychain_ok: identity-only; no secret lookups.
- posture_ok / posture_warning: the **aggregate** posture. ``posture_ok``
  is True only when BOTH the backend is persistent AND the home mode is
  0o700 (or unreadable — that is itself a posture issue we cannot hide).
- bind: the loopback interface only.
- live_arm_ui_allowed / trade_only_required: safety rails.
- credential_source: per venue, which source ``secrets.get`` uses
  (``keychain``, ``env_override``, or ``keyfile_fallback`` when a
  plaintext key file backs up the keychain), derived from environment
  variable presence only.

This module never reads, writes, or imports any venue credential. The
keychain backend identity is exposed as a *name* (class path) and a
boolean; if the backend is null/fake/fail, keychain_ok is False. It
never returns an environment variable's value or a key file path.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from krellbot.doctor import _keychain_backend
from krellbot.secrets import VENUES as _CREDENTIAL_VENUES

_IS_WINDOWS = sys.platform == "win32"

# A home directory is "private" exactly when its POSIX mode is 0o700.
# A wider mode means other local users can read the keys, journal, and
# receipts. We never accept 0o755, 0o775, or 0o777 as private.
_HOME_MODE_PRIVATE = "0o700"


def _home_mode_or_none(home: Path) -> str | None:
    """Return home's POSIX mode as an octal string, or None if unreadable.

    On Windows we return None — the wizard has nothing useful to say
    about DACLs in this version.
    """
    if _IS_WINDOWS:
        return None
    try:
        st = os.stat(str(home))
    except OSError:
        return None
    return oct(stat.S_IMODE(st.st_mode))


def _credential_source(venue: str) -> str:
    """Name where ``secrets.get(venue)`` takes credentials from.

    Mirrors the precedence in ``secrets.get`` without reading a credential,
    a keyring entry or a key file:

    - ``env_override``: ``KRELLBOT_<VEN>_KEY`` or ``_SECRET`` is present.
      ``secrets.get`` then uses the pair, or refuses a lone half, and never
      consults the keychain.
    - ``keyfile_fallback``: ``KRELLBOT_<VEN>_KEYFILE`` is set and non-empty
      (``secrets.get`` ignores an empty one). The keychain is still read
      first; the plaintext file is used only when it has no entry.
    - ``keychain``: neither is set; only the OS keychain is consulted.

    Only presence is tested. No value is kept or returned.
    """

    ven = venue.upper()
    if f"KRELLBOT_{ven}_KEY" in os.environ or f"KRELLBOT_{ven}_SECRET" in os.environ:
        return "env_override"
    if os.environ.get(f"KRELLBOT_{ven}_KEYFILE"):
        return "keyfile_fallback"
    return "keychain"


def trust_snapshot(home: Path) -> dict[str, object]:
    """Return a presentation-only snapshot of local trust posture.

    The dict shape is fixed. Callers must not depend on extra keys.

    The ``posture_ok`` aggregate is True only when the backend is
    persistent AND the home mode is the private 0o700. Any other
    combination produces a non-empty ``posture_warning`` string the
    UI can render so the user sees the truth.
    """
    backend, backend_warn = _keychain_backend()
    backend_ok = backend_warn is None
    home_mode = _home_mode_or_none(home)
    home_mode_ok = home_mode == _HOME_MODE_PRIVATE

    warnings: list[str] = []
    if not backend_ok:
        # Trust snapshot must not invent a backend name; surface the
        # backend warning verbatim.
        warnings.append(backend_warn or "keychain backend unavailable")
    if home_mode is None:
        if _IS_WINDOWS:
            warnings.append("home mode not checkable on Windows; review DACLs manually")
        else:
            warnings.append("home mode is unreadable; cannot confirm 0o700")
    elif not home_mode_ok:
        warnings.append(
            f"home mode is {home_mode}, expected {_HOME_MODE_PRIVATE} — another local user may read this directory"
        )
    posture_ok = not warnings
    posture_warning = None if posture_ok else "; ".join(warnings)

    return {
        "home": str(home),
        "home_mode": home_mode,
        "home_mode_ok": home_mode_ok,
        "keychain_backend": backend,
        "keychain_ok": backend_ok,
        "posture_ok": posture_ok,
        "posture_warning": posture_warning,
        "bind": "127.0.0.1",
        "live_arm_ui_allowed": False,
        "trade_only_required": True,
        "credential_source": {venue: _credential_source(venue) for venue in sorted(_CREDENTIAL_VENUES)},
    }


__all__ = ["trust_snapshot"]
