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
- [ ] `queue` client support
- [ ] `ssh` client method in library
- [ ] `tgt_type` alias + `metadata` parameter
- [ ] JWT auth support
- [ ] Async library interface
- [ ] Output formatting without Salt (native `--out=json|yaml|table`)
- [ ] `pepper job` subcommand (job status, log, kill)

### Phase 3 — Polish
- [ ] Subcommand structure (`run`, `key`, `event`, `job`)
- [ ] Config file in YAML/TOML instead of INI
- [ ] Completions (shell auto-complete)
- [ ] Rich error output with context
- [ ] Event streaming CLI

## Related

- [`salt-api`](https://github.com/saltstack/salt/tree/master/salt/netapi) — Salt's REST API
- [`rest_cherrypy`](https://docs.saltproject.io/en/latest/topics/netapi/rest_cherrypy.html) — CherryPy-based netapi module
- [`rest_tornado`](https://docs.saltproject.io/en/latest/topics/netapi/rest_tornado.html) — Tornado-based netapi module (SSE events, JWT)
