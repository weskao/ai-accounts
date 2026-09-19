"""Send a Telegram message via the Bot API. Stdlib-only, zero project dependencies.

Copy this single file into any other Python project that needs Telegram
notifications without a shell-script dependency (e.g. ~/.claude/scripts/tg-send.sh).
"""

from __future__ import annotations

import http.client
import urllib.error
import urllib.parse
import urllib.request

_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(token: str, chat_id: str, text: str, *, timeout: float = 10) -> bool:
    """POST one sendMessage to the Bot API. False on missing credentials or any failure."""
    if not token or not chat_id:
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(_API.format(token=token), data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return False  # incl. InvalidURL from a hand-corrupted token
    return True


def demo() -> None:
    assert send_telegram("", "42", "x") is False
    assert send_telegram("tok", "", "x") is False

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    import unittest.mock as mock

    with mock.patch("urllib.request.urlopen", return_value=_FakeResponse()):
        assert send_telegram("tok", "42", "hello") is True

    with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        assert send_telegram("tok", "42", "hello") is False

    print("telegram_notify.demo: ok")


if __name__ == "__main__":
    demo()
