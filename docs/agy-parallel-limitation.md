# Why `agy-accounts list` can't fetch usage in parallel

`codex-accounts list` and `claude-accounts list` fetch usage for every saved
profile concurrently (`fetch_parallel` in [`src/ai-accounts/_utils.py`](../src/ai-accounts/_utils.py)).
`agy-accounts list` ([`src/ai-accounts/gemini_accounts.py`](../src/ai-accounts/gemini_accounts.py))
deliberately stays sequential. This is not a missed optimization — it's a hard
constraint of how the real `agy` CLI stores its session.

## The constraint

Codex and Claude expose a plain bearer-token HTTPS endpoint: given a saved
`access_token`, any process can call the usage API directly. Antigravity/Gemini
has no such public endpoint for account usage. The only way to get live quota
numbers is to launch the real `agy` binary and query the local RPC server it
opens on `127.0.0.1` (`exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary`).

`agy` reads its session from **one shared macOS Keychain entry**
(service `gemini`, account `antigravity` — see the comment at
[`src/ai-accounts/gemini_accounts.py:56`](../src/ai-accounts/gemini_accounts.py#L56)).
To fetch account A's usage, `cmd_list` has to:

1. Write account A's credentials into that single Keychain entry
   (`_write_cli_auth_text`)
2. Launch `agy`, which reads whatever is currently in that entry at its own
   startup
3. Query its local RPC port
4. Restore the original Keychain contents (`finally: _restore_cli_auth`)

If two accounts' fetches ran at the same time, both `agy` processes would race
to read/write that same Keychain entry — whichever wins the race could load
the *wrong* account's credentials. That's not a slowdown, it's a correctness
bug: account A's usage could silently get reported under account B's row, or
a live token could get clobbered mid-rotation. So the fetch loop must stay
strictly one-account-at-a-time.

## Why "just save the token and query it directly" doesn't work either

Profiles already store the full OAuth token (access/refresh/expiry) at
`login-switch`/`save` time, so the natural follow-up question is: why not skip
`agy` entirely and call Google's API straight from the saved token, in
parallel, once per account?

Verified directly against the real endpoint (extracted from the `agy` binary
via `strings`):

```
POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary
     Authorization: Bearer <access_token from a saved profile>
→ 403 PERMISSION_DENIED  "The caller does not have permission"
```

The token itself is valid — `www.googleapis.com/oauth2/v3/tokeninfo` confirms
it's live, with the right `scope`/`aud`/`azp` — but the remote call is still
rejected. Reading the `agy` binary's proto descriptors shows why: the local RPC
`agy` exposes is a **proxy**. Between the local `LanguageServerService` call
and the remote `PredictionService`, `agy` injects an `ApiKeyConfig` baked into
the binary plus a project/resource ID it resolves internally. A bare user
access token isn't sufficient — the request also needs `agy`'s own client
credentials and project context, which live inside the binary, not in the
saved profile JSON. Reverse-engineering and replaying that internal API key
would be fragile (breaks on every `agy` update) and outside the intent of a
public API, so it's not something this tool does.

## Bottom line

- **Codex / Claude**: token → HTTPS call → parallel-safe. Already parallelized.
- **Gemini / Antigravity**: must "become" one account at a time in the single
  real Keychain slot `agy` reads from. Parallelizing would risk cross-account
  credential/usage mixups, not just save time.

## What was done instead

Since real parallelism is unsafe, the fetch was optimized within the
sequential constraint:

- The readiness poll interval in `gemini_usage.fetch_usage` dropped from
  `0.25s` to `0.1s` (poll cost is ~0 — `lsof` + a local connection attempt —
  so a tighter interval trims pure detection latency).
- Measured per-account floor: ~3.2s from process launch to the RPC answering,
  which is `agy`'s own boot-to-ready warmup — not tunable from outside the
  binary.
- Spinner text was unified across all three account tools
  (`Fetching <provider> usage… (i/N) <profile>`, profile name in magenta) so
  the sequential Gemini fetch and the parallel Codex/Claude fetches present
  the same UX.
