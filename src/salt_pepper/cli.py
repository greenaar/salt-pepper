"""
A CLI interface to a remote salt-api instance.

Usage::

    pepper 'minion-*' test.ping                          # inline mode
    pepper run 'minion-*' test.ping                      # explicit run
    pepper run --client runner jobs.list_jobs
    pepper key list                                       # manage minion keys
    pepper key accept myminion
    pepper job status 2025090512000012345                # job management
    pepper job log 2025090512000012345
    pepper event                                          # stream events
    pepper completions bash                               # shell completions
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import sys
import time
from configparser import ConfigParser
from typing import Any

import click

from salt_pepper import Pepper
from salt_pepper.exceptions import (
    PepperArgumentsException,
    PepperAuthException,
    PepperException,
)
from salt_pepper.output import format_result
from salt_pepper.retcode import PepperRetcode

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# Config loading — YAML first, INI fallback
# ═══════════════════════════════════════════════════════════════════════


def _find_config(config_path: str | None) -> tuple[str, str]:
    """
    Locate and return (config_path, format).

    * If *config_path* is given explicitly, use it as-is.
    * Otherwise look for ``~/.pepperrc.yml``, ``~/.pepperrc.yaml``,
      then fall back to ``~/.pepperrc`` (INI).
    """
    if config_path:
        fmt = "yaml" if config_path.endswith((".yml", ".yaml")) else "ini"
        return config_path, fmt

    # Default search
    home = os.path.expanduser("~")
    for path in (
        os.environ.get("PEPPERRC"),
        os.path.join(home, ".pepperrc.yml"),
        os.path.join(home, ".pepperrc.yaml"),
        os.path.join(home, ".pepperrc"),
    ):
        if path and os.path.exists(path):
            fmt = "yaml" if path.endswith((".yml", ".yaml")) else "ini"
            return path, fmt

    return "", ""


def _config_exists() -> bool:
    """Check whether any pepper config file exists."""
    path, _ = _find_config(None)
    return bool(path) and os.path.exists(path)


def _maybe_migrate_config() -> None:
    """Check for old ``~/.pepperrc`` INI config and offer migration.

    If the old INI file exists and no YAML config exists yet, ask
    the user if they want to convert to YAML format.
    """
    home = os.path.expanduser("~")
    old_ini = os.path.join(home, ".pepperrc")
    new_yml = os.path.join(home, ".pepperrc.yml")

    if not os.path.exists(old_ini) or os.path.exists(new_yml):
        return

    click.echo()
    click.secho(
        "╔══════════════════════════════════════════════════╗",
        bold=True,
    )
    click.secho(
        "║  Old-style config detected: ~/.pepperrc         ║",
        bold=True,
    )
    click.secho(
        "║  New YAML format supports profiles, backends,   ║",
        bold=True,
    )
    click.secho(
        "║  SSH bastion tunnels, and SSL configuration.    ║",
        bold=True,
    )
    click.secho(
        "╚══════════════════════════════════════════════════╝",
        bold=True,
    )
    click.echo()

    if not click.confirm("Migrate to YAML format?", default=True):
        click.echo("Using old format. Run 'pepper setup init' later to upgrade.")
        return

    import shutil
    import yaml

    # Read old INI config
    config = ConfigParser(interpolation=None)
    config.read(old_ini)
    yaml_data: dict[str, dict[str, Any]] = {}
    for section in config.sections():
        profile: dict[str, Any] = {}
        for key, value in config.items(section):
            profile[key.lower()] = value
        yaml_data[section] = profile

    try:
        with open(new_yml, "w") as fh:
            yaml.dump(yaml_data, fh, default_flow_style=False)
        os.chmod(new_yml, 0o600)

        # Only remove old file after new one is written successfully
        os.remove(old_ini)

        click.secho(f"✓ Migrated to {new_yml}", fg="green")
        click.secho(f"  Old config ({old_ini}) removed.", fg="green")
    except OSError as exc:
        click.secho(f"✗ Migration failed: {exc}", fg="red", err=True)


def _load_profile_yaml(config_path: str, profile: str) -> dict[str, str | None]:
    """Load a profile from a YAML config file."""
    try:
        import yaml
    except ImportError:
        return {}

    with open(config_path) as fh:
        data = yaml.safe_load(fh) or {}

    profile_data = data.get(profile, {})
    results: dict[str, str | None] = {
        "SALTAPI_URL": profile_data.get("saltapi_url"),
        "SALTAPI_USER": profile_data.get("saltapi_user"),
        "SALTAPI_PASS": profile_data.get("saltapi_pass"),
        "SALTAPI_EAUTH": profile_data.get("saltapi_eauth", "auto"),
        "BACKEND": profile_data.get("backend", "cherrypy"),
        "SSH_BASTION": profile_data.get("ssh_bastion"),
        "SSH_KEY": profile_data.get("ssh_key"),
        "SSH_PORT": profile_data.get("ssh_port", "22"),
        "IGNORE_SSL_ERRORS": profile_data.get("ignore_ssl_errors"),
    }
    return results


def _load_profile_ini(config_path: str, profile: str) -> dict[str, str | None]:
    """Load a profile from an INI-style config file."""
    results: dict[str, str | None] = {
        "SALTAPI_URL": None,
        "SALTAPI_USER": None,
        "SALTAPI_PASS": None,
        "SALTAPI_EAUTH": "auto",
        "BACKEND": "cherrypy",
        "SSH_BASTION": None,
        "SSH_KEY": None,
        "SSH_PORT": "22",
        "IGNORE_SSL_ERRORS": None,
    }
    config = ConfigParser(interpolation=None)
    config.read(config_path)
    if config.has_section(profile):
        for key in results:
            if config.has_option(profile, key):
                results[key] = config.get(profile, key)
    return results


def _load_profile(
    config_path: str | None,
    profile: str,
    fmt: str | None = None,
) -> dict[str, str | None]:
    """Load profile from config, with env override."""
    path, actual_fmt = _find_config(config_path)
    actual_fmt = fmt or actual_fmt

    if not path or not actual_fmt:
        return _empty_profile()

    if actual_fmt == "yaml":
        results = _load_profile_yaml(path, profile)
    else:
        results = _load_profile_ini(path, profile)

    # Environment overrides
    _apply_env_overrides(results)

    return results


def _empty_profile() -> dict[str, str | None]:
    """Return a profile dict with all defaults."""
    return {
        "SALTAPI_URL": None,
        "SALTAPI_USER": None,
        "SALTAPI_PASS": None,
        "SALTAPI_EAUTH": "auto",
        "BACKEND": "cherrypy",
        "SSH_BASTION": None,
        "SSH_KEY": None,
        "SSH_PORT": "22",
        "IGNORE_SSL_ERRORS": None,
    }


def _apply_env_overrides(results: dict[str, str | None]) -> None:
    """Apply environment variable overrides to a profile dict."""
    env_map = {
        "SALTAPI_URL": "SALTAPI_URL",
        "SALTAPI_USER": "SALTAPI_USER",
        "SALTAPI_PASS": "SALTAPI_PASS",
        "SALTAPI_EAUTH": "SALTAPI_EAUTH",
        "BACKEND": "PEPPER_BACKEND",
        "SSH_BASTION": "PEPPER_SSH_BASTION",
        "SSH_KEY": "PEPPER_SSH_KEY",
        "SSH_PORT": "PEPPER_SSH_PORT",
        "IGNORE_SSL_ERRORS": "PEPPER_IGNORE_SSL_ERRORS",
    }
    for key, env_name in env_map.items():
        env_val = os.environ.get(env_name)
        if env_val is not None:
            results[key] = env_val

    return results


def _profile_name() -> str:
    return os.environ.get("PEPPERPROFILE", "main")


# ═══════════════════════════════════════════════════════════════════════
# Shared context
# ═══════════════════════════════════════════════════════════════════════


class PepperConfig:
    """Holds resolved connection and authentication state."""

    def __init__(self) -> None:
        self.api_url: str = "https://localhost:8000"
        self.api: Pepper | None = None
        self.auth: dict[str, Any] = {}
        self.backend: str = "cherrypy"
        self.verbose: int = 0
        self.debug_http: bool = False
        self.ignore_ssl: bool = False
        self.userun: bool = False
        self.mktoken: bool = False
        self.cache: str = os.environ.get(
            "PEPPERCACHE",
            os.path.join(os.path.expanduser("~"), ".peppercache"),
        )
        self.output_format: str = "json"
        self.jwt: str | None = None
        self.ssh_bastion: str | None = None
        self.ssh_key: str | None = None
        self.ssh_port: int = 22

    def setup_logging(self) -> None:
        root = logging.getLogger()
        # Remove any existing handlers to avoid duplicates
        for h in root.handlers[:]:
            root.removeHandler(h)
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(levelname)s  %(message)s"
        ))
        root.addHandler(handler)
        level = max(logging.ERROR - (self.verbose * 10), 1)
        root.setLevel(level)

    def build_api(self) -> Pepper:
        logger.info("Connecting to %s (backend: %s)", self.api_url, self.backend)
        return Pepper(
            self.api_url,
            debug_http=self.debug_http,
            ignore_ssl_errors=self.ignore_ssl,
            backend=self.backend,  # type: ignore[arg-type]
            ssh_bastion=self.ssh_bastion,
            ssh_key=self.ssh_key,
            ssh_port=self.ssh_port,
        )


# ═══════════════════════════════════════════════════════════════════════
# JSON decoder
# ═══════════════════════════════════════════════════════════════════════


def _parse_json_input(
    inline: str | None, filepath: str | None
) -> list[dict] | None:
    if inline:
        try:
            data = json.loads(inline)
            return _normalize_lowstate(data)
        except json.JSONDecodeError as exc:
            raise PepperArgumentsException(
                f"Invalid JSON: {exc}"
            ) from exc
    if filepath:
        try:
            with open(filepath) as fh:
                data = json.load(fh)
                return _normalize_lowstate(data)
        except FileNotFoundError as exc:
            raise PepperArgumentsException(
                f"Cannot open file: {filepath}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise PepperArgumentsException(
                f"Invalid JSON in {filepath}: {exc}"
            ) from exc
    return None


def _normalize_lowstate(data: Any) -> list[dict]:
    """Wrap a bare dict in a list so downstream can always iterate safely.

    Salt's lowstate accepts both ``{"client": "local", ...}`` (a single
    command) and ``[{"client": "local", ...}]`` (an array).  Pepper always
    normalises to the list form.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise PepperArgumentsException(
        "Lowstate must be a JSON object or array of objects"
    )


