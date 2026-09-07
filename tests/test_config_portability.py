"""``config export`` / ``config import`` — moving settings between machines.

Two rules this file exists to pin down:

* **Secrets never leave.** An export contains no masked field, and an import
  never writes one, even if a hand-edited file carries a mask-shaped value —
  that would destroy the real token stored on the importing machine.
* **The user is told, every time.** Both directions print a notice naming the
  file, the count, and anything skipped; silence about an excluded secret is
  how someone restores a backup and wonders why notifications stopped.

Fixtures use placeholder data only.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import ai_accounts, autoswitch, config_menu, config_schema

_TOKEN = "12345:FAKE-TOKEN-PLACEHOLDER"


class _ConfigFileMixin(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        env = mock.patch.dict(
            os.environ,
            {"AI_ACCOUNTS_CONFIG_JSON": str(self.dir / "config.json")},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ai_accounts.main(argv)
        return rc, out.getvalue(), err.getvalue()

    @property
    def _masked_keys(self) -> tuple[str, ...]:
        return tuple(f.key for f in config_schema.FIELDS if f.masked)


class ExportTest(_ConfigFileMixin):
    def test_writes_every_declared_key_except_secrets(self) -> None:
        autoswitch.save_config(
            {"enabled": True, "switch_when_used_pct": 42, "telegram_bot_token": _TOKEN}
        )
        target = self.dir / "backup.json"
        rc, _out, err = self._run(["config", "export", str(target)])
        self.assertEqual(rc, 0)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["enabled"], True)
        self.assertEqual(data["switch_when_used_pct"], 42)
        for key in self._masked_keys:
            self.assertNotIn(key, data)
        self.assertNotIn(_TOKEN, target.read_text(encoding="utf-8"))
        # Every non-masked declared key is present, so a restore is complete.
        expected = {f.key for f in config_schema.FIELDS if not f.masked}
        self.assertEqual(set(data), expected)
        # The notice names the file and the excluded secret.
        self.assertIn(str(target), err)
        for key in self._masked_keys:
            self.assertIn(key, err)

    def test_no_path_writes_json_to_stdout_and_notice_to_stderr(self) -> None:
        autoswitch.save_config({"telegram_bot_token": _TOKEN})
        rc, out, err = self._run(["config", "export"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertNotIn("telegram_bot_token", data)
        self.assertNotIn(_TOKEN, out)
        self.assertIn("telegram_bot_token", err)

    def test_unwritable_path_reports_and_exits_1(self) -> None:
        rc, _out, err = self._run(["config", "export", str(self.dir / "no" / "\0bad")])
        self.assertEqual(rc, 1)
        self.assertTrue(err.strip())


class ImportTest(_ConfigFileMixin):
    def _write(self, payload: object) -> Path:
        path = self.dir / "incoming.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_applies_known_keys_and_reports_the_count(self) -> None:
        path = self._write({"enabled": True, "switch_when_used_pct": 55})
        rc, _out, err = self._run(["config", "import", str(path)])
        self.assertEqual(rc, 0)
        cfg = autoswitch.load_config()
        self.assertIs(cfg["enabled"], True)
        self.assertEqual(cfg["switch_when_used_pct"], 55)
        self.assertIn("2", err)
        self.assertIn(str(path), err)

    def test_never_overwrites_a_stored_secret(self) -> None:
        autoswitch.save_config({"telegram_bot_token": _TOKEN})
        path = self._write({"telegram_bot_token": "not-a-real-token", "enabled": True})
        rc, _out, err = self._run(["config", "import", str(path)])
        self.assertEqual(rc, 0)
        self.assertEqual(autoswitch.load_config()["telegram_bot_token"], _TOKEN)
        self.assertIs(autoswitch.load_config()["enabled"], True)
        self.assertIn("telegram_bot_token", err)

    def test_unknown_keys_are_skipped_and_named(self) -> None:
        path = self._write({"enabled": True, "from_a_newer_version": 7})
        rc, _out, err = self._run(["config", "import", str(path)])
        self.assertEqual(rc, 0)
        self.assertIs(autoswitch.load_config()["enabled"], True)
        self.assertNotIn("from_a_newer_version", autoswitch._read_json(autoswitch.config_path()))
        self.assertIn("from_a_newer_version", err)

    def test_an_invalid_value_aborts_the_whole_import(self) -> None:
        autoswitch.save_config({"switch_when_used_pct": 10})
        path = self._write({"enabled": True, "notify": "carrier-pigeon"})
        rc, _out, err = self._run(["config", "import", str(path)])
        self.assertEqual(rc, 1)
        cfg = autoswitch.load_config()
        self.assertIs(cfg["enabled"], False)  # nothing landed
        self.assertEqual(cfg["switch_when_used_pct"], 10)
        self.assertIn("notify", err)

    def test_missing_or_junk_file_reports_and_changes_nothing(self) -> None:
        for payload in (None, ["not", "an", "object"]):
            with self.subTest(payload=payload):
                if payload is None:
                    path = self.dir / "absent.json"
                else:
                    path = self._write(payload)
                rc, _out, err = self._run(["config", "import", str(path)])
                self.assertEqual(rc, 1)
                self.assertTrue(err.strip())
                self.assertFalse(autoswitch.config_flag("enabled"))

    def test_round_trips_through_export(self) -> None:
        autoswitch.save_config({"switch_when_used_pct": 33, "layout": "narrow"})
        backup = self.dir / "backup.json"
        self.assertEqual(self._run(["config", "export", str(backup)])[0], 0)
        autoswitch.save_config({"switch_when_used_pct": 99, "layout": "wide"})
        self.assertEqual(self._run(["config", "import", str(backup)])[0], 0)
        cfg = autoswitch.load_config()
        self.assertEqual((cfg["switch_when_used_pct"], cfg["layout"]), (33, "narrow"))


class UsageTest(_ConfigFileMixin):
    def test_usage_line_documents_both_directions(self) -> None:
        rc, _out, err = self._run(["config", "bogus-subcommand"])
        self.assertEqual(rc, 1)
        self.assertIn("export", err)
        self.assertIn("import", err)

    def test_every_cli_shares_the_implementation(self) -> None:
        for prog in ("codex-accounts", "claude-accounts", "vibe-accounts"):
            with self.subTest(prog=prog):
                target = self.dir / f"{prog}.json"
                self.assertEqual(
                    config_menu.cmd_config(["export", str(target)], prog=prog), 0
                )
                self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
