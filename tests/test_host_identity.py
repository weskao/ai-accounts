"""Device labels must survive network changes and failed OS lookups."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_accounts import host_identity as host


def test_macos_serial_is_masked():
    result = subprocess.CompletedProcess([], 0, '    "IOPlatformSerialNumber" = "TEST123456"\n')
    with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", return_value=result):
        assert host.masked_device_code() == "TEST******"


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired("ioreg", 5)])
def test_macos_lookup_failure_keeps_notifications_working(failure):
    with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", side_effect=failure):
        assert host.masked_device_code() == "unknown"


@pytest.mark.parametrize("value", ["", "uninitialized", "0" * 32])
def test_linux_invalid_identity_is_not_a_device(value):
    with patch.object(sys, "platform", "linux"), patch.object(Path, "read_text", return_value=value):
        assert host.masked_device_code() == "unknown"


def test_linux_fallback_id_is_private_and_repeatable():
    value = "1234567890abcdef1234567890abcdef"
    with patch.object(sys, "platform", "linux"), patch.object(Path, "read_text", side_effect=[PermissionError(), value] * 2 + ["1234567890abcdef1234567890abcdee"]):
        first = host.device_code()
        assert len(first) == 12
        assert first not in value
        assert first == host.device_code()
        assert first != host.device_code()


@pytest.mark.parametrize("result", [
    subprocess.CompletedProcess([], 1, '"IOPlatformSerialNumber" = "TEST123456"'),
    subprocess.CompletedProcess([], 0, "missing serial"),
    subprocess.CompletedProcess([], 0, '"IOPlatformSerialNumber" = ""'),
])
def test_macos_bad_response_does_not_leak_or_invent_an_id(result):
    with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run", return_value=result):
        assert host.masked_device_code() == "unknown"


def test_linux_unreadable_files_keep_notifications_working():
    with patch.object(sys, "platform", "linux"), patch.object(Path, "read_text", side_effect=PermissionError()):
        assert host.masked_device_code() == "unknown"


def test_windows_registry_id_is_private_and_repeatable():
    registry = MagicMock()
    registry.QueryValueEx.return_value = ("12345678-90ab-cdef-1234-567890abcdef", 1)
    with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, winreg=registry):
        first = host.device_code()
        assert len(first) == 12
        assert first == host.device_code()
        assert first != "1234567890ab"
        registry.QueryValueEx.side_effect = PermissionError()
        assert host.masked_device_code() == "unknown"