# ═══════════════════════════════════════════════════════════════════════
# Error formatting — rich context from salt-api responses
# ═══════════════════════════════════════════════════════════════════════


def _format_pepper_error(exc: Exception) -> str:
    """Format a Pepper exception with extra context if available."""
    msg = str(exc)
    if isinstance(exc, PepperException) and exc.response_body:
        try:
            data = json.loads(exc.response_body)
            if isinstance(data, dict):
                ret = data.get("return")
                if isinstance(ret, list) and ret:
                    err_msg = ret[0]
                    if isinstance(err_msg, dict):
                        parts = []
                        for k in ("message", "error", "info", "comment"):
                            v = err_msg.get(k)
                            if v:
                                parts.append(f"{k}: {v}")
                        if parts:
                            msg += " | " + "; ".join(parts)
        except (json.JSONDecodeError, TypeError, IndexError):
            pass
    return f"Pepper error: {msg}"


# ═══════════════════════════════════════════════════════════════════════
# Argument parsing
# ═══════════════════════════════════════════════════════════════════════


def _build_lowstate(
    ctx: click.Context,
    client: str,
    target: str | None,
    command: str | None,
    args: tuple[str, ...],
    expr_form: str,
    batch: str | None,
    api: Pepper,
    json_input: list[dict] | None,
    metadata: dict | None = None,
) -> list[dict]:
    if json_input is not None:
        return json_input

    args_list = list(args)
    low: dict[str, Any] = {"client": client}

    if client.startswith(("local", "ssh")):
        if target is None or command is None:
            raise click.UsageError(
                "Target and function are required for local/ssh clients",
                ctx=ctx,
            )
        low["tgt"] = target
        low["fun"] = command
        if expr_form:
            low["tgt_type"] = expr_form
        if batch:
            low["batch"] = batch
        if args_list:
            low["arg"] = args_list

    elif client.startswith(("runner", "wheel")):
        if command is None:
            raise click.UsageError(
                f"Function name required for '{client}' client",
                ctx=ctx,
            )
        low["fun"] = command
        if api.salt_version:
            low["arg"] = args_list
        else:
            for arg_val in args_list:
                if "=" in arg_val:
                    key, value = arg_val.split("=", 1)
                    try:
                        low[key] = json.loads(value)
                    except json.JSONDecodeError:
                        low[key] = value
                else:
                    low.setdefault("arg", []).append(arg_val)
    else:
        raise PepperException(f"Client not implemented: {client}")

    if metadata:
        low["metadata"] = metadata

    return [low]


# ═══════════════════════════════════════════════════════════════════════
# Auth helpers
# ═══════════════════════════════════════════════════════════════════════


def _authenticate(
    cfg: PepperConfig,
    username: str | None,
    password: str | None,
    eauth: str,
    non_interactive: bool,
    token_expire: int | None,
    config_path: str | None = None,
    profile_name: str | None = None,
) -> None:
    api = cfg.api
    assert api is not None

    if cfg.jwt:
        api.jwt_login(cfg.jwt)
        cfg.auth = api.auth
        return

    profile = _load_profile(config_path, profile_name or _profile_name())
    resolved_user = username or profile.get("SALTAPI_USER")
    resolved_pass = password or profile.get("SALTAPI_PASS")
    resolved_eauth = eauth or profile.get("SALTAPI_EAUTH") or "auto"

    # Alcali backend: use JWT login on /api/token/
    if cfg.backend == "alcali":
        if resolved_user is None:
            if non_interactive:
                raise PepperAuthException("SALTAPI_USER required")
            resolved_user = input("Username: ")
        if resolved_pass is None:
            if non_interactive:
                raise PepperAuthException("SALTAPI_PASS required")
            resolved_pass = getpass.getpass(prompt="Password: ")
        cfg.auth = api.alcali_login(resolved_user, resolved_pass)  # type: ignore[union-attr]
        return

    if resolved_user is None:
        if non_interactive:
            raise PepperAuthException("SALTAPI_USER required")
        resolved_user = input("Username: ")

    if resolved_pass is None and resolved_eauth != "kerberos":
        if non_interactive:
            raise PepperAuthException("SALTAPI_PASS required")
        resolved_pass = getpass.getpass(prompt="Password: ")

    auth_kwargs: dict[str, Any] = {
        "username": resolved_user,
        "password": resolved_pass,
        "eauth": resolved_eauth,
    }
    if token_expire is not None:
        auth_kwargs["token_expire"] = token_expire

    login_method = api.token if cfg.userun else api.login
    if cfg.mktoken:
        _load_or_create_token(cfg, login_method, auth_kwargs)
    else:
        cfg.auth = login_method(**auth_kwargs)
    api.auth = cfg.auth


def _load_or_create_token(
    cfg: PepperConfig,
    login_method: Any,
    auth_kwargs: dict,
) -> None:
    token_file = cfg.cache
    try:
        with open(token_file) as fh:
            auth = json.load(fh)
        if auth.get("expire", 0) < time.time() + 30:
            logger.error("Login token expired")
            raise ValueError("expired")
        cfg.auth = auth
        return
    except Exception:
        auth = login_method(**auth_kwargs)
        try:
            fdsc = os.open(token_file, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fdsc, "wt") as fh:
                json.dump(auth, fh)
        except Exception as exc:
            logger.error("Unable to save token to %s: %s", token_file, exc)
        cfg.auth = auth


