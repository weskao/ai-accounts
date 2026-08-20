"""One-time installation shared by ``autoswitch setup`` and config."""

from __future__ import annotations


def install() -> None:
    """Install event hooks plus the low-frequency OS fallback."""
    from . import autoswitch_hooks, autoswitch_timer

    autoswitch_timer.install()
    autoswitch_hooks.install()


def is_installed() -> bool:
    """A complete setup needs both the scheduler and relevant hooks."""
    from . import autoswitch_hooks, autoswitch_timer

    return autoswitch_timer.status() == "installed" and autoswitch_hooks.is_installed()
