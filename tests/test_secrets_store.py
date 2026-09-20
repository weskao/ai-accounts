"""The bot token lives in the OS credential store, never in ``config.json``.

The credential store itself is faked per-test by the autouse
``_isolate_real_keychain`` fixture in ``conftest.py`` — it swaps only
``secrets_store``'s handle on ``_utils``, so the precedence and
refuse-to-store logic under test here is the real thing and no test ever
writes to the developer's own keychain. The platform dispatchers underneath
are covered separately by ``test_cross_platform.py``.

Fixtures use placeholder data only.
"""

from __future__ import annotations

import json

import pytest

from ai_accounts import autoswitch, secrets_store

_TOKEN = "12345:FAKE-TOKEN-PLACEHOLDER"


def _file_contents() -> dict:
    path = autoswitch.config_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class TestTheFileNeverHoldsTheSecret:
    def test_saving_a_token_keeps_it_out_of_the_config_file(self):
        autoswitch.save_config({"telegram_bot_token": _TOKEN, "enabled": True})
        raw = autoswitch.config_path().read_text(encoding="utf-8")
        assert _TOKEN not in raw
        assert "telegram_bot_token" not in _file_contents()
        # ...but it is still the effective value every caller sees.
        assert autoswitch.load_config()["telegram_bot_token"] == _TOKEN

    def test_a_non_secret_saved_alongside_still_lands_in_the_file(self):
        autoswitch.save_config({"telegram_bot_token": _TOKEN, "switch_when_used_pct": 42})
        assert _file_contents()["switch_when_used_pct"] == 42

    def test_clearing_the_token_removes_it_from_the_store(self, _isolate_real_keychain):
        autoswitch.save_config({"telegram_bot_token": _TOKEN})
        assert _isolate_real_keychain.slots
        autoswitch.save_config({"telegram_bot_token": ""})
        assert autoswitch.load_config()["telegram_bot_token"] == ""
        assert not _isolate_real_keychain.slots


class TestPrecedence:
    def test_the_environment_wins_over_the_stored_value(self, monkeypatch):
        autoswitch.save_config({"telegram_bot_token": _TOKEN})
        monkeypatch.setenv("AI_ACCOUNTS_TELEGRAM_BOT_TOKEN", "from-the-environment")
        assert autoswitch.load_config()["telegram_bot_token"] == "from-the-environment"

    def test_the_env_var_is_named_after_the_key(self):
        assert secrets_store.env_var("telegram_bot_token") == "AI_ACCOUNTS_TELEGRAM_BOT_TOKEN"


class TestBackCompat:
    """An install that already has a plaintext token must not break."""

    def _legacy_file(self) -> None:
        path = autoswitch.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"telegram_bot_token": _TOKEN, "enabled": True}), encoding="utf-8"
        )

    def test_a_plaintext_token_already_in_the_file_is_still_honoured(self):
        self._legacy_file()
        assert autoswitch.load_config()["telegram_bot_token"] == _TOKEN

    def test_the_next_save_migrates_it_into_the_store_and_off_disk(self):
        self._legacy_file()
        autoswitch.save_config({"switch_when_used_pct": 42})  # unrelated setting
        assert _TOKEN not in autoswitch.config_path().read_text(encoding="utf-8")
        assert autoswitch.load_config()["telegram_bot_token"] == _TOKEN


class TestNoCredentialStore:
    """Refuse to store rather than fall back to plaintext or fake encryption."""

    def test_saving_a_new_secret_raises_and_names_the_env_var(self, _isolate_real_keychain):
        _isolate_real_keychain.unavailable = "no secret-tool"
        with pytest.raises(ValueError) as caught:
            autoswitch.save_config({"telegram_bot_token": _TOKEN})
        message = str(caught.value)
        assert "AI_ACCOUNTS_TELEGRAM_BOT_TOKEN" in message
        assert "no secret-tool" in message
        assert _TOKEN not in message

    def test_the_refused_secret_never_reaches_the_file(self, _isolate_real_keychain):
        _isolate_real_keychain.unavailable = "no secret-tool"
        with pytest.raises(ValueError):
            autoswitch.save_config({"telegram_bot_token": _TOKEN})
        # The raise happens before the write, so there is nothing on disk at all.
        assert "telegram_bot_token" not in _file_contents()

    def test_an_unrelated_save_does_not_destroy_a_legacy_plaintext_token(
        self, _isolate_real_keychain
    ):
        # The only copy the user has is in the file and we cannot move it, so
        # leave it exactly where it is rather than dropping it on the floor.
        path = autoswitch.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"telegram_bot_token": _TOKEN}), encoding="utf-8")
        _isolate_real_keychain.unavailable = "no secret-tool"
        autoswitch.save_config({"switch_when_used_pct": 42})
        assert _file_contents()["telegram_bot_token"] == _TOKEN

    def test_saving_only_non_secrets_still_works(self, _isolate_real_keychain):
        _isolate_real_keychain.unavailable = "no secret-tool"
        autoswitch.save_config({"switch_when_used_pct": 42})
        assert _file_contents()["switch_when_used_pct"] == 42


class TestSchemaDriven:
    def test_the_secret_list_comes_from_the_schema_not_a_hand_written_list(self):
        from ai_accounts import config_schema

        expected = tuple(f.key for f in config_schema.FIELDS if f.masked)
        assert autoswitch._secret_keys() == expected
        assert "telegram_bot_token" in expected