# ═══════════════════════════════════════════════════════════════════════
# Poller
# ═══════════════════════════════════════════════════════════════════════


def _poll_for_returns(
    cfg: PepperConfig,
    load: list[dict],
    timeout: int,
    fail_incomplete: bool,
    seconds_to_wait: int = 3,
) -> Any:
    api = cfg.api
    assert api is not None

    load[0]["client"] = "local_async"
    async_ret = api.low(load)
    jid = async_ret["return"][0]["jid"]
    nodes = async_ret["return"][0]["minions"]

    ret_nodes: list[str] = []
    start_time = time.time()
    exit_code = 0

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            exit_code = 1
            break

        jid_ret = api.low(
            [
                {
                    "client": "runner",
                    "fun": "jobs.lookup_jid",
                    "kwarg": {"jid": jid},
                }
            ]
        )
        inner_ret = jid_ret["return"][0]
        if "data" in inner_ret:
            inner_ret = inner_ret["data"]

        responded = set(inner_ret.keys()) - set(ret_nodes)
        for node in responded:
            yield None, [{node: inner_ret[node]}]

        ret_nodes = list(inner_ret.keys())
        if set(ret_nodes) >= set(nodes):
            break
        time.sleep(seconds_to_wait)

    exit_code = exit_code if fail_incomplete else 0
    failed = set(nodes) - set(ret_nodes)
    if failed:
        yield exit_code, [{"Failed": sorted(failed)}]


# ═══════════════════════════════════════════════════════════════════════
# Output helper
# ═══════════════════════════════════════════════════════════════════════


def _output(result: Any, output_file: str | None, fmt: str = "json") -> None:
    formatted = format_result(result, fmt=fmt)
    if output_file:
        with open(output_file, "a") as fh:
            print(formatted, file=fh)
    else:
        click.echo(formatted)


# ═══════════════════════════════════════════════════════════════════════
# Shared auth/resolve helpers for subcommands
# ═══════════════════════════════════════════════════════════════════════


def _resolve_config(ctx: click.Context) -> PepperConfig:
    """Build a PepperConfig from parent context (for subcommands)."""
    cfg = PepperConfig()
    p = ctx.parent.params  # type: ignore[union-attr]
    cfg.verbose = p.get("verbose", 0)
    cfg.debug_http = p.get("debug_http", False)
    cfg.ignore_ssl = p.get("ignore_ssl_errors", False)
    cfg.userun = p.get("run_uri", False)
    cfg.mktoken = p.get("make_token", False)
    cfg.cache = p.get("cache_path") or cfg.cache
    cfg.output_format = p.get("output_format") or "json"
    cfg.jwt = p.get("jwt_token")
    cfg.backend = p.get("backend") or cfg.backend

    # Resolve URL
    saltapi_url = p.get("saltapi_url")
    config_path = p.get("config_path")
    profile_name_val = p.get("profile_name") or _profile_name()
    profile = _load_profile(config_path, profile_name_val)
    cfg.api_url = (
        saltapi_url
        or profile.get("SALTAPI_URL")
        or os.environ.get("SALTAPI_URL")
        or "https://localhost:8000"
    )

    cfg.api = cfg.build_api()
    cfg.setup_logging()

    _authenticate(
        cfg,
        p.get("username"),
        p.get("password"),
        p.get("eauth", "auto"),
        p.get("non_interactive", False),
        p.get("token_expire"),
        config_path=config_path,
        profile_name=profile_name_val,
    )
    return cfg


# ═══════════════════════════════════════════════════════════════════════
# CLI — Root group
# ═══════════════════════════════════════════════════════════════════════

# Shared options applied to root + subcommands that need them
_SHARED_OPTS = [
    click.option(
        "-c", "--config", "config_path", default=None,
        help="Config file path (default: ~/.pepperrc.yml or ~/.pepperrc).",
    ),
    click.option(
        "-p", "--profile", "profile_name",
        default=lambda: os.environ.get("PEPPERPROFILE", "main"),
        help="Config profile (default: $PEPPERPROFILE or 'main').",
        show_default=False,
    ),
    click.option(
        "-u", "--saltapi-url", "saltapi_url",
        help="salt-api URL (default: from config or https://localhost:8000).",
    ),
    click.option(
        "-a", "--auth", "--eauth", "--extended-auth", "eauth",
        default=None,
        help="External auth backend (pam, auto, kerberos, etc.).",
    ),
    click.option("--username", help="Salt API username."),
    click.option("--password", help="Salt API password."),
    click.option(
        "--non-interactive", is_flag=True, default=False,
        help="Fail rather than prompting for missing credentials.",
    ),
    click.option(
        "--token-expire", type=int,
        help="EAuth token expiry in seconds.",
    ),
    click.option(
        "-T", "--make-token", is_flag=True, default=False,
        help="Generate and cache an eauth token.",
    ),
    click.option(
        "-r", "--run-uri", is_flag=True, default=False,
        help="Use /run URL with token.",
    ),
    click.option(
        "-x", "--cache", "cache_path",
        default=lambda: os.environ.get(
            "PEPPERCACHE",
            os.path.join(os.path.expanduser("~"), ".peppercache"),
        ),
        help="Token cache file.",
        show_default=False,
    ),
    click.option(
        "--jwt", "jwt_token", default=None,
        help="JWT token for authentication (rest_tornado).",
    ),
    click.option(
        "--backend", "backend",
        type=click.Choice(["cherrypy", "alcali"]),
        default=None,
        help="Backend: cherrypy (salt-api rest_cherrypy) or alcali (Django REST API).",
    ),
    click.option(
        "-v", "--verbose", count=True,
        help="Increase verbosity.",
    ),
    click.option(
        "-H", "--debug-http", is_flag=True, default=False,
        help="Print HTTP request/response headers.",
    ),
    click.option(
        "--ignore-ssl-errors", is_flag=True, default=False,
        help="Skip SSL certificate verification.",
    ),
    click.option(
        "-o", "--out", "output_format",
        type=click.Choice(["json", "yaml", "table"]),
        default="json", help="Output format.",
    ),
]


def _apply_shared_opts(fn: Any) -> Any:
    """Decorate a click command with all shared options."""
    for decorator in reversed(_SHARED_OPTS):
        fn = decorator(fn)
    return fn


def _pepper_config_from_params(params: dict) -> PepperConfig:
    """Build PepperConfig from a flat param dict (reused by subcommands)."""
    cfg = PepperConfig()
    cfg.verbose = params.get("verbose", 0)
    cfg.debug_http = params.get("debug_http", False)
    cfg.ignore_ssl = params.get("ignore_ssl_errors", False)
    cfg.userun = params.get("run_uri", False)
    cfg.mktoken = params.get("make_token", False)
    cfg.cache = params.get("cache_path") or cfg.cache
    cfg.output_format = params.get("output_format") or "json"
    cfg.jwt = params.get("jwt_token")
    cfg.backend = params.get("backend") or cfg.backend
    cfg.setup_logging()

    saltapi_url = params.get("saltapi_url")
    config_path = params.get("config_path")
    profile = _load_profile(config_path, _profile_name())
    cfg.api_url = (
        saltapi_url
        or profile.get("SALTAPI_URL")
        or os.environ.get("SALTAPI_URL")
        or "https://localhost:8000"
    )
    # SSL verification from profile/env (CLI flag was already applied above)
    if not cfg.ignore_ssl:
        profile_ssl = profile.get("IGNORE_SSL_ERRORS")
        if profile_ssl is not None:
            cfg.ignore_ssl = str(profile_ssl).lower() in ("true", "1", "yes")
        elif os.environ.get("PEPPER_IGNORE_SSL_ERRORS", "").lower() in ("true", "1", "yes"):
            cfg.ignore_ssl = True
    cfg.ssh_bastion = profile.get("SSH_BASTION") or os.environ.get("PEPPER_SSH_BASTION")
    cfg.ssh_key = profile.get("SSH_KEY") or os.environ.get("PEPPER_SSH_KEY")
    try:
        cfg.ssh_port = int(
            profile.get("SSH_PORT")
            or os.environ.get("PEPPER_SSH_PORT")
            or "22"
        )
    except (ValueError, TypeError):
        cfg.ssh_port = 22
    cfg.api = cfg.build_api()

    _authenticate(
        cfg,
        params.get("username"),
        params.get("password"),
        params.get("eauth", "auto"),
        params.get("non_interactive", False),
        params.get("token_expire"),
        config_path=config_path,
    )
    return cfg


