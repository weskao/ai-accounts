"""Interactive config menu: pure renderer + pure state machine + thin shell.

Canonical here (rendering half): :func:`render`, turning menu state (a schema-driven
field list, current values, cursor position, and optional edit/error state)
into a bordered ``list[str]`` of display lines. **Pure** — no ``print``, no
``sys.stdout``, no ``isatty`` checks, no cursor-movement escapes. That purity
is the entire point: every behavior here is a plain ``render(...) -> list``
call a test can assert on directly, with zero mocking of a terminal.

Schema-driven, not key-driven: :func:`render` iterates whatever ``Field``
tuple it is given (normally :data:`ai_accounts.config_schema.FIELDS`) — there is
no per-key ``if``/``elif`` chain and no hardcoded list of the six known keys.
Masking is entirely delegated to :mod:`ai_accounts.config_schema`
(``format_value``/``mask_secret``); this module never re-implements or
second-guesses a mask decision, and never prints a masked field's raw value.

Column alignment uses :func:`ai_accounts._present.visible_len` (ANSI-stripped,
East-Asian-width aware), not ``len()`` — a CJK label or a colored value would
otherwise shift the box. The bordered look mirrors
:func:`ai_accounts._present.panel`, but ``panel()`` prints and cannot express a
highlighted cursor row or an edit-buffer/error variant, so this module builds
its own border logic alongside it rather than forcing those needs through a
print-only helper.

**Deliberate decision — edit-buffer echo on a masked field:** while a value
is being typed (``editing=True``), the in-progress buffer is shown in
cleartext, even for ``telegram_bot_token``. This is the user's own terminal,
and the buffer holds a *new*, not-yet-saved value, never the previously
saved secret (which stays masked everywhere else, including mid-edit on
other rows). Recorded here so it reads as a choice, not an oversight.

**Data-loss rule — a masked row never round-trips its own value.** Opening a
masked field seeds the edit buffer EMPTY, never ``field.format(current)``:
``format`` returns the *mask* for a secret, so a prefilled buffer plus a
"looks fine, Enter" would commit ``********WXYZ`` as the new token and
silently destroy it. The empty buffer therefore means "type a new value",
and the two ways out both preserve what is stored:

* **Enter on an empty buffer CANCELS** (leaves the saved secret untouched)
  rather than writing ``""`` — blanking a token by pressing Enter twice is
  the same silent destruction in a different costume. There is deliberately
  NO bare-Enter path to clear a secret; ``config set telegram_bot_token ""``
  is the explicit, typed-out way to do that.
* **Escape cancels** as it does everywhere else.

**Data-loss rule — a save writes only the keys the user touched.** ``values``
is a snapshot taken when the menu opened, so posting all of it to
``save_config`` (whose merge is ``{**on_disk, **updates}``) would make that
stale snapshot beat every concurrent write — another terminal's freshly-set
``telegram_bot_token`` would silently revert to the one the session started
with. :class:`MenuState` therefore carries ``touched``, updated only where a
value actually changes, and :func:`_save` passes that subset. Side benefit: an
untouched key is never written, so the six defaults stop being materialised
into the file on every save. :func:`fallback_menu` already saved a single key
and needs nothing.

The same rule applies in :func:`fallback_menu` (a blank answer for a masked
field keeps the current value, and its prompt says so) — one root cause, both
call sites. Non-masked fields still prefill from ``field.format``, which is
lossless for bools/ints/plain strings and so cannot corrupt anything.

Canonical here (behavioural half): :class:`MenuState` + :func:`step`, the
**pure** ``(state, KeyEvent) -> state`` transition every behaviour test drives
from a fake key iterator; :func:`run_menu`, the thin terminal shell around it
(raw mode, in-place redraw, ``Spinner`` while saving); the non-TTY numbered
fallback; and :func:`cmd_config`, the ONE entry point all six CLIs delegate
to for both the menu and the scriptable ``config get`` / ``config set`` forms.

Delegated elsewhere: key decoding, raw mode and the "is a menu even possible"
check (:mod:`ai_accounts._keyreader`); reading/writing the config file
(:mod:`ai_accounts.autoswitch` — ``load_config``/``save_config``/``masked_config``,
never a direct write from here); per-key types, parsing, ranges and masking
(:mod:`ai_accounts.config_schema`); colors, ``log_*`` and ``Spinner``
(:mod:`ai_accounts._utils`).

Schema-driven, no per-key branching anywhere: whether a row is *typed* or
*cycled* comes from the descriptor (``choices``, or ``type is bool``), and
validation is ``Field.parse``. Appending a 7th field to
``config_schema.FIELDS`` needs no change here — see
``tests/test_config_menu.py::SeventhFieldTest``.

**Deliberate decisions, recorded so they read as choices:**

* *Quit with unsaved changes discards them* (with a yellow warning naming the
  ``s`` key) rather than prompting. A save-on-quit prompt on an escape-hatch
  keystroke is how a half-typed threshold gets persisted; ``s`` is one key.
* *``config set`` echoes a masked value for a masked key* — the only byte
  divergence from the legacy ``ai_accounts`` block, which echoed the raw
  token back. Every other key, including the ``True``/``False`` Python repr
  of booleans, is echoed byte-identically.
* *The non-TTY fallback exits 0 on immediate EOF* (piped/empty stdin): it has
  already printed the full config listing, which is the whole answer for a
  non-interactive caller — nothing failed, so nothing should look like it did.
  An invalid *choice* still exits 1, matching ``_present.choose_profile``.
* *``_present.choose_profile`` is not reused* for that fallback: its header and
  prompt are account-flavored and hardcoded ("Choose a … profile:", "Select
  account number:"), which would read as a bug on a settings list. The shared
  part — numbered rows, ``input()`` wrapped against ``EOFError``/
  ``KeyboardInterrupt``, invalid input to exit 1 — is followed exactly.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from . import autoswitch, config_schema
from . import _keyreader as kr
from ._present import visible_len
from ._utils import BOLD, CYAN, DIM, GREEN, MAGENTA, RED, RESET, YELLOW, Spinner
from ._utils import log_red, log_yellow

_CURSOR_MARK = "❯ "
_NO_CURSOR_MARK = "  "
_UNSET = "(unset)"
_FOOTER_HINT = "↑↓ select · ←→ change · ⏎ edit/toggle · s save · Esc cancel · q/Ctrl-C quit"
_MIN_WIDTH = 40


def _sync_timer(enabled: bool) -> None:
    """Keep the OS scheduled check in step with the ``enabled`` setting.

    ``enabled: true`` on its own only *permits* a switch — something still has
    to run the quota check, or the user is back to typing `codex-accounts
    autoswitch` by hand. Turning the setting on therefore registers the
    periodic job; turning it off unregisters it.

    The import is function-local on purpose: ``autoswitch_timer`` reaches
    ``ai_accounts``, which imports this module, so a top-level import here
    closes a cycle.
    """
    from . import autoswitch_timer

    if enabled:
        autoswitch_timer.install()
    else:
        autoswitch_timer.uninstall()


def _save_config(updates: dict) -> None:
    """``autoswitch.save_config``, plus the timer that makes ``enabled`` real.

    The one write funnel for this module's three save sites (menu, numbered
    fallback, ``config set``), so "turn it on" means the same thing in all
    three. Raises ``ValueError`` from ``save_config`` — the scheduler is only
    touched after the value actually reached the file. ``is True`` rather than
    truthiness, matching ``autoswitch.config_flag``: anything but a real
    boolean true fails closed.
    """
    autoswitch.save_config(updates)
    if "enabled" in updates:
        _sync_timer(updates["enabled"] is True)


def _format_value(field: config_schema.Field, value: object) -> str:
    """*value* rendered for display, honouring the field's masked flag and
    turning an empty string into a visibly-dimmed ``(unset)`` rather than a
    blank cell that reads as a rendering bug."""
    if value == "":
        return f"{DIM}{_UNSET}{RESET}"
    # ``field.format`` directly (not ``config_schema.format_value(key, ...)``)
    # since *field* may not be registered in the global FIELDS tuple — the
    # renderer is schema-driven off whatever tuple it's handed, not a lookup
    # keyed on the canonical registry.
    rendered = field.format(value)
    if field.masked:
        return f"{YELLOW}{rendered}{RESET}"
    if isinstance(value, bool):
        return f"{GREEN}{rendered}{RESET}" if value else f"{DIM}{rendered}{RESET}"
    return rendered


def _field_row(
    field: config_schema.Field,
    value: object,
    *,
    is_cursor: bool,
    label_width: int,
    editing: bool,
    edit_buffer: str,
) -> str:
    marker = _CURSOR_MARK if is_cursor else _NO_CURSOR_MARK
    label = field.label
    pad = " " * (label_width - visible_len(label))
    if is_cursor and editing:
        # Cleartext while typing — see module docstring "Deliberate decision".
        value_text = f"{MAGENTA}{edit_buffer}{RESET}_"
    else:
        value_text = _format_value(field, value)
    label_text = f"{BOLD}{label}{RESET}" if is_cursor else label
    return f"{marker}{label_text}{pad}  {value_text}"


def render(
    title: str,
    fields: Sequence[config_schema.Field],
    values: Mapping[str, object],
    cursor: int,
    *,
    editing: bool = False,
    edit_buffer: str = "",
    error: str | None = None,
    width: int | None = None,
) -> list[str]:
    """Render one frame of the config menu as a ``list[str]`` — no I/O.

    ``fields`` is normally :data:`config_schema.FIELDS`, passed explicitly
    (rather than defaulted) so callers — and tests — can prove the renderer
    is schema-driven by handing it an extended tuple. ``cursor`` is the
    index into ``fields`` of the highlighted row. When ``editing`` is true,
    that row shows ``edit_buffer`` (the in-progress typed value) instead of
    its stored value. ``error``, if given, is shown as its own line — for
    displaying a ``config_schema.parse_value`` ``ValueError`` message.
    """
    label_width = max((visible_len(f.label) for f in fields), default=0)
    field_rows: list[str] = []
    previous_group: str | None = None
    for index, field in enumerate(fields):
        if field.group != previous_group and field.group is not None:
            field_rows.append(f"{BOLD}{field.group}{RESET}")
        field_rows.append(
            _field_row(
                field,
                values.get(field.key, field.default),
                is_cursor=(index == cursor),
                label_width=label_width,
                editing=editing,
                edit_buffer=edit_buffer,
            )
        )
        previous_group = field.group

    body = ["", *field_rows]
    if fields:
        body.append(f"{DIM}{fields[cursor].help}{RESET}")
    if error is not None:
        body.append(f"{RED}⚠ {error}{RESET}")
    body.append("")
    body.append(f"{DIM}{_FOOTER_HINT}{RESET}")

    # ``inner`` is the visible width of everything between the two vertical
    # borders (both the top/bottom dash rule and every content row) — kept
    # to a single number so every returned line has identical visible width.
    max_line = max((visible_len(line) for line in body), default=0)
    inner = max(max_line + 3, visible_len(title) + 4, _MIN_WIDTH - 2)
    if width is not None:
        inner = max(inner, width - 2)

    dashes = inner - visible_len(title) - 3
    top = f"{CYAN}┌─ {BOLD}{title}{RESET}{CYAN} {'─' * dashes}┐{RESET}"
    content_lines = [
        f"{CYAN}│{RESET}  {line}{' ' * (inner - 2 - visible_len(line))}{CYAN}│{RESET}"
        for line in body
    ]
    bottom = f"{CYAN}└{'─' * inner}┘{RESET}"
    return [top, *content_lines, bottom]


# ── state machine (pure: no I/O, no terminal, fully testable) ────────────────


@dataclass(frozen=True)
class MenuState:
    """One frame of menu state. Frozen, so ``step`` returns a new value and a
    test can compare whole states instead of poking at attributes."""

    values: Mapping[str, object]
    cursor: int = 0
    editing: bool = False
    edit_buffer: str = ""
    error: str | None = None
    dirty: bool = False
    pending_save: bool = False
    quitting: bool = False
    #: Keys whose value the USER actually changed this session — the ONLY keys
    #: a save may write. ``values`` is a snapshot taken when the menu opened,
    #: so posting all of it to ``save_config`` would make the stale snapshot win
    #: over every concurrent write (another terminal's freshly-set token
    #: included) and would also materialise all six defaults into the file.
    #: ``frozenset`` — immutable, so it is a safe default on a frozen dataclass.
    touched: frozenset[str] = frozenset()


def _cycle_options(field: config_schema.Field) -> tuple[object, ...] | None:
    """The values this field cycles through, or ``None`` if it is typed.

    Read off the descriptor — an enum cycles its ``choices``, a bool cycles
    false/true. This is the single place "can this row be toggled?" is decided,
    so a new choices-bearing field is cyclable the moment it is declared.
    """
    if field.choices is not None:
        return field.choices
    if field.type is bool:
        return (False, True)
    return None


def _cycled(state: MenuState, field: config_schema.Field, delta: int) -> MenuState:
    options = _cycle_options(field)
    if options is None:
        return state
    current = state.values.get(field.key, field.default)
    position = options.index(current) if current in options else 0
    chosen = options[(position + delta) % len(options)]
    return replace(
        state,
        values={**state.values, field.key: chosen},
        touched=state.touched | {field.key},
        dirty=True,
        error=None,
    )


def _seed_buffer(state: MenuState, field: config_schema.Field) -> str:
    """The edit buffer a freshly-opened row starts with.

    Empty for a masked field — ``field.format`` would hand back the MASK, and
    accepting that unchanged would write the mask over the real secret. For
    every other field ``format`` is lossless (bools ``true``/``false``, ints,
    plain strings all round-trip through ``field.parse``), so prefilling is
    both safe and the friendlier default.
    """
    if field.masked:
        return ""
    return field.format(state.values.get(field.key, field.default))


def _step_editing(
    state: MenuState, event: kr.KeyEvent, field: config_schema.Field
) -> MenuState:
    if event.key is kr.Key.ESCAPE:
        return replace(state, editing=False, edit_buffer="", error=None)
    if event.key is kr.Key.BACKSPACE:
        return replace(state, edit_buffer=state.edit_buffer[:-1])
    if event.key is kr.Key.CHAR and event.char:
        return replace(state, edit_buffer=state.edit_buffer + event.char)
    if event.key is kr.Key.ENTER:
        if field.masked and not state.edit_buffer:
            # Empty commit on a secret = CANCEL, keep what is stored. Writing
            # "" here would silently destroy the token, which is the very bug
            # the empty seed exists to prevent. See module docstring.
            return replace(state, editing=False, edit_buffer="", error=None)
        try:
            value = field.parse(state.edit_buffer)
        except ValueError as exc:
            # Stay in edit mode with the schema's own message: the invalid
            # value never enters `values`, so it can never reach disk.
            return replace(state, error=str(exc))
        if value == state.values.get(field.key, field.default):
            # Committing the value that is already there is not a change: don't
            # mark it touched (that would make an untouched key win over a
            # concurrent write) and don't claim the menu is dirty.
            return replace(state, editing=False, edit_buffer="", error=None)
        return replace(
            state,
            values={**state.values, field.key: value},
            touched=state.touched | {field.key},
            editing=False,
            edit_buffer="",
            error=None,
            dirty=True,
        )
    return state  # UNKNOWN, arrows, anything else: ignored, buffer intact


def _step_browsing(
    state: MenuState, event: kr.KeyEvent, fields: Sequence[config_schema.Field]
) -> MenuState:
    if not fields:
        # No rows to move over, cycle or edit — only save/quit stay meaningful.
        if event.key is kr.Key.CHAR and event.char == "s":
            return replace(state, pending_save=True, error=None)
        if event.key is kr.Key.CHAR and event.char == "q":
            return replace(state, quitting=True)
        return state
    field = fields[state.cursor]
    if event.key is kr.Key.UP:
        return replace(state, cursor=(state.cursor - 1) % len(fields), error=None)
    if event.key is kr.Key.DOWN:
        return replace(state, cursor=(state.cursor + 1) % len(fields), error=None)
    if event.key is kr.Key.RIGHT:
        return _cycled(state, field, 1)
    if event.key is kr.Key.LEFT:
        return _cycled(state, field, -1)
    if event.key is kr.Key.ENTER:
        if _cycle_options(field) is not None:
            return _cycled(state, field, 1)
        return replace(
            state, editing=True, edit_buffer=_seed_buffer(state, field), error=None
        )
    if event.key is kr.Key.CHAR and event.char == "s":
        return replace(state, pending_save=True, error=None)
    if event.key is kr.Key.CHAR and event.char == "q":
        return replace(state, quitting=True)
    return state


def step(
    state: MenuState,
    event: kr.KeyEvent,
    fields: Sequence[config_schema.Field] = config_schema.FIELDS,
) -> MenuState:
    """*state* advanced by one keypress. Pure — the only function tests need.

    ``Key.UNKNOWN`` (T2 leaves SS3 arrows and Home/End/PgUp/PgDn undecoded)
    returns *state* unchanged rather than raising.
    """
    if event.key is kr.Key.CTRL_C:
        return replace(state, quitting=True, editing=False)
    if state.editing:
        return _step_editing(state, event, fields[state.cursor])
    return _step_browsing(state, event, fields)


# ── terminal shell (thin: draw, read one key, hand it to `step`) ─────────────


def _draw(lines: list[str], out, previous: int) -> int:
    """Repaint *lines* in place. Moves the cursor up over the previous frame and
    clears to end of screen, so a frame that lost its error line leaves no
    orphan text — and never hides the cursor, so nothing needs restoring."""
    prefix = f"\033[{previous}A" if previous else ""
    out.write(prefix + "".join(f"{line}\033[K\n" for line in lines) + "\033[J")
    out.flush()
    return len(lines)


def run_menu(
    title: str,
    fields: Sequence[config_schema.Field] = config_schema.FIELDS,
    values: Mapping[str, object] | None = None,
    *,
    read: Callable[[], kr.KeyEvent] = kr.read_key,
    out=None,
) -> int:
    """Run the interactive menu until the user quits. Returns an exit code.

    *read* is the injection seam: production passes ``_keyreader.read_key``,
    tests pass an iterator of ``KeyEvent``s and never touch a terminal.
    """
    out = sys.stdout if out is None else out
    state = MenuState(values=dict(autoswitch.load_config() if values is None else values))
    painted = 0
    with kr.raw_mode():
        try:
            while True:
                painted = _draw(
                    render(
                        title,
                        fields,
                        state.values,
                        state.cursor,
                        editing=state.editing,
                        edit_buffer=state.edit_buffer,
                        error=state.error,
                    ),
                    out,
                    painted,
                )
                state = step(state, read(), fields)
                if state.pending_save:
                    state = _save(state)
                if state.quitting:
                    break
        except KeyboardInterrupt:
            # cbreak leaves ISIG on, so a real Ctrl-C arrives as a signal
            # rather than as a decodable byte — same exit as pressing `q`.
            state = replace(state, quitting=True)
    if state.dirty:
        log_yellow("Discarded unsaved changes (press s to save next time).")
    return 0


def _save(state: MenuState) -> MenuState:
    # ONLY the keys the user actually touched. Handing over the whole snapshot
    # would defeat `save_config`'s merge (every on-disk value would lose to a
    # value read before the session started) and silently revert another
    # terminal's write, secrets included.
    updates = {key: state.values[key] for key in state.touched if key in state.values}
    try:
        with Spinner("Saving config…"):
            _save_config(updates)
    except ValueError as exc:
        # A pre-existing invalid value elsewhere in the stored config (e.g. a
        # hand-edited "notify") poisons the whole-dict rewrite. Surface it on
        # the error line rather than let it traceback, and keep `dirty` true —
        # the save did not actually happen.
        return replace(state, pending_save=False, error=str(exc))
    return replace(
        state, pending_save=False, dirty=False, error=None, touched=frozenset()
    )


# ── non-TTY numbered fallback ───────────────────────────────────────────────


def _ask(prompt: str) -> str | None:
    """``input()`` that answers ``None`` instead of exploding on EOF/Ctrl-C."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def fallback_menu(title: str, fields: Sequence[config_schema.Field] = config_schema.FIELDS) -> int:
    """Numbered picker for when a keyboard menu is impossible (piped stdin,
    CI, `| cat`). Prints the whole config first, so an immediate EOF has
    already been answered — see the module docstring for the exit-0 contract."""
    cfg = autoswitch.load_config()
    print(f"{BOLD}{title}{RESET}")
    for index, field in enumerate(fields, start=1):
        value = _format_value(field, cfg.get(field.key, field.default))
        print(f"  {index}) {field.label}: {value}")

    selection = _ask("Select a setting to change (blank to exit): ")
    if selection is None or not selection:
        return 0
    if not selection.isdecimal() or not 1 <= int(selection) <= len(fields):
        log_red("❌ Enter one of the setting numbers shown above.")
        return 1
    field = fields[int(selection) - 1]

    hint = f" ({'/'.join(field.choices)})" if field.choices else ""
    if field.masked:
        hint = " (blank keeps the current value)"
    raw = _ask(f"New value for {field.label}{hint}: ")
    if raw is None:
        return 0
    if field.masked and not raw:
        # Same rule as the keyboard menu: an empty commit on a secret cancels
        # rather than blanking it. See module docstring.
        return 0
    try:
        value = field.parse(raw)
    except ValueError as exc:
        log_red(f"❌ {exc}")
        return 1
    try:
        _save_config({field.key: value})
    except ValueError as exc:
        # A pre-existing invalid value elsewhere in the stored config (e.g. a
        # hand-edited "notify") poisons the whole-dict rewrite even when the
        # user only touched an unrelated field.
        log_red(f"❌ {exc}")
        return 1
    print(f"{field.key} = {field.format(value) if field.masked else value}")
    return 0


