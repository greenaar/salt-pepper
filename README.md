# salt-pepper — A modern CLI front-end for salt-api

A rewrite of [saltstack/pepper](https://github.com/saltstack/pepper), which
hasn't been updated in **8 years** (last meaningful commit ~2018).

Pepper provides a Python library (`pepperlib`) and CLI script for accessing a
remote `salt-api` instance without needing Salt installed locally.

## The Problem

The original pepper (`saltstack/pepper`) is frozen in time:

- **Python 2 compat deadweight** — still has `urllib2` fallback, `ConfigParser` compat shims, `raw_input`/`input` polyfills
- **Deprecated `optparse`** — instead of `argparse` or `click`
- **Mixed HTTP stack** — uses `urllib` for main calls, `requests` only for streaming/kerberos; duplicates error handling across two backends
- **Python 2.5–3.6 classifiers** — ancient
- **Travis CI / Azure Pipelines** — both dead CI platforms
- **No pyproject.toml** — `setup.py` only, no modern packaging
- **No type hints anywhere**
- **No async support** — everything synchronous
- **Salt 3000+ API features** — missing support for clients and parameters added since 2018

## What the Original Pepper Supports

| Client | Method | Notes |
|--------|--------|-------|
| `local` | `Pepper.local()` | ✅ Minion execution |
| `local_async` | `Pepper.local_async()` | ✅ With polling |
| `local_batch` | `Pepper.local_batch()` | ✅ Batch execution |
| `runner` | `Pepper.runner()` | ✅ Runner functions |
| `wheel` | `Pepper.wheel()` | ✅ Wheel functions |
| `ssh` | CLI only | ❌ No `Pepper.ssh()` method |
| `queue` | — | ❌ Not implemented |

| Feature | Status |
|---------|--------|
| Auth: PAM, `auto`, kerberos | ✅ |
| Token-based auth (`/token` + `/run`) | ✅ |
| Config: `.pepperrc`, env vars, CLI flags | ✅ |
| Targeting: glob, pcre, list, grain, grain_pcre, pillar, pillar_pcre, range, compound, nodegroup | ✅ |
| `--json` / `--json-file` input | ✅ |
| Retcode validation (`--fail-any`, `--fail-all`, etc.) | ✅ |
| Outputters (via salt module) | ✅ (requires `salt` installed) |

## What's Missing (New Features Needed)

### CLI & Library

1. **`queue` client** — Salt has a `queue` runner that pepper can't drive
2. **`ssh` method** — CLI parses SSH commands but library has no `Pepper.ssh()`
3. **Proper `argparse` or `click`** — replace deprecated `optparse`
4. **Strip Python 2 compat** — Python 2 is EOL, remove all shims
5. **Single HTTP backend** — use `requests` exclusively, drop `urllib2`
6. **Async** — `async` / `await` via `httpx` or `aiohttp` for the library; optional parallel execution polling
7. **Type hints everywhere** — full `pyproject.toml` with mypy, ruff
8. **`tgt_type` alias** — Salt added `tgt_type` as the successor to `expr_form`; support both
9. **Metadata parameter** — `metadata` dict in lowstate calls
10. **JID subscription** — `--client local --async` with proper job tracking (Salt modern versions)
11. **`full_return` toggle** — baked into modern Salt-api, don't force it
12. **Modern packaging** — `pyproject.toml`, hatchling/setuptools, remove `setup.py`
13. **Better error messages** — current errors are terse and low-context
14. **Output formatting without Salt** — clean `--out=json|yaml|table` without requiring `salt` at all

### Auth

15. **JWT auth support** — Salt 3006+ supports JWT tokens via `rest_tornado`
16. **OAuth2 / OpenID Connect** — for enterprise deployments

### Operations

17. **CI via Forgejo Actions** — working CI pipeline instead of Travis/Azure
18. **Unit tests with mocked HTTP** — current tests are integration-only
19. **Timeout per-command** — work properly with all client types (currently only local and runner)
20. **Configurable output file per-run** — extend `--output-file`
21. **Subcommands** — `pepper run`, `pepper key`, `pepper event`, `pepper job` instead of only `--client`

## Project Structure

```
salt-pepper/
├── src/
│   └── salt_pepper/          # (renamed from pepper/)
│       ├── __init__.py        # version, public API
│       ├── client.py          # Pepper class — HTTP client to salt-api
│       ├── cli.py             # CLI entry points (click groups)
│       ├── auth.py            # Auth providers (PAM, JWT, kerberos, etc.)
│       ├── config.py          # Config loader (YAML or env)
│       ├── exceptions.py      # Custom exceptions
│       ├── output.py          # Output formatting (JSON, YAML, table, nested)
│       └── types.py           # Typed dicts for lowstate payloads
├── tests/
│   ├── unit/                  # Mocked HTTP tests
│   └── integration/           # Against a real/nearby salt-api
├── pyproject.toml
├── .forgejo/
│   └── workflows/
│       └── ci.yml
└── README.md
```

## Development

```bash
# Set up dev environment
cd salt-pepper
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check src/

# Type-check
mypy src/
```

## Secrets & Authentication

salt-pepper supports multiple authentication methods, each with different
security characteristics. Here is how they work and how to keep credentials
safe.

### Authentication Order of Precedence

Secrets are resolved in this order (highest priority first):

1. **CLI flags** (`--username`, `--password`, `--jwt`)
2. **Environment variables** (`SALTAPI_USER`, `SALTAPI_PASS`, `SALTAPI_URL`, `SALTAPI_EAUTH`)
3. **Config file** (`~/.pepperrc.yml` or `~/.pepperrc`)

### Methods

#### 1. Interactive prompt (default)

When no credentials are given, pepper prompts for username and password:

```bash
pepper '*' test.ping
Username: <type user>
Password: <type password, hidden>
```

This is the safest for interactive use — credentials never touch disk,
terminal history, or process lists.

#### 2. Environment variables (non-interactive / CI)

```bash
export SALTAPI_URL=https://salt.example.com:8000
export SALTAPI_USER=deploybot
export SALTAPI_PASS=s8CR3t!
export SALTAPI_EAUTH=pam

pepper --non-interactive '*' test.ping
```

**Security**: Environment variables are visible in `/proc/self/environ`
to the owning user and root. Good for containers and CI runners where
the environment is scoped to a single process tree.

#### 3. YAML config file (`~/.pepperrc.yml`)

```yaml
main:
  saltapi_url: https://salt.example.com:8000
  saltapi_user: admin
  saltapi_eauth: pam

prod:
  saltapi_url: https://salt-prod.example.com:8000
  saltapi_user: deploy
  saltapi_eauth: auto
```

Use with:
```bash
pepper -p prod '*' test.ping
```

**Security**: The config file is a plaintext file in your home directory.
Protect it with `chmod 600 ~/.pepperrc.yml`. Do not commit it to version
control. Never store production passwords in config files — use
environment variables or the interactive prompt instead.

Config files are optional and backward-compatible — old `.pepperrc` INI
files still work.

#### 4. Token caching (`--make-token`)

```bash
pepper -T '*' test.ping     # login once, token cached to ~/.peppercache
pepper '*' test.ping         # reuses cached token
```

The cached token is stored in `~/.peppercache` with `0600` permissions.
Tokens expire based on Salt master settings; pepper automatically
refreshes them.

#### 5. JWT tokens (`--jwt`)

```bash
pepper --jwt 'eyJhbGciOi...' '*' test.ping
```

JWT is supported by Salt's `rest_tornado` module. The token is sent as
`X-Auth-Token` on every request. No separate `/login` call is made.

### What NOT to do

- Do **not** put passwords in `.bash_history` (use `$HISTIGNORE`)
- Do **not** pass passwords in CLI flags in shared/multi-user environments
  (they are visible in `/proc/pid/cmdline`)
- Do **not** commit config files with passwords to git

## Roadmap

### Phase 1 — Modernize (minimal diffs)
- [ ] `pyproject.toml` + modern packaging
- [ ] Strip Python 2 compat
- [ ] Replace `optparse` with `click`
- [ ] Replace `urllib2` with `requests` throughout
- [ ] Add type hints
- [ ] Ruff lint + mypy type checking
- [ ] Forgejo Actions CI
- [ ] Unit test suite with mocked HTTP

### Phase 2 — New Features
- [x] `queue` client support
- [x] `ssh` client method in library
- [x] `tgt_type` alias + `metadata` parameter
- [x] JWT auth support
- [x] Async library interface (httpx-based `AsyncPepper`)
- [x] Output formatting without Salt (native `--out=json|yaml|table`)
- [x] `pepper job` subcommand (job status, log, kill)

### Phase 3 — Polish
- [x] Subcommand structure (`run`, `key`, `event`, `job`)
- [x] Config file in YAML/TOML instead of INI
- [x] Completions (shell auto-complete)
- [x] Rich error output with context
- [x] Event streaming CLI

## Related

- [`salt-api`](https://github.com/saltstack/salt/tree/master/salt/netapi) — Salt's REST API
- [`rest_cherrypy`](https://docs.saltproject.io/en/latest/topics/netapi/rest_cherrypy.html) — CherryPy-based netapi module
- [`rest_tornado`](https://docs.saltproject.io/en/latest/topics/netapi/rest_tornado.html) — Tornado-based netapi module (SSE events, JWT)