# ── Error handler ──


def _handle_pepper_error(
    ctx: click.Context, exc: Exception
) -> None:
    """Report a Pepper exception to stderr."""
    click.echo(_format_pepper_error(exc), err=True)
    ctx.exit(1)


# ═══════════════════════════════════════════════════════════════════════
# CLI — inline mode (default, no subcommand)
# ═══════════════════════════════════════════════════════════════════════


@click.group(invoke_without_command=True)
@_apply_shared_opts
@click.option(
    "-t", "--timeout", type=int, default=60,
    help="Command timeout in seconds (default: 60).",
)
@click.option(
    "--client", default="local",
    help="Salt client: local, local_async, local_batch, runner, wheel, ssh, queue.",
)
@click.option(
    "--json", "json_input", help="Raw JSON lowstate.",
)
@click.option(
    "--json-file", "json_file", type=click.Path(exists=True),
    help="File containing JSON lowstate.",
)
@click.option(
    "--output-file", "output_file", help="Write output to FILE (appends).",
)
@click.option(
    "--metadata", "metadata_json", default=None,
    help="Arbitrary JSON metadata attached to the job.",
)
@click.option(
    "--fail-incomplete", is_flag=True, default=False,
    help="Return non-zero if not all expected minions respond.",
)
@click.option(
    "--fail-any", is_flag=True, default=False,
    help="Fail if any minion retcode is non-zero.",
)
@click.option(
    "--fail-any-none", is_flag=True, default=False,
    help="Fail if any retcode is non-zero or no retcodes exist.",
)
@click.option(
    "--fail-all", is_flag=True, default=False,
    help="Fail if ALL minion retcodes are non-zero.",
)
@click.option(
    "--fail-all-none", is_flag=True, default=False,
    help="Fail if all retcodes are non-zero or no retcodes exist.",
)
@click.option(
    "-E", "--pcre", "expr_form", flag_value="pcre",
    help="Target with PCRE regular expressions.",
)
@click.option(
    "-L", "--list", "expr_form", flag_value="list",
    help="Comma-separated list of hostnames.",
)
@click.option(
    "-G", "--grain", "expr_form", flag_value="grain",
    help="Target by grain value.",
)
@click.option(
    "--grain-pcre", "expr_form", flag_value="grain_pcre",
    help="Target by PCRE grain match.",
)
@click.option(
    "-I", "--pillar", "expr_form", flag_value="pillar",
    help="Target by pillar value.",
)
@click.option(
    "--pillar-pcre", "expr_form", flag_value="pillar_pcre",
    help="Target by PCRE pillar match.",
)
@click.option(
    "-R", "--range", "expr_form", flag_value="range",
    help="Target by range expression.",
)
@click.option(
    "-C", "--compound", "expr_form", flag_value="compound",
    help="Compound target expression.",
)
@click.option(
    "-N", "--nodegroup", "expr_form", flag_value="nodegroup",
    help="Named nodegroup target.",
)
@click.option(
    "--batch", default=None,
    help="Batch size (e.g. '50%', '10').",
)
@click.option(
    "-m", "--master", "master_config",
    default=lambda: os.environ.get(
        "MASTER_CONFIG",
        os.path.join(os.path.expanduser("~"), ".config", "pepper", "master"),
    ),
    help="Salt master config path (for outputters).",
    show_default=False,
)
@click.argument("target", required=False)
@click.argument("command", required=False)
@click.argument("extra_args", nargs=-1)
@click.pass_context
def cli(
    ctx: click.Context,
    config_path: str | None,
    profile_name: str,
    saltapi_url: str | None,
    eauth: str,
    username: str | None,
    password: str | None,
    non_interactive: bool,
    token_expire: int | None,
    make_token: bool,
    run_uri: bool,
    cache_path: str,
    jwt_token: str | None,
    backend: str | None,
    verbose: int,
    debug_http: bool,
    ignore_ssl_errors: bool,
    output_format: str,
    timeout: int,
    client: str,
    json_input: str | None,
    json_file: str | None,
    output_file: str | None,
    metadata_json: str | None,
    fail_incomplete: bool,
    fail_any: bool,
    fail_any_none: bool,
    fail_all: bool,
    fail_all_none: bool,
    expr_form: str,
    batch: str | None,
    master_config: str,
    target: str | None,
    command: str | None,
    extra_args: tuple[str, ...],
) -> None:
    """
    Salt API CLI — run commands on a remote Salt infrastructure.

    \b
    Examples:

        pepper '*' test.ping
        pepper -C 'G@os:Ubuntu' cmd.run 'uptime'
        pepper --client runner jobs.list_jobs
        pepper run '*' test.ping
        pepper key list
        pepper job status <jid>
        pepper event
    """
    if ctx.invoked_subcommand is not None:
        return

    # nargs=-1 on group arguments prevents Click from detecting
    # subcommands when argv has target=setup, command=verify, etc.
    # Check if the first arg matches a known subcommand and dispatch.
    _SUBCMDS = frozenset({"run", "key", "event", "completions", "job", "setup"})
    if command in _SUBCMDS:
        sub_cmd = command
        sub_args = [sub_cmd]
    elif target in _SUBCMDS:
        sub_cmd = target
        sub_args = [sub_cmd] + ([command] if command else [])
    else:
        sub_cmd = None
    
    if sub_cmd is not None:
        sub_args += list(extra_args)
        cmd_obj = cli.get_command(ctx, sub_cmd)
        if cmd_obj is not None:
            ctx.invoked_subcommand = sub_cmd
            # Parse and invoke the subcommand (a group like setup or key)
            # with the remaining args
            try:
                cmd_obj.main(
                    args=sub_args[1:],  # exclude the subcommand name itself
                    parent=ctx,
                    standalone_mode=False,
                )
            except SystemExit:
                pass
            return

    # ── Config check ──
    _maybe_migrate_config()

    # ── No-config guard ──
    # If no config file exists and the user hasn't provided enough
    # information via CLI flags, show a helpful message instead of
    # prompting for credentials and falling back to localhost:8000.
    if (
        not saltapi_url
        and not username
        and not password
        and not _config_exists()
        and ctx.invoked_subcommand is None
    ):
        click.echo()
        click.secho("No configuration found.", fg="yellow", bold=True)
        click.echo(
            "  Run 'pepper setup init' to create a configuration file,"
        )
        click.echo(
            "  or pass credentials via --username/--password/--saltapi-url."
        )
        click.echo()
        ctx.exit(1)
        return

    # ── Mutual exclusion ──
    fail_flags = [
        n
        for n, v in [
            ("fail_any", fail_any),
            ("fail_any_none", fail_any_none),
            ("fail_all", fail_all),
            ("fail_all_none", fail_all_none),
        ]
        if v
    ]
    if len(fail_flags) > 1:
        raise click.UsageError(
            f"Options {', '.join(fail_flags)} are mutually exclusive"
        )

    # ── Metadata ──
    parsed_metadata: dict | None = None
    if metadata_json:
        try:
            parsed_metadata = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise click.UsageError(
                f"Invalid --metadata JSON: {exc}"
            ) from exc

    # ── Build config ──
    cfg = PepperConfig()
    cfg.verbose = verbose
    cfg.debug_http = debug_http
    cfg.ignore_ssl = ignore_ssl_errors or False
    cfg.userun = run_uri
    cfg.mktoken = make_token
    cfg.cache = cache_path
    cfg.output_format = output_format
    cfg.jwt = jwt_token
    cfg.backend = backend or cfg.backend
    cfg.setup_logging()

    if cfg.debug_http:
        logger.info("Enabling HTTP debug logging")
        try:
            import http.client as _http_client
            _http_client.HTTPConnection.debuglevel = 1
            requests_log = logging.getLogger("requests.packages.urllib3")
            requests_log.setLevel(logging.DEBUG)
            requests_log.propagate = True
        except Exception:
            pass

    profile = _load_profile(config_path, profile_name)
    logger.info(
        "Config: %s profile=%s",
        config_path or "~/.pepperrc.yml",
        profile_name,
    )

    cfg.api_url = (
        saltapi_url
        or profile.get("SALTAPI_URL")
        or os.environ.get("SALTAPI_URL")
        or "https://localhost:8000"
    )
    # SSL verification — CLI flag takes priority, fall back to profile/env
    if not ignore_ssl_errors:
        profile_ssl = profile.get("IGNORE_SSL_ERRORS")
        if profile_ssl is not None:
            cfg.ignore_ssl = str(profile_ssl).lower() in ("true", "1", "yes")
        elif os.environ.get("PEPPER_IGNORE_SSL_ERRORS", "").lower() in ("true", "1", "yes"):
            cfg.ignore_ssl = True
    if cfg.ignore_ssl:
        logger.info("SSL verification disabled")
    # SSH bastion tunnel config
    cfg.ssh_bastion = profile.get("SSH_BASTION") or os.environ.get("PEPPER_SSH_BASTION")
    cfg.ssh_key = profile.get("SSH_KEY") or os.environ.get("PEPPER_SSH_KEY")
    try:
        cfg.ssh_port = int(
            profile.get("SSH_PORT")
            or os.environ.get("PEPPER_SSH_PORT")
            or "22"
        )
    except (ValueError, TypeError):
        cfg.ssh_port = 22
    if cfg.ssh_bastion:
        logger.info("SSH bastion: %s", cfg.ssh_bastion)
    cfg.api = cfg.build_api()
    api = cfg.api

    # Click.group with invoke_without_command=True and nargs=-1 doesn't
    # properly parse --non-interactive when it appears after positional args.
    # Detect it early since it affects authentication behavior.
    _KNOWN_CLI_FLAGS: frozenset = frozenset({
        "--non-interactive", "--debug-http", "--ignore-ssl-errors",
        "--make-token", "--run-uri", "--json", "--json-file",
        "--fail-incomplete", "--fail-any", "--fail-any-none",
        "--fail-all", "--fail-all-none", "--output-file",
        "-v", "-vv", "-vvv", "-vvvv",
        "--username", "--password", "--auth", "--eauth",
        "--extended-auth", "--backend", "--jwt",
        "--saltapi-url", "--config", "--profile",
        "--client", "--timeout", "--batch", "--master",
        "--metadata", "--out", "-o", "-t", "-T", "-c", "-p", "-u", "-a", "-H",
    })
    leaked_extra = [a for a in extra_args if a in _KNOWN_CLI_FLAGS]
    if leaked_extra:
        logger.info("Applying leaked CLI flags: %s", " ".join(leaked_extra))
        for flag in leaked_extra:
            if flag == "--non-interactive":
                non_interactive = True
            elif flag in ("-v", "-vv", "-vvv", "-vvvv"):
                verbose = len(flag) - 1  # -v → 1, -vv → 2, etc.
                cfg.verbose = verbose
                cfg.setup_logging()
            elif flag == "--debug-http":
                debug_http = True
                cfg.debug_http = True
            elif flag == "--ignore-ssl-errors":
                ignore_ssl_errors = True
                cfg.ignore_ssl = True
    filtered_args = tuple(a for a in extra_args if a not in _KNOWN_CLI_FLAGS)

    _authenticate(cfg, username, password, eauth, non_interactive, token_expire, config_path, profile_name)
    logger.info("Authenticated as %s (eauth: %s)", cfg.auth.get("user", "?"), eauth or "auto")

    # ── Parse input ──
    json_lowstate = _parse_json_input(json_input, json_file)

    if client.startswith("local") and batch:
        client = "local_batch"

    # Runner/wheel clients don't need a target — if only one positional
    # was given, treat it as the function name.
    if target is not None and command is None and client.startswith(("runner", "wheel")):
        command = target
        target = None

    load = _build_lowstate(
        ctx, client, target, command, filtered_args, expr_form,
        batch, api, json_lowstate, metadata=parsed_metadata,
    )

    for entry in load:
        if not entry.get("client", "").startswith("wheel"):
            entry["full_return"] = True

    if api.salt_version and timeout:
        for entry in load:
            if not entry.get("client", "").startswith("wheel"):
                entry["timeout"] = timeout

    # ── Execute ──
    try:
        if fail_incomplete:
            gen = _poll_for_returns(cfg, load, timeout, fail_incomplete)
            for _exit_code, partial in gen:
                _output(partial, output_file, fmt=output_format)
        else:
            path = "/run" if cfg.userun else "/"
            if cfg.userun:
                for entry in load:
                    entry["token"] = cfg.auth.get("token")
            ret = api.low(load, path=path)
            _output(ret, output_file, fmt=output_format)
            exit_code = 0

        if exit_code is not None and exit_code == 0:
            rc = PepperRetcode()
            validated: int = rc.validate(
                ctx.params,
                ret if not fail_incomplete else partial,
            )
            if isinstance(validated, int) and validated != 0:
                exit_code = validated

        ctx.exit(exit_code if exit_code is not None else 0)

    except (
        PepperException,
        PepperAuthException,
        PepperArgumentsException,
    ) as exc:
        click.echo(_format_pepper_error(exc), err=True)
        ctx.exit(1)
    except KeyboardInterrupt:
        ctx.exit(0)
    except click.exceptions.Exit:
        raise  # ctx.exit() — let Click handle it
    except Exception as exc:
        click.echo(str(exc), err=True)
        click.echo(
            "Uncaught Pepper error (increase verbosity for full traceback).",
            err=True,
        )
        logger.debug("Uncaught traceback:", exc_info=True)
        ctx.exit(1)


