# salt-pepper — A modern CLI front-end for salt-api

A rewrite of [saltstack/pepper](https://github.com/saltstack/pepper), which
hasn't been updated in **8 years** (last meaningful commit ~2018).

Pepper provides a Python library (`pepperlib`) and CLI script for accessing a
remote `salt-api` instance without needing Salt installed locally.

## Quick Start

### Install from release

```bash
# Via pip from release artifact (no git needed)
pip install https://forge.thatserver.ca/agreengrass/salt-pepper/releases/download/v4.0.0/salt_pepper-4.0.0-py3-none-any.whl

# Or via git
pip install git+ssh://git@forge.thatserver.ca:8222/agreengrass/salt-pepper.git@v4.0.0

# Or from source
cd salt-pepper
pip install .
```

### First use

```bash
# Interactive — pepper prompts for username/password
pepper '*' test.ping

# Non-interactive via env vars
export SALTAPI_URL=https://salt.example.com:8080
export SALTAPI_USER=admin
export SALTAPI_PASS=hunter2
export SALTAPI_EAUTH=pam
pepper --non-interactive '*' test.ping
```

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

## Backends

salt-pepper supports multiple salt-api backends, selectable via `--backend` or config.

| Backend | Port | Auth Method | Notes |
|---------|------|-------------|-------|
| `cherrypy` (default) | 8080 | Session cookie (PAM login) | Standard Salt rest_cherrypy |
| `alcali` | 8443 | JWT Bearer token | Django REST API frontend |

```bash
# CherryPy backend (default)
pepper '*' test.ping

# Alcali backend
pepper --backend alcali '*' test.ping

# Set in config file (~/.pepperrc.yml)
# main:
#   backend: alcali
#   saltapi_url: https://salt.example.com:8443
```

### Auth internals (why v4.0.0 matters)

Salt's `rest_cherrypy` authenticates via **session cookies**, not `X-Auth-Token` headers.
The original pepper extracted the token from login and sent it as `X-Auth-Token` on every
subsequent request — but CherryPy ignores that header and returns 401.

**v4.0.0 fix**: `requests.Session()` automatically stores the `session_id` cookie from
the `/login` response and sends it on every follow-up request. No manual token
extraction needed. This is transparent — just authenticate normally.

For `rest_tornado` (JWT mode), `X-Auth-Token` is still used — that backend follows a
different protocol.

## Clients & Features

| Client | Method | Notes |
|--------|--------|-------|
| `local` | `Pepper.local()` | ✅ Minion execution |
| `local_async` | `Pepper.local_async()` | ✅ With polling |
| `local_batch` | `Pepper.local_batch()` | ✅ Batch execution |
| `runner` | `Pepper.runner()` | ✅ Runner functions |
| `wheel` | `Pepper.wheel()` | ✅ Wheel functions |
| `ssh` | `Pepper.ssh()` | ✅ SSH client method |
| `queue` | `Pepper.queue()` | ✅ Queue client |

| Feature | Status |
|---------|--------|
| Auth: PAM, auto, kerberos, JWT | ✅ |
| Backends: cherrypy (session cookie), alcali (JWT Bearer) | ✅ |
| Config: YAML profiles, INI, env vars, CLI flags | ✅ |
| Profile selection (`--profile`, `PEPPERPROFILE`) | ✅ |
| Targeting: glob, pcre, list, grain, grain_pcre, pillar, pillar_pcre, range, compound, nodegroup | ✅ |
| `--json` / `--json-file` input | ✅ |
| Retcode validation (`--fail-any`, `--fail-all`, etc.) | ✅ |
| Output formatting without Salt (JSON, YAML, table, nested) | ✅ |
| Subcommands (`run`, `key`, `event`, `job`) | ✅ |
| Event streaming CLI | ✅ |
| Shell completions | ✅ |
| Rich error output with context | ✅ |
| Async library interface (`AsyncPepper` via httpx) | ✅ |
| Type hints (mypy checked) | ✅ |
| Ruff linting | ✅ |
| Unit test suite (mocked HTTP) | ✅ |
| Forgejo Actions CI | ✅ |

## Roadmap — Future

- Release automation (auto-build on tag push)
- OpenID Connect / OAuth2 auth providers
- `full_return` toggle baked into library calls
- Timeout per-command for all client types

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

1. **CLI flags** (`--username`, `--password`, `--jwt`, `--backend`)
2. **Environment variables** (`SALTAPI_USER`, `SALTAPI_PASS`, `SALTAPI_URL`,
   `SALTAPI_EAUTH`, `PEPPER_BACKEND`)
3. **Config file** (`~/.pepperrc.yml` or `~/.pepperrc`, selected by `PEPPERPROFILE`)

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
# Default profile (cherrypy backend)
main:
  saltapi_url: https://salt.example.com:8080
  saltapi_user: admin
  saltapi_eauth: pam
  backend: cherrypy

# Prod profile with Alcali backend
prod:
  saltapi_url: https://salt-prod.example.com:8443
  saltapi_user: deploy
  saltapi_eauth: auto
  backend: alcali
  # Pre-set JWT token (avoid storing this in shared configs)
  jwt_token: eyJhbGciOi...

# Legacy INI-style config still works
```

Use with profile selection:
```bash
pepper -p prod '*' test.ping
# Or via env var
PEPPERPROFILE=prod pepper '*' test.ping
```

**Security**: The config file is a plaintext file in your home directory.
Protect it with `chmod 600 ~/.pepperrc.yml`. Do not commit it to version
control. Never store production passwords in config files — use
environment variables or the interactive prompt instead.

Config files are optional and backward-compatible — old `.pepperrc` INI
files still work.

#### 4. Profile-based config with environment override

Set a default profile in your shell init:

```bash
export PEPPERPROFILE=prod
export PEPPER_BACKEND=alcali
pepper '*' test.ping
```

CLI `--profile` flag takes precedence over `PEPPERPROFILE`.

#### 5. Token caching (`--make-token`)

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
