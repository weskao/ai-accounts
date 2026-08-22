# ai-accounts

Manage, inspect, refresh, and switch saved profiles across AI coding CLIs from
one dependency-free Python package.

`ai-accounts` is the all-provider command. The package also installs focused
commands for Codex, Claude Code, Antigravity, Grok Build, and Mistral Vibe.

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/)
- The provider CLI for each account manager you use

The Python package has no runtime dependencies. It supports macOS, Linux, and
Windows; credential-store integration follows the native platform.

## Install

Install the first standalone release:

```sh
uv tool install --from git+https://github.com/weskao/ai-accounts.git@v0.1.0 ai-accounts
```

Or install the latest `main`:

```sh
uv tool install --from git+https://github.com/weskao/ai-accounts.git ai-accounts
```

Upgrade or uninstall:

```sh
uv tool upgrade ai-accounts
uv tool uninstall ai-accounts
```

## Commands

| Command | Purpose |
| --- | --- |
| `ai-accounts` | Run one operation across every provider |
| `codex-accounts` | Manage Codex CLI profiles and ChatGPT OAuth usage |
| `claude-accounts` | Manage Claude Code profiles and quota usage |
| `agy-accounts` | Manage Antigravity profiles and quota usage |
| `grok-accounts` | Manage Grok Build OAuth profiles |
| `vibe-accounts` | Manage Mistral Vibe API-key profiles |

Every provider command supports the common profile workflow:

```sh
codex-accounts who
codex-accounts save work
codex-accounts list
codex-accounts switch work
codex-accounts refresh --all
codex-accounts remove work
codex-accounts --help
```

Use the umbrella command to run the same action for all providers:

```sh
ai-accounts list
ai-accounts who
ai-accounts usage
ai-accounts refresh --all
ai-accounts sync
ai-accounts --help
```

`list` fetches providers concurrently. Interactive actions run providers one at
a time so their prompts remain usable.

## Profile storage

Saved profiles and shared settings live under `~/.ai-accounts`:

```text
~/.ai-accounts/
├── config.json
├── autoswitch-state.json
├── codex/accounts/
├── claude/accounts/
├── antigravity/accounts/
├── grok/accounts/
└── vibe/accounts/
```

Profile JSON files contain live credentials. Do not commit, publish, or share
this directory. Writes use owner-only permissions and atomic replacement where
the provider format allows it.

Provider-native legacy stores such as `~/.codex/accounts` and
`~/.claude/accounts` are moved into the central directory on first use. Override
paths with `CODEX_ACCOUNT_DIR`, `CLAUDE_ACCOUNT_DIR`,
`ANTIGRAVITY_ACCOUNT_DIR`, `GROK_ACCOUNT_DIR`, or `VIBE_ACCOUNT_DIR`. Override
the shared config with `AI_ACCOUNTS_CONFIG_JSON`.

## Auto-switch

Auto-switch can refresh quota data, select another saved profile when the active
profile crosses a configured threshold, notify you, and restart supported
interactive sessions.

```sh
ai-accounts config
ai-accounts config get
ai-accounts config set enabled true
ai-accounts autoswitch
ai-accounts autoswitch setup
ai-accounts timer-status
```

`ai-accounts autoswitch setup` installs provider event hooks plus a low-frequency
OS timer fallback. Re-run it after reinstalling the package so hooks point at
the current Python environment.

Remove only the timer with:

```sh
ai-accounts uninstall-timer
```

The interactive config menu documents each setting. CLI config reads mask the
Telegram bot token.

## Platform notes

| Provider | Credential source | Notes |
| --- | --- | --- |
| Codex | `~/.codex/auth.json` and the native credential store | `codex` is required for login flows |
| Claude Code | `~/.claude/.credentials.json` and macOS Keychain when used | `claude` is required for login flows |
| Antigravity | macOS Keychain, Windows Credential Manager, or Linux Secret Service | Linux needs `secret-tool` from libsecret |
| Grok Build | `$GROK_HOME/auth.json` | Quota switching is skipped when no quota API is available |
| Mistral Vibe | macOS Keychain or `$VIBE_HOME/.env` | `vibe` is required for login flows |

Run a provider command with `--help` for its exact files, environment overrides,
and authentication behavior.

## Development

```sh
git clone https://github.com/weskao/ai-accounts.git
cd ai-accounts
uv sync --locked
uv run pytest
uv run ruff check .
uv build
```

Install the checkout globally while developing:

```sh
uv tool install --editable .
```

## License

[MIT](LICENSE)