# ═══════════════════════════════════════════════════════════════════════
# Subcommand: pepper run
# ═══════════════════════════════════════════════════════════════════════


@cli.command()
@click.option("--client", default="local",
              help="Salt client: local, local_async, local_batch, runner, wheel, ssh, queue.")
@click.option("--json", "json_input", help="Raw JSON lowstate.")
@click.option("--json-file", "json_file", type=click.Path(exists=True),
              help="File containing JSON lowstate.")
@click.option("--output-file", "output_file", help="Write output to FILE.")
@click.option("--metadata", "metadata_json", default=None,
              help="Arbitrary JSON metadata attached to the job.")
@click.option("--fail-incomplete", is_flag=True, default=False,
              help="Return non-zero if not all expected minions respond.")
@click.option("--fail-any", is_flag=True, default=False,
              help="Fail if any minion retcode is non-zero.")
@click.option("--fail-any-none", is_flag=True, default=False,
              help="Fail if any retcode is non-zero or no retcodes exist.")
@click.option("--fail-all", is_flag=True, default=False,
              help="Fail if ALL minion retcodes are non-zero.")
@click.option("--fail-all-none", is_flag=True, default=False,
              help="Fail if all retcodes are non-zero or no retcodes exist.")
@click.option("-E", "--pcre", "expr_form", flag_value="pcre", help="Target with PCRE.")
@click.option("-L", "--list", "expr_form", flag_value="list", help="Comma-separated list.")
@click.option("-G", "--grain", "expr_form", flag_value="grain", help="Target by grain.")
@click.option("--grain-pcre", "expr_form", flag_value="grain_pcre", help="Target by PCRE grain.")
@click.option("-I", "--pillar", "expr_form", flag_value="pillar", help="Target by pillar.")
@click.option("--pillar-pcre", "expr_form", flag_value="pillar_pcre",
              help="Target by PCRE pillar.")
