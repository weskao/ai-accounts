"""Keep secrets out of ``config.json``: store them in the OS credential store.

``telegram_bot_token`` is the only secret ai-accounts owns — every provider
credential belongs to that vendor's own CLI — and it is never written to the
config file, not even at 0600. It goes to whichever native store the machine
has, reached through the same :mod:`ai_accounts._utils` helpers the provider
modules already use for live credential slots:

===========  ===========================================================
macOS        Keychain, via the ``security`` CLI
Linux        Secret Service (GNOME Keyring / KWallet), via ``secret-tool``
Windows      Credential Manager, via ``CredWriteW``
none         **nothing is stored**
===========  ===========================================================

That last row is the point of the module. With no credential store available
it refuses to write rather than falling back to a plaintext file or to
home-rolled obfuscation, and the caller tells the user to export the
environment variable instead. An encoding anyone can reverse is not storage
security, it is a comforting lie, so it is not offered.

Read precedence is **environment > credential store > legacy plaintext
config**. The last rung is what keeps an existing install working: a token
already sitting in ``config.json`` is still honoured, and the next
:func:`ai_accounts.autoswitch.save_config` moves it into the store and drops
it from the file.

The backends are the ``go_keyring_*`` helpers rather than new code. Their name
describes the *convention* they implement (how Go CLIs lay out a slot), but
the mechanism is an ordinary generic-secret read/write on all three platforms,
and it already handles the traps a fresh implementation would rediscover —
notably that ``security add-generic-password -w`` prompts the tty instead of
reading stdin, so the secret has to go through ``security -i`` batch mode to
stay out of ``ps``.
"""

from __future__ import annotations

import os

from . import _utils as u

#: Keychain service / Secret Service attribute identifying this program's items.
SERVICE = "ai-accounts"


def env_var(key: str) -> str:
    """The environment variable that overrides *key* (e.g. ``AI_ACCOUNTS_TELEGRAM_BOT_TOKEN``)."""
    return f"AI_ACCOUNTS_{key.upper()}"


def available() -> bool:
    """Whether this machine has a credential store we can write to."""
    return u.go_keyring_available()[0]


def unavailable_reason() -> str:
    """Why :func:`available` is False — an install hint on Linux, else a platform note."""
    return u.go_keyring_available()[1]


def get(key: str) -> str:
    """The secret for *key*, or ``""`` when there is none.

    Never raises: a locked keyring, a missing helper or a denied prompt all
    mean "no secret", which every caller already handles.
    """
    from_env = os.environ.get(env_var(key), "")
    if from_env:
        return from_env
    if not available():
        return ""
    return u.go_keyring_read(SERVICE, key) or ""


def set(key: str, value: str) -> bool:  # noqa: A001 - the store's verb, not the builtin
    """Store *value* for *key*. ``False`` when it could not be stored securely.

    An empty *value* removes the item instead of storing a blank — that is how
    the menu and ``config set <key> ""`` clear a secret.
    """
    if not available():
        return False  # refuse rather than write plaintext; see the module docstring
    if not value:
        return delete(key)
    return u.go_keyring_write(SERVICE, key, value)


def delete(key: str) -> bool:
    """Remove the stored secret for *key*. ``False`` if there was nothing to remove."""
    if not available():
        return False
    return u.go_keyring_delete(SERVICE, key)
