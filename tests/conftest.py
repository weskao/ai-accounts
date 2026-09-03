"""Shared pytest fixtures for the whole suite.

**Locale pin (module level, HARD requirement, must run before any
``ai_accounts`` import).** Several module-level constants across the package
resolve the OS locale exactly ONCE, at first import, and freeze the result
for the rest of the process — by design (see ``config_schema.py``'s
``language`` field: "resolved once, at import"), but with a real
hermeticity cost for tests: ``config_schema.FIELDS``'s ``language`` entry,
``autoswitch.DEFAULTS`` (built from it), and every function whose default
argument is one of those two objects (e.g. ``config_menu.step``'s
``fields=config_schema.FIELDS`` parameter) all bake in whatever
``i18n.system_language()`` resolves to AT IMPORT TIME. A later per-test
fixture patching ``os.environ`` or even ``config_schema.FIELDS`` itself
cannot reach an already-bound function-default-argument object — the only
sound fix is to make the input deterministic BEFORE any of those first
imports happen. pytest always imports ``conftest.py`` before any test
module, so pinning the locale env vars here, at module scope (not inside a
fixture), guarantees every one of those baked-in values is ``"en"``
regardless of the locale the test process actually started under (a
contributor's or CI runner's non-English ``LANG``/``LC_ALL``/``LC_MESSAGES``).
"""

from __future__ import annotations

import os

os.environ["LANG"] = "en_US.UTF-8"
os.environ.pop("LC_ALL", None)
os.environ.pop("LC_MESSAGES", None)

import pytest

from ai_accounts import _present, i18n

# Comfortably above NARROW_BELOW so this stays a wide pin even if that
# threshold is ever raised.
_WIDE_COLUMNS = _present.NARROW_BELOW * 4


@pytest.fixture(autouse=True)
def _pin_wide_layout(monkeypatch):
    """``_present.layout_mode()`` caches its answer process-wide until
    :func:`ai_accounts._present.reset_layout_cache` is called, and —
    whenever the ``layout`` config key is left at its shipped default
    ``"auto"`` (true of almost every test in this suite, since almost none
    of them set that key) — it resolves by asking
    :func:`ai_accounts._present.terminal_width`, i.e. the ambient
    ``COLUMNS``/tty width of whatever is running the tests.

    Without pinning, any test that asserts on WIDE box-drawing output (which
    is most of this suite, written before ``layout`` existed) passes only by
    accident of the developer's terminal being >= ``NARROW_BELOW`` columns
    wide — and goes red under a narrower terminal, a narrow-``COLUMNS`` CI
    runner, or an ``ssh``/tmux session. A suite whose result depends on the
    window size of the machine it happens to run on is the actual defect:
    pin every test to a wide terminal by default here, once, so ambient
    width can never leak in. Tests that specifically exercise narrow mode
    opt in explicitly — either by passing ``mode="narrow"`` (which always
    wins, see ``_present.panel``/``accounts_table``/``config_menu.render``)
    or by patching ``COLUMNS`` themselves and calling
    ``reset_layout_cache()`` again inside the test.
    """
    monkeypatch.setenv("COLUMNS", str(_WIDE_COLUMNS))
    _present.reset_layout_cache()
    yield
    _present.reset_layout_cache()


@pytest.fixture(autouse=True)
def _isolate_real_config(monkeypatch, tmp_path):
    """Point every test at a throwaway config file instead of the real
    ``~/.ai-accounts/config.json``.

    Found while chasing the layout-pinning bug: a handful of tests (this
    file's own included, via ``cs.parse_value``/``i18n.t`` reading
    ``i18n.current_language()``) never override ``AI_ACCOUNTS_CONFIG_JSON``
    themselves, so they silently read whatever is really on disk on the
    machine running the suite — e.g. a real ``language: "zh-TW"`` — and,
    because :func:`ai_accounts.i18n.current_language` is process-wide
    ``lru_cache``d, poison every later test in the same run too. Same root
    cause as the layout cache and the locale pin above: a test's outcome
    must not depend on ambient machine state. Most test classes already
    self-isolate with their own ``AI_ACCOUNTS_CONFIG_JSON`` override (see
    ``_ConfigMixin`` in ``test_ai_accounts.py``) — this is just that same
    pattern made the default for the handful that don't, via a fresh path
    per test so no two tests can see each other's writes either.
    """
    monkeypatch.setenv("AI_ACCOUNTS_CONFIG_JSON", str(tmp_path / "ai-accounts-config.json"))
    i18n.refresh()
    yield
    i18n.refresh()