@click.option("-R", "--range", "expr_form", flag_value="range", help="Target by range.")
@click.option("-C", "--compound", "expr_form", flag_value="compound",
              help="Compound target expression.")
@click.option("-N", "--nodegroup", "expr_form", flag_value="nodegroup",
              help="Named nodegroup target.")
@click.option("--batch", default=None, help="Batch size (e.g. '50%').")
@click.argument("target", required=True)
@click.argument("command", required=True)
@click.argument("extra_args", nargs=-1)
@click.pass_context
def run(
    ctx: click.Context,
    client: str,
    json_input: str | None,
    json_file: str | None,
    output_file: str | None,
    metadata_json: str | None,
    fail_incomplete: bool,
    fail_any: bool,
    fail_any_none: bool,
    fail_all: bool,
    fail_all_none: bool,
    expr_form: str,
    batch: str | None,
    target: str,
    command: str,
    extra_args: tuple[str, ...],
) -> None:
    """Run a Salt command (equivalent to inline mode).

    Examples:

        pepper run '*' test.ping

        pepper run --client runner jobs.list_jobs
    """
    assert ctx.parent is not None
    p = ctx.parent.params
    p.update({
        "client": client,
        "json_input": json_input,
        "json_file": json_file,
        "output_file": output_file,
        "metadata_json": metadata_json,
        "fail_incomplete": fail_incomplete,
        "fail_any": fail_any,
        "fail_any_none": fail_any_none,
        "fail_all": fail_all,
        "fail_all_none": fail_all_none,
        "expr_form": expr_form,
        "batch": batch,
        "target": target,
        "command": command,
        "extra_args": extra_args,
    })
    ctx.invoked_subcommand = None
    cli.invoke(ctx)


# ═══════════════════════════════════════════════════════════════════════
# Subcommand: pepper key
# ═══════════════════════════════════════════════════════════════════════


@cli.group()
def key() -> None:
    """Manage Salt minion keys."""


@key.command("list")
@click.pass_context
def key_list(ctx: click.Context) -> None:
    """List all minion keys."""
    try:
        cfg = _resolve_config(ctx)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)
        return
    api = cfg.api
    assert api is not None
    try:
        result = api.wheel("key.list_all")
        _output(result, None, fmt=cfg.output_format)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)


@key.command("accept")
@click.argument("minion", required=True)
@click.pass_context
def key_accept(ctx: click.Context, minion: str) -> None:
    """Accept a minion's key."""
    try:
        cfg = _resolve_config(ctx)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)
        return
    api = cfg.api
    assert api is not None
    try:
        result = api.wheel("key.accept", match=minion)
        _output(result, None, fmt=cfg.output_format)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)


@key.command("reject")
@click.argument("minion", required=True)
@click.pass_context
def key_reject(ctx: click.Context, minion: str) -> None:
    """Reject a minion's key."""
    try:
        cfg = _resolve_config(ctx)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)
        return
    api = cfg.api
    assert api is not None
    try:
        result = api.wheel("key.reject", match=minion)
        _output(result, None, fmt=cfg.output_format)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)


@key.command("delete")
@click.argument("minion", required=True)
@click.pass_context
def key_delete(ctx: click.Context, minion: str) -> None:
    """Delete a minion's key."""
    try:
        cfg = _resolve_config(ctx)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)
        return
    api = cfg.api
    assert api is not None
    try:
        result = api.wheel("key.delete", match=minion)
        _output(result, None, fmt=cfg.output_format)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)


@key.command("finger")
@click.pass_context
def key_finger(ctx: click.Context) -> None:
    """Show minion key fingerprints."""
    try:
        cfg = _resolve_config(ctx)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)
        return
    api = cfg.api
    assert api is not None
    try:
        result = api.wheel("key.finger")
        _output(result, None, fmt=cfg.output_format)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)


# ═══════════════════════════════════════════════════════════════════════
# Subcommand: pepper event
# ═══════════════════════════════════════════════════════════════════════


@cli.command()
@click.option(
    "--tag", "-t", "tag_filter", default=None,
    help="Optional tag prefix filter (e.g. 'salt/job/').",
)
@click.option(
    "--raw", "raw_mode", is_flag=True, default=False,
    help="Print raw SSE lines instead of formatted JSON.",
)
@click.option(
    "--count", "-n", "max_events", type=int, default=0,
    help="Stop after N events (0 = infinite).",
)
@click.pass_context
def event(
    ctx: click.Context,
    tag_filter: str | None,
    raw_mode: bool,
    max_events: int,
) -> None:
    """
    Stream Salt events from the salt-api SSE endpoint.

    Connects to ``/events`` and prints each event as it arrives.
    Press Ctrl+C to stop.
    """
    try:
        cfg = _resolve_config(ctx)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)
        return
    api = cfg.api
    assert api is not None

    try:
        response = api.req_stream("/events")
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)
        return

    count = 0
    try:
        for line in response.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8") if isinstance(line, bytes) else line

            # SSE format: "tag: <tag>" then "data: <json>"
            if decoded.startswith("tag:"):
                current_tag = decoded[4:].strip()
                continue
            if decoded.startswith("data:"):
                data_str = decoded[5:].strip()
                if tag_filter and current_tag and not current_tag.startswith(tag_filter):
                    continue
                count += 1
                if raw_mode:
                    click.echo(f"tag: {current_tag}\ndata: {data_str}\n")
                else:
                    try:
                        parsed = json.loads(data_str)
                        output = json.dumps(
                            {"tag": current_tag, "data": parsed},
                            indent=2, default=str,
                        )
                        click.echo(output)
                    except json.JSONDecodeError:
                        click.echo(f"tag: {current_tag}\ndata: {data_str}")
                if max_events and count >= max_events:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        response.close()


# ═══════════════════════════════════════════════════════════════════════
# Subcommand: pepper completions
# ═══════════════════════════════════════════════════════════════════════


@cli.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completions(shell: str) -> None:
    """Generate shell completion script."""
    # Click provides a built-in completion mechanism
    # We output the shell script to stdout
    script = _completion_script(shell)
    click.echo(script)


def _completion_script(shell: str) -> str:
    """Generate a shell completion script."""
    if shell == "bash":
        return """
# bash completion for pepper
_pepper_completions() {
    local cur prev words cword
    _init_completion || return
    COMP_WORDS=( "${COMP_WORDS[@]}" )
    COMP_CWORD=$COMP_CWORD
    _pepper_completions_bash
}
complete -F _pepper_completions pepper
""".strip()
    elif shell == "zsh":
        return """
#compdef pepper
_pepper() {
    eval $(env _CLICK_PEPPER_COMPLETE=zsh_source pepper)
}
if [[ $ZSH_EVAL_CONTEXT == *:file:* ]]; then
    _pepper
fi
""".strip()
    elif shell == "fish":
        return """
# fish completion for pepper
complete -c pepper -f
""".strip()
    return ""


# ═══════════════════════════════════════════════════════════════════════
# Subcommand: pepper job (existing, with shared auth)
# ═══════════════════════════════════════════════════════════════════════


@cli.group()
def job() -> None:
    """Query and manage Salt jobs."""


@job.command()
@click.argument("jid", required=True)
@click.pass_context
def status(ctx: click.Context, jid: str) -> None:
    """Show the status of a Salt job (list_job)."""
    try:
        cfg = _resolve_config(ctx)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)
        return
    api = cfg.api
    assert api is not None
    try:
        result = api.runner("jobs.list_job", jid=jid)
        _output(result, None, fmt=cfg.output_format)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)


@job.command()
@click.argument("jid", required=True)
@click.pass_context
def log(ctx: click.Context, jid: str) -> None:
    """Show the results of a completed job (lookup_jid)."""
    try:
        cfg = _resolve_config(ctx)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)
        return
    api = cfg.api
    assert api is not None
    try:
        result = api.lookup_jid(jid)
        _output(result, None, fmt=cfg.output_format)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)


