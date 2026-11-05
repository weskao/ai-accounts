"""Scheduler registration must fail loudly so post-upgrade repair can report it."""

from __future__ import annotations

import subprocess

import pytest

from ai_accounts import autoswitch_timer as timer


def _fail_only_checked_write(cmd, **kwargs):
    """Mimic subprocess.run(check=True) without launching an OS scheduler."""
    if kwargs.get("check"):
        raise subprocess.CalledProcessError(1, cmd)
    return subprocess.CompletedProcess(cmd, 1, "", "")


def test_macos_launchctl_load_failure_is_not_reported_as_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(timer, "_launchd_plist_path", lambda: tmp_path / "agent.plist")
    monkeypatch.setattr(timer, "_tcc_blocked_source", lambda: None)
    monkeypatch.setattr(timer.u, "run", _fail_only_checked_write)

    with pytest.raises(subprocess.CalledProcessError):
        timer._install_macos(300)


def test_windows_scheduler_create_failure_is_not_silent(monkeypatch):
    monkeypatch.setattr(timer.u, "run", _fail_only_checked_write)

    with pytest.raises(subprocess.CalledProcessError):
        timer._install_windows(300)


def test_linux_cron_registration_failure_is_not_silent(monkeypatch):
    monkeypatch.setattr(timer.u, "run", _fail_only_checked_write)

    with pytest.raises(subprocess.CalledProcessError):
        timer._install_linux_cron(300)
