# salt-pepper — A modern CLI front-end for salt-api

A modernised fork of [saltstack/pepper](https://github.com/saltstack/pepper) —
the original was a well-crafted tool that served the Salt community for years,
but its last meaningful update was around 2018. This rewrite brings it into
the modern Python landscape while keeping the same familiar workflow.

Pepper provides a Python library (`pepperlib`) and CLI script for accessing a
remote `salt-api` instance without needing Salt installed locally.

## Quick Start

### Install from release

```bash
# Via pip from release artifact (no git needed)
pip install https://forge.thatserver.ca/agreengrass/salt-pepper/releases/download/v4.8.0/salt_pepper-4.8.0-py3-none-any.whl

# Or via git
pip install git+ssh://git@forge.thatserver.ca:8222/agreengrass/salt-pepper.git@v4.8.0

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

The original pepper (`saltstack/pepper`) was built for an earlier era of Python
and Salt. It served well, but the Python and Salt ecosystems have moved on:

- Python 2 is long retired — the compat shims (`urllib2` fallback, `ConfigParser`,
  `raw_input` polyfills) add complexity without benefit
- `optparse` was the right choice in 2014, but `click` is now the standard for
  CLI tooling
- The dual HTTP backend (`urllib` + `requests`) means error handling is
  duplicated across two code paths
- CI was configured for Travis CI and Azure Pipelines, both of which no longer
  serve this project
- Type hints, `pyproject.toml`, and modern packaging conventions make
  maintenance and contribution easier
- Salt itself has added new API features (queue client, metadata, JWT auth,
  `tgt_type` aliasing) since the original was last updated

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

### Auth internals (how salt-api authentication works)

Salt's `rest_cherrypy` netapi module itself does not handle authentication —
it delegates to Salt's auth modules (PAM, `auto`, etc.). After a successful
login at ``/login``, the server returns an auth token and sets a
``session_id`` cookie.

The token can be presented on subsequent requests in two ways, both handled
by Salt's auth layer (not by CherryPy itself):

1. **``X-Auth-Token`` header** — checked first and takes priority
   (see `app.py#L863-L872
   <https://github.com/saltstack/salt/blob/621251a7376f114e8a67eea323c637a94cefc917/salt/netapi/rest_cherrypy/app.py#L863-L872>`_)
2. **``session_id`` cookie** — fallback if no ``X-Auth-Token`` is present

The original pepper extracted the token from the login response but sent it
as a URL or form parameter rather than as an ``X-Auth-Token`` header, so
the auth layer never saw it and returned 401.

**v4.0.0+ fix**: ``requests.Session()`` automatically stores the
``session_id`` cookie from the ``/login`` response and sends it on every
follow-up request. The token is still available in the response if you need
to use ``X-Auth-Token`` directly (e.g. for scripting).

For `rest_tornado` (JWT mode), `X-Auth-Token` is still used — that backend follows a
different protocol.

### SSH bastion / jumphost (optional)

If the salt-api is behind a bastion host, pepper can tunnel through it
transparently. The bastion is **optional** — omit it to connect directly.

Set the bastion in your config file:

```yaml
main:
  saltapi_url: https://salt-master.internal:8080
  saltapi_user: admin
  saltapi_pass: hunter2
  saltapi_eauth: pam
  # SSL — set to true to skip verification (self-signed certs)
  ignore_ssl_errors: false
  # SSH bastion — omit or set to null for direct connection
  ssh_bastion: user@bastion.example.com
  ssh_key: /home/user/.ssh/bastion_key   # optional, path to identity file
  ssh_port: 22                            # optional, default 22
```

Or via environment variables:

```bash
# SSL — set to "true" to skip verification
export PEPPER_IGNORE_SSL_ERRORS=true
# Optional — omit these for a direct connection
export PEPPER_SSH_BASTION=user@bastion.example.com
export PEPPER_SSH_KEY=~/.ssh/bastion_key
export PEPPER_SSH_PORT=2222
pepper '*' test.ping
```

**Setup wizard** (`pepper setup init`) — if a config file already exists
with profiles, shows them with masked secrets and lets you select one to
edit, add a new one, or start fresh. Use ``--profile`` to target a
specific profile directly (creates it if missing, edits it if present).
When editing, blank input keeps the previous value.

The wizard asks whether to verify SSL certificates (default yes). Answer no
for self-signed certs. It also asks about SSH bastion support. When prompted
for an SSH key path, you can enter a **directory** (e.g. ``~/.ssh``) to
list its contents and then type the filename.

**Verify** (`pepper setup verify`) displays bastion settings if configured
and tests the tunnel as part of Step 1/3.

### How the tunnel works

Pepper spawns ``ssh -L`` in the background, forwarding a random local port
to the salt-api's host:port through the bastion:

```
ssh -N -L 127.0.0.1:PORT:REMOTE_HOST:REMOTE_PORT \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    [-i KEY] [-p PORT] user@bastion
```

The tunnel is automatically torn down when the command completes (via
``Pepper.close()`` / ``__del__``). The SSH connection encrypts traffic
end-to-end, so pepper preserves the original URL scheme (``https``
traffic gets a TLS handshake through the tunnel, ``http`` goes plain).

**Error handling**: If the SSH binary is missing, the host is unreachable,
or authentication fails, pepper raises a clear ``PepperException`` with the
SSH stderr output — no silent hangs.

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
| Unit test suite (mocked HTTP, 174 tests) | ✅ |
| Forgejo Actions CI | ✅ |

## Changelog

### v4.8.0 — 2026-09-07

- **Fixed**: `pepper setup init -p` flag now properly selects the target profile
  (Click's inline dispatch was consuming `-p` at the parent level)
- **Fixed**: `pepper setup verify --profile` works reliably (same dispatch fix)
- **Improved**: Salt API URL prompt now shows format example
  (`https://hostname:port`)
- **Improved**: EAuth backend prompt now lists known options
  (`pam, auto, kerberos, ldap, etc.`)
- **Fixed**: SSH bastion tunnel preserves the original URL scheme — HTTPS
  traffic through the tunnel now gets a proper TLS handshake instead of
  being sent plain (fixes `RemoteDisconnected` errors when the remote
  salt-api expects HTTPS)

### v4.7.0 — Previous release

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

The config file supports multiple named **profiles**, each containing connection
settings for a different Salt master or backend. Profiles are selected with the
`--profile` / `-p` flag or the `PEPPERPROFILE` environment variable.

##### CherryPy backend (standard salt-api)

```yaml
# ~/.pepperrc.yml — CherryPy backend on port 8080
# Profile name: 'main' (default when no --profile is given)
main:
  saltapi_url: https://salt.example.com:8080
  saltapi_user: admin
  saltapi_pass: your_password_here
  saltapi_eauth: pam
  backend: cherrypy
  # Optional: pre-authenticate with a session token
  # (pepper will re-auth automatically if this expires)
  # jwt_token: eyJ...

# Named profile for a different master
production:
  saltapi_url: https://salt-prod.internal:8080
  saltapi_user: deploy-bot
  saltapi_pass: s3cr3t!
  saltapi_eauth: pam
  backend: cherrypy
```

Usage:
```bash
# Uses 'main' profile by default
pepper '*' test.ping

# Select a named profile
pepper -p production '*' test.ping

# Environment override — PEPPERPROFILE=production
```

##### Alcali backend (Django REST API / JWT)

```yaml
# ~/.pepperrc.yml — Alcali backend on port 8443
# Connects to the Alcali Django app which proxies to salt-api
alcali-dev:
  saltapi_url: https://salt.example.com:8443
  saltapi_user: admin
  saltapi_pass: alcali_password
  saltapi_eauth: pam
  backend: alcali

# Alcali with a pre-obtained JWT token (no login needed)
alcali-token:
  saltapi_url: https://salt.example.com:8443
  backend: alcali
  jwt_token: eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...
```

Usage:
```bash
# Alcali with password login
pepper -p alcali-dev --non-interactive '*' test.ping

# Alcali with pre-set JWT token
pepper -p alcali-token '*' test.ping
```

##### Mixed backends, one config file

```yaml
# Profile 'main' — cherrypy (default)
main:
  saltapi_url: https://salt.example.com:8080
  saltapi_user: devops
  saltapi_pass: hunter2
  saltapi_eauth: pam
  backend: cherrypy

# Profile 'prod' — different master, cherrypy
prod:
  saltapi_url: https://salt-prod.internal.lan:8080
  saltapi_user: deploy-bot
  saltapi_pass: prod_s3cr3t
  saltapi_eauth: pam
  backend: cherrypy

# Profile 'alcali' — Django REST API with JWT
alcali:
  saltapi_url: https://salt.example.com:8443
  saltapi_user: admin
  saltapi_pass: alcali_pw
  saltapi_eauth: pam
  backend: alcali

# Profile 'alcali-token' — no password, just a JWT
alcali-token:
  saltapi_url: https://salt.example.com:8443
  backend: alcali
  jwt_token: eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...

# Profile 'legacy' — old INI-style still supported
```

Switch between them:
```bash
# Default profile (cherrypy)
pepper '*' test.ping

# Production (different master)
pepper -p prod '*' test.ping

# Alcali Django API (JWT login)
pepper -p alcali '*' test.ping

# Alcali with pre-set token (no login)
pepper -p alcali-token '*' test.ping
```

##### Environment variable reference

| Variable | Maps to | Example |
|----------|---------|--------|
| `SALTAPI_URL` | `saltapi_url` | `https://salt:8080` |
| `SALTAPI_USER` | `saltapi_user` | `admin` |
| `SALTAPI_PASS` | `saltapi_pass` | `hunter2` |
| `SALTAPI_EAUTH` | `saltapi_eauth` | `pam` |
| `PEPPER_BACKEND` | `backend` | `cherrypy` or `alcali` |
| `PEPPERPROFILE` | — (selects profile) | `prod` |

##### Config file location and format

Pepper looks for config in this order (first found wins):

1. `./.pepperrc.yml` — project-local config (great for team repos, no passwords)
2. `~/.pepperrc.yml` — user-wide config (default)
3. `~/.pepperrc` — legacy INI format (still supported)

**Security**: Protect your config file with `chmod 600 ~/.pepperrc.yml`.
Never commit config files containing real passwords to version control.
For CI/CD use environment variables instead of config files.

##### Legacy INI format (backward compatible)

```ini
[main]
saltapi_url = https://salt.example.com:8080
saltapi_user = admin
saltapi_pass = hunter2
saltapi_eauth = pam

[production]
saltapi_url = https://salt-prod.internal:8080
saltapi_user = deploy-bot
saltapi_pass = s3cr3t!
saltapi_eauth = pam
```

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

#### 6. JWT tokens (`--jwt`)

```bash
pepper --jwt 'eyJhbGciOi...' '*' test.ping
```

JWT is supported by Salt's `rest_tornado` module and also by the Alcali
Django backend. The token is sent as `X-Auth-Token` or `Authorization: Bearer`
depending on the backend. No separate `/login` call is made.

### What NOT to do

- Do **not** put passwords in `.bash_history` (use `$HISTIGNORE`)
- Do **not** pass passwords in CLI flags in shared/multi-user environments
  (they are visible in `/proc/pid/cmdline`)
- Do **not** commit config files with passwords to git

## Related

- [`salt-api`](https://github.com/saltstack/salt/tree/master/salt/netapi) — Salt's REST API
- [`rest_cherrypy`](https://docs.saltproject.io/en/latest/topics/netapi/rest_cherrypy.html) — CherryPy-based netapi module
- [`rest_tornado`](https://docs.saltproject.io/en/latest/topics/netapi/rest_tornado.html) — Tornado-based netapi module (SSE events, JWT)
