"""`i18n.t(..., default=...)` must not restate a string the catalogue already has.

The catalogue's ``en`` entry always wins over ``default``, so such a default is
dead text that silently drifts out of sync the next time the message is edited.
A ``default`` is only legitimate for a msgid with no ``en`` entry, where the
call site *is* the English source.
"""

import ast
import pathlib

from ai_accounts import i18n

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "ai_accounts"


def test_no_default_duplicates_catalogue_english():
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            msgid = node.args[0]
            if name != "t" or not isinstance(msgid, ast.Constant):
                continue
            if not any(kw.arg == "default" for kw in node.keywords):
                continue
            if "en" in (i18n.MESSAGES.get(msgid.value) or {}):
                offenders.append(f"{path.name}:{node.lineno} {msgid.value}")
    assert not offenders, "drop the redundant default=: " + ", ".join(offenders)
