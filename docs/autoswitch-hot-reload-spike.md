# Auto-switch hot-reload spike

**Question:** for each AI CLI whose accounts `ai-accounts` manages, does a **running** process pick up
rotated credentials **without being restarted**?

**Fallback ladder** (the rung each provider ships on):

| rung | meaning |
| --- | --- |
| `seamless` | the live session picks up the new account; notify only, never restart |
| `auto-restart` | not seamless, but the session can be restarted programmatically — do that |
| `manual-restart` | neither; tell the user to restart by hand |

Every claim below is labelled `observed:` (I ran it and saw the output), `inferred:` (derived from
source or documented behaviour, not directly witnessed), or `not observed:` (a check I deliberately
did not run, and why). All probes ran in a throwaway sandbox; see
[Safety containment](#safety-containment).

---

## Summary

| provider | verdict | confidence (observed / inferred) |
| --- | --- | --- |
| codex | `auto-restart` | inferred |
| claude | `auto-restart` — **conditional** ¹ | inferred |
| agy (antigravity) | `auto-restart` (CLI) / `manual-restart` (IDE running) ² | inferred |
| grok | `auto-restart` — **unreachable** ³ | inferred |

¹ **claude:** auto-restart MUST exclude the restarting process's own session. See
[the claude section](#claude).

² **agy:** `auto-restart` applies to the `agy` CLI only. If an Antigravity **IDE** process is
running, it re-writes the keyring instantly and has no programmatic resume — detect it and downgrade
to `manual-restart`. See [the agy section](#agy-antigravity).

³ **grok:** the rung is real but nothing can reach it — xAI ships **no quota API** for Grok Build, so
`grok-accounts autoswitch` prints `autoswitch unsupported for grok: no quota API` and returns before
the switch engine is ever consulted. grok's verdict is what it *would* ship the day a quota API
exists, not shipped behavior. See [the grok section](#grok).

**Do not consume the verdict column without these three footnotes** — an unconditional `auto-restart`
for agy ships the exact behaviour this spike found unsafe, and grok's cannot fire at all.

No provider reached `seamless`. Every provider's *restartability* is `observed:` (each CLI has a
session-resume flag); what stays `inferred:` is the negative — that a live session does **not**
re-read a rotated credential store. That negative cannot be observed without valid credentials for
two accounts, which this spike was explicitly scoped away from.

**Cross-cutting finding (`inferred:`, applies to all four):** the risk is not merely that a running
session ignores a rotation — it is that the running session **clobbers** it. All four CLIs refresh
their own OAuth tokens in memory during normal use and write the rotated result back to the same
store `ai-accounts` just overwrote. `ai-accounts` already compensates for this on the *outgoing* side by
folding the live token back into its profile before overwriting
(`src/ai-accounts/codex_accounts.py:938-947`, `src/ai-accounts/claude_accounts.py:503-509`,
`src/ai-accounts/gemini_accounts.py:858-866`) — but nothing protects the *incoming* account from a
still-running process writing its old token back on top. This makes `seamless` not just unavailable
but unsafe to fake.

---

## codex

### What I did

`observed:` Ran `codex exec` twice inside a throwaway `CODEX_HOME`, once with **no** `auth.json` and
once with a **malformed** one. Both sandboxed via `CODEX_HOME` /
`CODEX_AUTH_JSON` (`src/ai-accounts/codex_accounts.py:96`, `:108`).

### What I saw

`observed:` With an empty sandbox `CODEX_HOME`, codex did **not** gate at startup. It printed a full
session header and only failed when it tried to talk to the API:

```
OpenAI Codex v0.147.0
model: gpt-5.6-sol
session id: <redacted>
ERROR codex_api::endpoint::responses_websocket: failed to connect to websocket:
  HTTP error: 401 Unauthorized, url: wss://api.openai.com/v1/responses
```

`observed:` With a **malformed** `auth.json`, codex reported no parse error and did not refuse. It
issued the request with **no bearer header at all**, then retried five times:

```
warning: Falling back from WebSockets to HTTPS transport. unexpected status 401 Unauthorized:
  Missing bearer or basic authentication in header
ERROR: Reconnecting... 1/5 … 5/5
ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header
```

### Evidence

- `observed:` codex resolves credentials **lazily, at request time**, not as a startup gate — an
  absent or corrupt auth store still produces a live session that fails per-request.
- `inferred:` That laziness makes codex the **best** hot-reload candidate of the four, but it is not
  proof. Nothing observed shows codex re-reading the store *after* its first resolution within a
  session; a token cached on first use would produce output identical to what I saw.
- `inferred:` On macOS codex reads the **login keychain in preference to `auth.json`**, service
  `"Codex Auth"`, account `cli|<first 16 hex of sha256(realpath(CODEX_HOME))>`
  (`src/ai-accounts/codex_accounts.py:146-167`). `ai-accounts`'s switch mirrors into it
  (`src/ai-accounts/codex_accounts.py:954-958`). Any hot-reload therefore has to re-read the *keychain*,
  not the file — a stricter requirement than re-reading a file mtime.
- `inferred:` A running codex rotates **and revokes** its refresh token during normal use
  (`src/ai-accounts/codex_accounts.py:938-947`). A live session will write its own token back over a
  rotation performed underneath it.
- `not observed:` I did not exercise the keychain write path. Doing so would have required
  `security add-generic-password` against the real `"Codex Auth"` service. Deliberately skipped.

### Auto-restart feasibility

**Feasible.** `observed:` `codex --help` exposes session resume:

```
resume    Resume a previous interactive session (picker by default; use --last to continue
fork      Fork a previous interactive session
```

Restart the `codex` process, then `codex resume --last` (or `codex resume <session-id>`) to continue
the same conversation.

### Recommended shipped behavior

`auto-restart` — after a switch, restart the codex process and resume with `codex resume --last`.

---

## claude

### What I did

`observed:` **I refused the live probe**, and the refusal is itself the finding.

`inferred:` (from source, not witnessed) Claude Code's
keychain account is `getpass.getuser()` — the **login user** — not anything derived from a path
(`src/ai-accounts/claude_accounts.py:208-215`). It is therefore **not redirectable** by `HOME`,
`CLAUDE_CONFIG_DIR`, or `CLAUDE_CREDENTIALS_JSON`. A "sandboxed" `claude` run would have read the
user's **real** credentials and hit the real API with them, and a sandboxed `claude-accounts switch`
would have **overwritten the user's live keychain item** — mid-session, for a session plausibly
running this very task. Static evidence only.

### Evidence

- `inferred:` Claude Code reads the login keychain (service `"Claude Code-credentials"`) **in
  preference to** `~/.claude/.credentials.json`, "so a switch that rewrites only the file is silently
  ignored" — `src/ai-accounts/claude_accounts.py:199-204`.
- `inferred:` `ai-accounts` states the model outright after a switch —
  `"Claude Code will use this account on its next launch."` (`src/ai-accounts/claude_accounts.py:864`).
- `inferred:` The keychain-failure warning is the same model from the other direction: *"Could not
  update the macOS keychain; Claude Code may keep using the previous account **until its next
  login**."* (`src/ai-accounts/claude_accounts.py:269-272`). Both messages encode a launch-scoped, not
  request-scoped, credential read.
- `inferred:` A running session rotates tokens; `_fold_active_into_profile` exists precisely because
  the live account drifts from its saved profile (`src/ai-accounts/claude_accounts.py:503-509`).

### Auto-restart feasibility

**Feasible, with a hard exclusion.** `observed:` `claude --help` exposes
`-c, --continue` ("Continue the most recent conversation") and resume by session ID. `observed:` A
live process on this machine is already running in exactly that shape
(`claude --resume <session-id>`), so resume-after-restart is a real, in-use path.

⚠️ `observed:` Multiple `claude` processes are running on this machine, and the auto-switch is
plausibly being driven **from inside one of them**. An auto-restart implementation MUST identify and
exclude its own session — restarting the orchestrating process would kill the switch mid-flight.
T6 should treat "never restart the current session" as a hard requirement, not a nicety.

### Recommended shipped behavior

`auto-restart` — restart and `claude --continue`, **excluding the current session's own PID/session
id**; fall back to `manual-restart` when the target session cannot be distinguished from self.

---

## agy (antigravity)

### What I did

`observed:` **I refused the live probe.**

`inferred:` (from source, not witnessed) agy's credential store is a go-keyring item with a
**fixed** service/account pair — service `"gemini"`, account `"antigravity"`
(`src/ai-accounts/gemini_accounts.py:122-123`). Neither is env-overridable, so a sandboxed run would
have read and could have overwritten the user's real keyring session.
`ANTIGRAVITY_ACCOUNT_DIR` (`src/ai-accounts/gemini_accounts.py:110`) redirects only `ai-accounts`'s own
*profile* store, **not** the live keyring the CLI actually reads. Static evidence only.

### Evidence

- `inferred:` **This is the strongest anti-hot-reload evidence in the repo.** `ai-accounts`'s own source
  records that a running Antigravity process does not merely ignore an external credential change —
  it **undoes** it:

  > `# A running Antigravity IDE re-writes the deleted keyring session instantly, so`
  > `# without a ceiling the poll loop would spin forever waiting for a login the`
  > `# CLI can't trigger on its own.`
  >
  > — `src/ai-accounts/gemini_accounts.py:127-129`

  A live process holding its session in memory and instantly re-writing the store is the exact
  opposite of hot-reload: rotation is clobbered, and clobbered *fast*.
- `inferred:` `ai-accounts` states the launch-scoped model after a switch — `"agy will use this account
  on its next launch."` (`src/ai-accounts/gemini_accounts.py:873`).
- `inferred:` A switch writes the keyring first and only then mirrors to a file
  (`src/ai-accounts/gemini_accounts.py:225-239`); the keyring is the source of truth, so the clobber
  above lands on the authoritative store.

### Auto-restart feasibility

**Split.** `observed:` The `agy` **CLI** supports resume — `agy --help` exposes `-c` / `--continue`
("Continue the most recent conversation") and `--conversation` ("Resume a previous conversation by
ID"). Restarting the CLI and resuming is feasible.

`inferred:` The **Antigravity IDE** is a GUI application with no documented CLI resume entry point,
and it is the process named in the clobber note above. It cannot be programmatically restarted with
session continuity, and while it runs it will keep re-writing the keyring. For that case the only
honest rung is `manual-restart`.

### Recommended shipped behavior

`auto-restart` for the `agy` CLI (restart + `agy --continue`); **downgrade to `manual-restart` and
warn when an Antigravity IDE process is detected**, because the IDE will overwrite the rotation.

---

## grok

### What I did

`observed:` Ran `grok -p "say hi"` inside a throwaway `GROK_HOME` containing no `auth.json`. grok is
the only one of the four with a purely file-based, fully env-redirectable store — `GROK_HOME` /
`GROK_AUTH_JSON`, no keychain (`src/ai-accounts/grok_accounts.py:71-75`) — so this probe was safe.

### What I saw

`observed:` grok refused **at startup, before any network call**:

```
Not signed in. To authenticate without a browser, run:
  grok login --device-code
Alternatively, set the XAI_API_KEY environment variable or run `grok login` on a machine with a browser.
```

### Evidence

- `observed:` grok refuses at process start, before any network call — the opposite of codex's lazy
  per-request resolution.
  `inferred:` that startup gate is a strong structural signal that the credential read is
  launch-scoped, but gating at startup does not by itself prove grok never re-reads later.
- `observed:` The probe also proves containment: grok saw the empty sandbox store, **not** the real
  `~/.grok/auth.json`, confirming `GROK_HOME` fully redirects the lookup.
- `inferred:` grok's own embedded documentation (recovered from the binary's program text, not from
  any credential file) states: *"Grok stores your credentials in `~/.grok/auth.json` … Grok refreshes
  your credentials automatically and prompts you to sign in again when they can no longer be
  renewed."* Self-managed in-process refresh implies an in-memory token that will be written back
  over an external rotation.
- `inferred:` `ai-accounts` states the launch-scoped model after a switch — `"Grok Build CLI will use
  this account on its next launch."` (`src/ai-accounts/grok_accounts.py:394`).

### Auto-restart feasibility

**Feasible, and the cleanest of the four.** `observed:` `grok --help` exposes `-c, --continue`
("Continue the most recent session for the current working directory") plus `--resume` and
`--fork-session`. `observed:` grok has no keychain dependency, so a switch is a single file write —
the least fragile restart target.

### Recommended shipped behavior

`auto-restart` — restart the grok process and resume with `grok --continue`.

**Blocked, and therefore not shipped:** auto-switching never runs for grok at all, so this rung is
currently unreachable. xAI exposes **no quota API** for Grok Build, and the whole feature triggers on
a used-percent quota reading — so `grok-accounts autoswitch` prints `autoswitch unsupported for grok:
no quota API` and exits `0` before the engine is consulted
(`src/ai-accounts/grok_accounts.py:cmd_autoswitch`). `autoswitch.PROVIDER_VERDICTS["grok"]` records this
verdict for the day that API appears and carries the same caveat in a comment; until then no code
path reads it. The blocker is a missing **quota** API — restartability itself is settled and is the
cleanest of the four (above).

---

## Safety containment

Every constraint on this spike was honoured. Specifically:

- `observed:` **No real credential path was read, written, moved, backed up, or deleted.** Not
  `~/.claude/.credentials.json`, `~/.claude.json`, `~/.codex/auth.json`, `~/.grok/auth.json`, nor
  anything under `~/.ai-accounts/`. These were touched only by `[ -e ]` existence tests; their contents
  were never opened.
- `observed:` **No keychain item was created, read, updated, or deleted.** No
  `security add-generic-password` or `delete-generic-password` was ever run — against the real
  service names or any throwaway one. The codex keychain path is consequently reported as
  `inferred`, exactly as the spike brief anticipated.
- `observed:` **Live-process guard.** `pgrep` confirmed running `codex`, `claude`, and Antigravity
  tooling against the real `$HOME` before probing, and confirmed **no** process was using the
  throwaway sandbox directories. The sandboxed codex run authenticating as *nobody* (401) is positive
  proof the probe never reached the real account.
- `observed:` **claude and agy were probed statically only**, because their credential lookups key off
  the login user / a fixed keyring service rather than any redirectable path. Refusing those two live
  probes cost certainty and was the correct trade.
- `observed:` Binary inspection read each CLI's **own program text** (embedded docs / strings) — never
  a credential store.
- `observed:` All scratch artifacts lived under a temp scratchpad and were deleted. No production
  code was written: `src/`, `tests/`, `README.md`, and `pyproject.toml` are untouched.

All identifiers in this document are redacted or placeholders (`<redacted>`, `user@example.com`).
No real email, account id, token, session id, or keychain content appears anywhere above.

## What would upgrade these verdicts to `observed`

Each provider's `inferred` verdict needs the same experiment, which requires **two** valid accounts
and an accepted risk this spike declined: start a session, rotate the credential store underneath it,
issue a second turn, and check which account the provider bills. Until then `auto-restart` is the
correct rung — it is safe when the verdict is wrong in either direction, whereas shipping `seamless`
on an unproven hot-reload silently bills the wrong account.