# ── cmd_config: the one entry point all six CLIs delegate to ───────────────


def _valid_keys() -> dict[str, object]:
    """The authoritative key list. ``autoswitch.DEFAULTS`` (not
    ``config_schema.FIELDS``) so a key a newer ai-accounts added to DEFAULTS is
    still settable — matching the legacy block's behaviour exactly."""
    return autoswitch.DEFAULTS


def _descriptor(key: str) -> config_schema.Field:
    """*key*'s schema descriptor, synthesised from its default when the key is
    known to ``DEFAULTS`` but not declared in ``FIELDS`` — so parsing is always
    the schema's, never a second hand-kept type table.

    Caveat for a future *secret*: a synthesised descriptor is ``masked=False``
    (a default value carries no mask flag), so a new secret key MUST be
    declared in ``config_schema.FIELDS`` with ``masked=True``, not dropped into
    ``autoswitch.DEFAULTS`` alone, or its ``set`` echo would print in the
    clear. Every path that displays a *stored* value already reads the flag
    from the schema, so declaring it there is the whole fix."""
    declared = config_schema.field(key)
    if declared is not None:
        return declared
    default = _valid_keys()[key]
    return config_schema.Field(
        key=key, type=type(default), default=default, label=key, help=""
    )


def _unknown_key(key: str) -> int:
    log_red(
        f"❌ Unknown config key: {key!r}. Valid keys: "
        + ", ".join(sorted(_valid_keys()))
    )
    return 1