@job.command()
@click.argument("jid", required=True)
@click.pass_context
def kill(ctx: click.Context, jid: str) -> None:
    """Kill a running Salt job."""
    try:
        cfg = _resolve_config(ctx)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)
        return
    api = cfg.api
    assert api is not None
    try:
        result = api.runner("jobs.kill_job", jid=jid)
        _output(result, None, fmt=cfg.output_format)
    except (PepperException, PepperAuthException) as exc:
        _handle_pepper_error(ctx, exc)


# ═══════════════════════════════════════════════════════════════════════
# Subcommand: pepper setup (config initialization and verification)
# Note: named "setup" not "config" to avoid conflict with the
# --config option on the cli group.


@cli.group()
def setup() -> None:
    """Manage pepper configuration files."""


def _pick_or_create_profile(data: dict[str, object]) -> str:
    """Let the user pick an existing profile or add a new one."""
    profiles = list(data.keys())
    if not profiles:
        return click.prompt("Profile name", default="main")

    click.echo()
    click.secho("Existing profiles:", bold=True)
    for i, name in enumerate(profiles, 1):
        p = data[name]
        pw = p.get("saltapi_pass", "") if isinstance(p, dict) else ""
        masked = "****" if pw else "(no password)"
        token = p.get("jwt_token", "") if isinstance(p, dict) else ""
        tmasked = f", token:****" if token else ""
        url = p.get("saltapi_url", "?") if isinstance(p, dict) else "?"
        user = p.get("saltapi_user", "?") if isinstance(p, dict) else "?"
        click.echo(f"  {i}. {name} \u2014 {user} @ {url} ({masked}{tmasked})")

    click.echo(f"  {len(profiles) + 1}. Add a new profile")
    click.echo(f"  {len(profiles) + 2}. Start fresh (overwrite all)")
    click.echo()

    while True:
        try:
            choice = click.prompt(
                "Select", type=int, default=1,
            )
            if 1 <= choice <= len(profiles):
                return profiles[choice - 1]
            elif choice == len(profiles) + 1:
                return click.prompt("New profile name", default="main")
            elif choice == len(profiles) + 2:
                if click.confirm("Overwrite all existing profiles?", default=False):
                    return click.prompt("Profile name", default="main")
                click.echo("Cancelled.")
                continue
            else:
                click.secho(f"  Choose 1\u2013{len(profiles) + 2}.", fg="yellow")
        except (ValueError, TypeError):
            click.secho("  Enter a number.", fg="yellow")


def _prompt_ssh_key(prev_value: str | None = None) -> str | None:
    """Interactively prompt for an SSH key path.

    If the user enters a directory path, list its contents and
    loop back so they can pick a specific file.
    """
    while True:
        hint = f" [blank = keep: {prev_value}]" if prev_value else ""
        raw = click.prompt(
            f"SSH identity file (optional, press Enter to skip, "
            f"or enter a directory to browse){hint}",
            default="",
            show_default=False,
        )
        if not raw and prev_value is not None:
            return prev_value
        if not raw:
            return None

        expanded = os.path.expanduser(raw)
        if os.path.isdir(expanded):
            click.echo(f"  Contents of {raw}:")
            try:
                entries = sorted(
                    e for e in os.listdir(expanded)
                    if e.startswith("id_")
                    or e.endswith((".pem", ".key", ".priv", ".pub"))
                )
                if entries:
                    for e in entries:
                        click.echo(f"    {e}")
                else:
                    for e in sorted(os.listdir(expanded)):
                        full = os.path.join(expanded, e)
                        if os.path.isfile(full) and not e.startswith("."):
                            click.echo(f"    {e}")
                click.echo()
                continue
            except PermissionError:
                click.secho(f"  (no permission to read {raw})", fg="yellow")
                continue
        elif os.path.isfile(expanded):
            return expanded
        else:
            click.secho(f"  Path does not exist: {raw}", fg="yellow")
            continue


@setup.command("init")
@click.option(
    "--profile", "profile_name",
    default=None,
    help="Profile name to edit or create (from $PEPPERPROFILE or parent -p). "
         "If omitted, interactively select.",
)
@click.pass_context
def setup_init(ctx: click.Context, profile_name: str | None) -> None:
    """Generate or edit a configuration file interactively.

    If a config file already exists with profiles, displays them with
    masked secrets and lets you select one to edit or add a new one.
    Use ``--profile`` to skip the selection and directly edit or
    create a named profile. Blank input = keep previous value.
    """
    import getpass as _getpass
    import yaml

    # Fall back to parent context's profile if our own option wasn't resolved
    # (Click's inline subcommand dispatch can consume -p at the parent level)
    if profile_name is None and ctx.parent is not None:
        profile_name = ctx.parent.params.get("profile_name")

    click.echo()
    click.secho("\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557", bold=True)
    click.secho("\u2551      salt-pepper configuration wizard    \u2551", bold=True)
    click.secho("\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d", bold=True)
    click.echo()

    # Resolve config path
    config_path = os.path.join(os.path.expanduser("~"), ".pepperrc.yml")
    existing_data: dict[str, object] = {}
    existing_config = os.path.exists(config_path)

    if existing_config:
        try:
            with open(config_path) as fh:
                existing_data = yaml.safe_load(fh) or {}
        except Exception:
            existing_data = {}

    # Determine profile name
    if profile_name:
        if isinstance(existing_data.get(profile_name), dict):
            click.secho(f"Editing profile: {profile_name}", bold=True)
        else:
            click.secho(f"Creating new profile: {profile_name}", bold=True)
    elif existing_data:
        profile_name = _pick_or_create_profile(existing_data)
    else:
        profile_name = click.prompt("Profile name", default="main")

    # Load existing values for this profile (if editing)
    existing_profile: dict[str, object] = {}
    if existing_data and isinstance(existing_data.get(profile_name), dict):
        existing_profile = existing_data[profile_name]  # type: ignore[assignment]

    def _prompt(
        label: str,
        key: str,
        default: object = "",
        secret: bool = False,
        type_: type = str,
    ) -> object:
        """Prompt with blank = keep previous value."""
        prev = existing_profile.get(key) if existing_profile else None
        if prev is not None:
            display = "****" if secret else str(prev)
            hint = f" [blank = keep: {display}]"
        else:
            hint = ""
        while True:
            if secret:
                raw = _getpass.getpass(f"{label}{hint}: ")
            else:
                raw = click.prompt(f"{label}{hint}", default="", show_default=False)
            if not raw and prev is not None:
                return prev
            if not raw:
                return default
            try:
                return type_(raw)
            except (ValueError, TypeError):
                click.secho(f"  Invalid value. Enter a {type_.__name__}.", fg="yellow")
                continue

    settings: dict[str, object] = {}

    # Backend
    prev_backend = existing_profile.get("backend", "cherrypy")
    click.secho("Backend type:", bold=True, fg="cyan")
    click.echo("  cherrypy  \u2014 Standard Salt rest_cherrypy (port 8080)")
    click.echo("  alcali    \u2014 Django REST API with JWT (port 8443)")
    settings["backend"] = click.prompt(
        "  Select backend",
        type=click.Choice(["cherrypy", "alcali"]),
        default=str(prev_backend),
        show_choices=False,
    )

    settings["saltapi_url"] = _prompt(
        "Salt API URL (e.g. https://salt.example.com:8080)", "saltapi_url",
        default=existing_profile.get("saltapi_url", "https://salt.example.com:8080"),
    )
    settings["saltapi_user"] = _prompt(
        "Username", "saltapi_user",
        default=existing_profile.get("saltapi_user", ""),
    )
    settings["saltapi_pass"] = _prompt(
        "Password", "saltapi_pass",
        default="", secret=True,
    )
    settings["saltapi_eauth"] = _prompt(
        "EAuth backend (pam, auto, kerberos, ldap, etc.)",
        "saltapi_eauth",
        default=existing_profile.get("saltapi_eauth", "pam"),
    )

    # SSL verification
    prev_ssl: bool = existing_profile.get("ignore_ssl_errors", False)  # type: ignore[assignment]
    verify = click.confirm("Verify SSL certificate?", default=not prev_ssl)
    settings["ignore_ssl_errors"] = not verify

    # SSH bastion
    click.echo()
    prev_bastion = existing_profile.get("ssh_bastion")
    use_bastion = click.confirm(
        "Use SSH bastion/jumphost?",
        default=bool(prev_bastion),
    )
    if use_bastion:
        settings["ssh_bastion"] = _prompt(
            "SSH bastion (user@host)", "ssh_bastion",
            default=prev_bastion or "",
        )
        settings["ssh_key"] = _prompt_ssh_key(
            existing_profile.get("ssh_key") if existing_profile else None,
        )
        settings["ssh_port"] = _prompt(
            "SSH port", "ssh_port",
            default=existing_profile.get("ssh_port", 22),
            type_=int,
        )

    # Test connection
    click.echo()
    click.secho("Testing connection...", bold=True)
    if settings.get("ssh_bastion"):
        click.echo(f"  (via bastion: {settings['ssh_bastion']})")
    try:
        api = Pepper(
            str(settings["saltapi_url"]),
            backend=str(settings["backend"]),  # type: ignore[arg-type]
            ignore_ssl_errors=True,
            ssh_bastion=settings.get("ssh_bastion"),
            ssh_key=settings.get("ssh_key"),
            ssh_port=int(str(settings.get("ssh_port", 22))),
        )
        result = api.login(
            str(settings["saltapi_user"]),
            str(settings["saltapi_pass"]),
            str(settings["saltapi_eauth"]),
        )
        user = result.get("user", settings["saltapi_user"])
        perms = result.get("perms", [])
        click.secho(f"\u2713 Authenticated as {user}", fg="green")
        if perms:
            click.echo(f"  Permissions: {', '.join(perms[:3])}")
        if len(perms) > 3:
            click.echo(f"  ... and {len(perms) - 3} more")
        ping = api.low([{
            "client": "local", "tgt": "*", "fun": "test.ping",
        }])
        minions = ping.get("return", [{}])[0]
        if isinstance(minions, dict) and minions:
            up = sum(1 for v in minions.values() if v is True)
            click.secho(f"\u2713 {up}/{len(minions)} minions responding", fg="green")
        else:
            click.echo("  (no minion results \u2014 config file will still work)")
    except Exception as exc:
        click.secho(f"\u2717 Connection failed: {exc}", fg="red", err=True)
        click.echo()
        if not click.confirm("Save config anyway?", default=False):
            click.echo("Cancelled.")
            return
    else:
        click.echo()
        if not click.confirm("Save this configuration?", default=True):
            click.echo("Cancelled.")
            return

    # Save (merge with existing profiles)
    merged = dict(existing_data)
    merged[profile_name] = settings

    try:
        with open(config_path, "w") as fh:
            yaml.dump(merged, fh, default_flow_style=False)
        os.chmod(config_path, 0o600)
        click.secho(f"\u2713 Saved profile '{profile_name}' to {config_path}", fg="green")
        if not existing_config:
            click.secho(
                "  \u26a0  Protect this file: passwords are stored in plain text.",
                fg="yellow",
            )
    except OSError as exc:
        click.secho(f"\u2717 Failed to write {config_path}: {exc}", fg="red", err=True)

