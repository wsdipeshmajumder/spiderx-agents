"""Fernet-encrypted carrier credentials.

Plivo and Twilio give the operator one bearer-style credential pair (Auth ID +
Auth Token, or Account SID + Auth Token). Stolen, they let the attacker do
anything in the operator's carrier account — outbound dial, number purchase,
sub-account creation, recording exfiltration. So we encrypt at rest with
Fernet, key out-of-band in the env, and surface only the last-4 of the token
in API responses.

Key rotation: change `TELEPHONY_CRED_KEY` to a comma-separated list (new
key first, old keys after) — Fernet's MultiFernet decrypts with any, encrypts
with the first. Migrate-then-drop the old key once all rows are re-encrypted
in a background job (TODO when we actually rotate).

If `TELEPHONY_CRED_KEY` is unset, encryption is disabled and creds are
stored as plain UTF-8 JSON. This is FINE for local dev but the production
env MUST set the key — `setup_telephony_secret_key()` warns loudly at boot
when missing.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("eva.telephony.secrets")


_FERNET_INSTANCE: Optional[Any] = None
_KEY_WARNING_LOGGED = False


def _get_fernet() -> Optional[Any]:
    """Lazy-init a Fernet (or MultiFernet) instance from env. Returns None
    if the key isn't configured — caller must handle that case (we still
    let local dev work without the key)."""
    global _FERNET_INSTANCE, _KEY_WARNING_LOGGED
    if _FERNET_INSTANCE is not None:
        return _FERNET_INSTANCE
    raw = (os.environ.get("TELEPHONY_CRED_KEY") or "").strip()
    if not raw:
        if not _KEY_WARNING_LOGGED:
            log.warning(
                "TELEPHONY_CRED_KEY not set — carrier creds will be stored "
                "in PLAINTEXT. Set this env var in production: it must be a "
                "url-safe base64 32-byte Fernet key (run "
                "`python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'` to generate one)."
            )
            _KEY_WARNING_LOGGED = True
        return None
    try:
        from cryptography.fernet import Fernet, MultiFernet  # type: ignore
    except ImportError:
        if not _KEY_WARNING_LOGGED:
            log.warning(
                "cryptography package not installed; install it for "
                "Fernet-encrypted carrier creds. Falling back to plaintext.")
            _KEY_WARNING_LOGGED = True
        return None
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    fernets = [Fernet(k.encode() if isinstance(k, str) else k) for k in keys]
    if len(fernets) == 1:
        _FERNET_INSTANCE = fernets[0]
    else:
        _FERNET_INSTANCE = MultiFernet(fernets)
    return _FERNET_INSTANCE


def encrypt_creds(creds: dict[str, Any]) -> bytes:
    """Serialise the creds dict to JSON and Fernet-encrypt. Returns the
    raw token bytes ready to store in a bytea column.

    When no key is configured, returns the UTF-8 JSON wrapped in a
    `b"PLAIN:"` sentinel so we can tell on read whether we need to
    decrypt or not."""
    payload = json.dumps(creds or {}).encode("utf-8")
    f = _get_fernet()
    if f is None:
        return b"PLAIN:" + payload
    return f.encrypt(payload)


def decrypt_creds(stored: Optional[bytes]) -> dict[str, Any]:
    """Inverse of `encrypt_creds`. Returns `{}` if stored is None / empty
    or decryption fails."""
    if not stored:
        return {}
    if isinstance(stored, str):
        stored = stored.encode("utf-8")
    if stored.startswith(b"PLAIN:"):
        try:
            return json.loads(stored[6:].decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    f = _get_fernet()
    if f is None:
        # The blob is Fernet-encrypted but our key is gone. Nothing we can
        # do — the operator will need to re-enter creds.
        log.warning("decrypt_creds: stored blob is encrypted but TELEPHONY_CRED_KEY is unset")
        return {}
    try:
        plain = f.decrypt(stored)
        return json.loads(plain.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("decrypt_creds: failed to decrypt (%s)", e)
        return {}


def mask_token(token: str, *, keep_tail: int = 4) -> str:
    """Render a token for the UI — '••••…last4'. Empty input → ''."""
    s = (token or "").strip()
    if not s:
        return ""
    if len(s) <= keep_tail:
        return "•" * len(s)
    return "•" * (max(4, len(s) - keep_tail)) + s[-keep_tail:]