def cmd_config_get(key: str | None) -> int:
    cfg = autoswitch.masked_config()
    if key is None:
        for name, value in cfg.items():
            print(f"{name} = {value}")
        return 0
    if key not in cfg:
        return _unknown_key(key)
    print(f"{key} = {cfg[key]}")
    return 0


def cmd_config_set(key: str, raw_value: str) -> int:
    if key not in _valid_keys():
        return _unknown_key(key)
    field = _descriptor(key)
    try:
        value = field.parse(raw_value)
    except ValueError as exc:
        log_red(f"❌ {exc}")
        return 1
    try:
        _save_config({key: value})
    except ValueError as exc:
        # A pre-existing invalid value elsewhere in the stored config (e.g. a
        # hand-edited "notify") poisons the merged rewrite even when the user
        # only set an unrelated key — same treatment as `_save` and
        # `fallback_menu`, the other two callers.
        log_red(f"❌ {exc}")
        return 1
    # Masked keys echo masked (see module docstring); everything else echoes
    # the stored value byte-identically to the legacy block.
    print(f"{key} = {field.format(value) if field.masked else value}")
    return 0


def cmd_config(rest: list[str], *, prog: str = "ai-accounts") -> int:
    """``<prog> config [...]`` — the shared implementation for all six CLIs.

    No args → interactive menu, or the numbered fallback when a keyboard menu
    is impossible. ``get [key]`` / ``set <key> <value>`` keep the legacy
    scriptable behaviour. *prog* only labels output (panel title, usage line).
    """
    if not rest:
        title = f"{prog} config"
        if kr.is_interactive_tty():
            return run_menu(title)
        return fallback_menu(title)
    if rest[0] == "get":
        return cmd_config_get(rest[1] if len(rest) > 1 else None)
    if rest[0] == "set" and len(rest) == 3:
        return cmd_config_set(rest[1], rest[2])
    log_red(f"❌ Usage: {prog} config get [key] | config set <key> <value>")
    return 1