@setup.command("verify")
@click.option(
    "-c", "--config", "config_path", default=None,
    help="Config file path (default: ~/.pepperrc.yml).",
)
@click.option(
    "--profile", "profile_name",
    default=lambda: os.environ.get("PEPPERPROFILE", "main"),
    help="Config profile (default: $PEPPERPROFILE or 'main').",
)
def setup_verify(config_path: str | None, profile_name: str) -> None:
    """Verify a configuration file is valid.

    Tests connection, authentication, and basic command execution.
    """
    import yaml

    click.echo()
    click.secho("Verifying configuration...", bold=True)

    # Resolve config path
    path = config_path or os.path.join(
        os.path.expanduser("~"), ".pepperrc.yml"
    )
    ini_path = os.path.join(os.path.expanduser("~"), ".pepperrc")
    local_path = ".pepperrc.yml"

    for candidate in [config_path, local_path, path, ini_path]:
        if candidate and os.path.exists(candidate):
            path = candidate
            break
    else:
        click.secho(f"✗ No config file found at {path}", fg="red", err=True)
        return

    click.secho(f"  Config file: {path}", bold=True)
    click.secho(f"  Profile:     {profile_name}", bold=True)

    try:
        profile = _load_profile(path, profile_name)
    except Exception as exc:
        click.secho(f"✗ Failed to load config: {exc}", fg="red", err=True)
        return

    url = profile.get("SALTAPI_URL")
    user = profile.get("SALTAPI_USER")
    pw = profile.get("SALTAPI_PASS")
    eauth = profile.get("SALTAPI_EAUTH", "auto")
    backend = profile.get("BACKEND", "cherrypy")
    ssh_bastion = profile.get("SSH_BASTION")
    ssh_key = profile.get("SSH_KEY")
    ssh_port = profile.get("SSH_PORT", "22")
    ignore_ssl = profile.get("IGNORE_SSL_ERRORS", "False").lower() in ("true", "1", "yes")

    if not url:
        click.secho("✗ No SALTAPI_URL in config", fg="red", err=True)
        return
    if not user or not pw:
        click.secho("✗ Missing SALTAPI_USER or SALTAPI_PASS", fg="red", err=True)
        return

    click.echo(f"  Backend:     {backend}")
    click.echo(f"  URL:         {url}")
    click.echo(f"  User:        {user}")
    click.echo(f"  EAuth:       {eauth}")
    if ssh_bastion:
        click.echo(f"  SSH bastion: {ssh_bastion}")
        if ssh_key:
            click.echo(f"  SSH key:     {ssh_key}")
        click.echo(f"  SSH port:    {ssh_port}")
    click.echo(f"  SSL verify:  {'off' if ignore_ssl else 'on'}")
    click.echo()

    # Test connection
    click.secho("  Step 1/3: Connecting to salt-api...")
    if ssh_bastion:
        click.echo(f"           (via bastion: {ssh_bastion})")
    try:
        api = Pepper(
            url,
            backend=backend,  # type: ignore[arg-type]
            ignore_ssl_errors=ignore_ssl,
            ssh_bastion=ssh_bastion,
            ssh_key=ssh_key,
            ssh_port=int(ssh_port) if ssh_port else 22,
        )
        click.secho("  ✓  Reachable", fg="green")
    except Exception as exc:
        click.secho(f"  ✗  {exc}", fg="red", err=True)
        return

    # Test authentication
    click.secho("  Step 2/3: Authenticating...")
    try:
        result = api.login(user, pw, eauth)
        perms = result.get("perms", [])
        click.secho(f"  ✓  Authenticated as {result.get('user', user)}", fg="green")
        if perms:
            click.echo(f"     Permissions: {', '.join(perms[:4])}")
    except Exception as exc:
        click.secho(f"  ✗  {exc}", fg="red", err=True)
        return

    # Test command execution
    click.secho("  Step 3/3: Running test.ping...")
    try:
        ping = api.low([{
            "client": "local", "tgt": "*", "fun": "test.ping",
        }])
        minions = ping.get("return", [{}])[0]
        if isinstance(minions, dict):
            up = sum(1 for v in minions.values() if v is True)
            click.secho(f"  ✓  {up}/{len(minions)} minions responded", fg="green")
        else:
            click.echo("  ?  Unexpected response format")
    except Exception as exc:
        click.secho(f"  ⚠  Command failed (but auth works): {exc}", fg="yellow")
    else:
        click.echo()

    click.secho("✓ Configuration is valid", fg="green", bold=True)


if __name__ == "__main__":
    cli()
