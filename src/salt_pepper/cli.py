"""
A CLI interface to a remote salt-api instance.

Usage::

    pepper 'minion-*' test.ping
    pepper --client runner jobs.lookup_jid jid=12345
    pepper --client wheel key.accept match=myminion
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
from salt_pepper.retcode import PepperRetcode

logger = logging.getLogger(__name__)

# ── Config loading ──────────────────────────────────────────────────


def _load_profile(
    config_path: str, profile: str
) -> dict[str, str | None]:
    """
    Load authentication details from a ``.pepperrc`` config file.

    Compatibility: reads the old INI-style format used by pepper <=3.x.
    Environment variables and CLI flags override these values.
    """
    results: dict[str, str | None] = {
        "SALTAPI_URL": None,
        "SALTAPI_USER": None,
        "SALTAPI_PASS": None,
        "SALTAPI_EAUTH": "auto",
    }

    config = ConfigParser(interpolation=None)
    config.read(config_path)

    if config.has_section(profile):
        for key in results:
            if config.has_option(profile, key):
                results[key] = config.get(profile, key)

    # Environment overrides
    for key in results:
        env_val = os.environ.get(key)
        if env_val is not None:
            results[key] = env_val

    return results


# ── Shared context ──────────────────────────────────────────────────


class PepperConfig:
    """Holds resolved connection and authentication state."""

    def __init__(self) -> None:
        self.api_url: str = "https://localhost:8000"
        self.api: Pepper | None = None
        self.auth: dict[str, Any] = {}
        self.verbose: int = 0
        self.debug_http: bool = False
        self.ignore_ssl: bool = False
        self.userun: bool = False
        self.mktoken: bool = False
        self.cache: str = os.environ.get(
            "PEPPERCACHE", os.path.join(os.path.expanduser("~"), ".peppercache")
        )

    def setup_logging(self) -> None:
        """Configure root logger based on verbosity."""
        root = logging.getLogger()
        handler = logging.StreamHandler(sys.stderr)
        root.addHandler(handler)
        level = max(logging.ERROR - (self.verbose * 10), 1)
        root.setLevel(level)

    def build_api(self) -> Pepper:
        """Create and return a Pepper client instance."""
        return Pepper(
            self.api_url,
            debug_http=self.debug_http,
            ignore_ssl_errors=self.ignore_ssl,
        )


# ── JSON decoder for --json / --json-file ──────────────────────────


def _parse_json_input(
    inline: str | None, filepath: str | None
) -> list[dict] | None:
    """Parse JSON from inline string or file. Returns None if neither."""
    if inline:
        try:
            return json.loads(inline)
        except json.JSONDecodeError as exc:
            raise PepperArgumentsException(
                f"Invalid JSON: {exc}"
            ) from exc

    if filepath:
        try:
            with open(filepath) as fh:
                return json.load(fh)
        except FileNotFoundError as exc:
            raise PepperArgumentsException(
                f"Cannot open file: {filepath}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise PepperArgumentsException(
                f"Invalid JSON in {filepath}: {exc}"
            ) from exc

    return None


# ── Argument parsing (positional → lowstate) ───────────────────────


# Horizontal line for --help output
SEPARATOR = "-" * 60


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
) -> list[dict]:
    """
    Build a lowstate list from CLI arguments.

    Two modes:
    * JSON mode — returns the parsed JSON directly.
    * CLI mode — constructs a lowstate dict from flags + positionals.
    """
    if json_input is not None:
        return json_input

    args_list = list(args)

    low: dict[str, Any] = {"client": client}

    if client.startswith("local"):
        if target is None or command is None:
            raise click.UsageError(
                "Target and function are required for local/ssh clients",
                ctx=ctx,
            )
        low["tgt"] = target
        low["fun"] = command
        low["tgt_type"] = expr_form
        if batch:
            low["batch"] = batch
        if args_list:
            low["arg"] = args_list

    elif client.startswith("ssh"):
        if target is None or command is None:
            raise click.UsageError(
                "Target and function are required for SSH client",
                ctx=ctx,
            )
        low["tgt"] = target
        low["fun"] = command
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
            # Salt Neon+ — pass args raw for kwarg splitting on server side
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

    return [low]


# ── Auth helpers ────────────────────────────────────────────────────


def _authenticate(
    cfg: PepperConfig,
    username: str | None,
    password: str | None,
    eauth: str,
    non_interactive: bool,
    token_expire: int | None,
) -> None:
    """
    Authenticate with salt-api, handling token expiry and caching.
    """
    api = cfg.api
    assert api is not None

    # Resolve credentials
    profile = _load_profile(
        _rc_path(), _profile_name()
    )
    resolved_user = (
        username or profile.get("SALTAPI_USER")
    )
    resolved_pass = password or profile.get("SALTAPI_PASS")
    resolved_eauth = eauth or profile.get("SALTAPI_EAUTH") or "auto"

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


def _rc_path() -> str:
    """Determine the config file path."""
    return os.environ.get(
        "PEPPERRC",
        os.path.join(os.path.expanduser("~"), ".pepperrc"),
    )


def _profile_name() -> str:
    """Determine the config profile name."""
    return os.environ.get("PEPPERPROFILE", "main")


def _load_or_create_token(
    cfg: PepperConfig,
    login_method: Any,
    auth_kwargs: dict,
) -> None:
    """Try loading a cached token; create one if expired or missing."""
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
            logger.error(
                "Unable to save token to %s: %s", token_file, exc
            )
        cfg.auth = auth


# ── Poller ──────────────────────────────────────────────────────────


def _poll_for_returns(
    cfg: PepperConfig,
    load: list[dict],
    timeout: int,
    fail_incomplete: bool,
    seconds_to_wait: int = 3,
) -> Any:
    """Run async, poll job cache, yield results as they arrive."""
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


# ── Output formatter ────────────────────────────────────────────────


def _output(result: Any, output_file: str | None) -> None:
    """Write result to file or stdout as pretty-printed JSON."""
    formatted = json.dumps(result, sort_keys=True, indent=4)
    if output_file:
        with open(output_file, "a") as fh:
            print(formatted, file=fh)
    else:
        click.echo(formatted)


# ── CLI ─────────────────────────────────────────────────────────────


@click.group(invoke_without_command=True)
@click.option(
    "-c",
    "--config",
    "config_path",
    default=lambda: os.environ.get(
        "PEPPERRC",
        os.path.join(os.path.expanduser("~"), ".pepperrc"),
    ),
    help="Configuration file path (default: $PEPPERRC or ~/.pepperrc).",
    show_default=False,
)
@click.option(
    "-p",
    "--profile",
    "profile_name",
    default=lambda: os.environ.get("PEPPERPROFILE", "main"),
    help="Config profile to use (default: $PEPPERPROFILE or 'main').",
    show_default=False,
)
@click.option(
    "-u",
    "--saltapi-url",
    "saltapi_url",
    help="salt-api URL (default: read from config/env or https://localhost:8000).",
)
@click.option(
    "-a",
    "--auth",
    "--eauth",
    "--extended-auth",
    "eauth",
    default="auto",
    help="External auth backend (pam, auto, kerberos, etc.).",
)
@click.option("--username", help="Salt API username.")
@click.option("--password", help="Salt API password.")
@click.option(
    "--non-interactive",
    is_flag=True,
    default=False,
    help="Fail rather than prompting for missing credentials.",
)
@click.option(
    "--token-expire",
    type=int,
    help="EAuth token expiry in seconds (if allowed by master).",
)
@click.option(
    "-T",
    "--make-token",
    is_flag=True,
    default=False,
    help="Generate and cache an eauth token for re-use.",
)
@click.option(
    "-r",
    "--run-uri",
    is_flag=True,
    default=False,
    help="Use /run URL with token instead of session-based auth.",
)
@click.option(
    "-x",
    "--cache",
    "cache_path",
    default=lambda: os.environ.get(
        "PEPPERCACHE",
        os.path.join(os.path.expanduser("~"), ".peppercache"),
    ),
    help="Token cache file (default: $PEPPERCACHE or ~/.peppercache).",
    show_default=False,
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase verbosity (can be specified multiple times).",
)
@click.option(
    "-H",
    "--debug-http",
    is_flag=True,
    default=False,
    help="Print HTTP request/response headers to stderr.",
)
@click.option(
    "--ignore-ssl-errors",
    is_flag=True,
    default=False,
    help="Skip SSL certificate verification.",
)
@click.option(
    "-t",
    "--timeout",
    type=int,
    default=60,
    help="Command timeout in seconds (default: 60).",
)
@click.option(
    "--client",
    default="local",
    help="Salt client: local, local_async, local_batch, runner, wheel, ssh.",
)
@click.option(
    "--json",
    "json_input",
    help="Raw JSON input for lowstate (bypasses positional arg parsing).",
)
@click.option(
    "--json-file",
    "json_file",
    type=click.Path(exists=True),
    help="File containing JSON lowstate.",
)
@click.option(
    "-o",
    "--out",
    "output_format",
    help="Outputter name (requires Salt installed; otherwise ignored).",
)
@click.option(
    "--output-file",
    "output_file",
    help="Write output to FILE (appends).",
)
@click.option(
    "--fail-if-incomplete",
    is_flag=True,
    default=False,
    help="Return non-zero if not all expected minions respond (requires jobs.list_jobs access).",
)
@click.option(
    "--fail-any",
    is_flag=True,
    default=False,
    help="Fail if any minion retcode is non-zero.",
)
@click.option(
    "--fail-any-none",
    is_flag=True,
    default=False,
    help="Fail if any retcode is non-zero or no retcodes exist.",
)
@click.option(
    "--fail-all",
    is_flag=True,
    default=False,
    help="Fail if ALL minion retcodes are non-zero.",
)
@click.option(
    "--fail-all-none",
    is_flag=True,
    default=False,
    help="Fail if all retcodes are non-zero or no retcodes exist.",
)
# Targeting flags
@click.option(
    "-E",
    "--pcre",
    "expr_form",
    flag_value="pcre",
    help="Target with PCRE regular expressions.",
)
@click.option(
    "-L",
    "--list",
    "expr_form",
    flag_value="list",
    help="Comma-separated list of hostnames.",
)
@click.option(
    "-G",
    "--grain",
    "expr_form",
    flag_value="grain",
    help="Target by grain value.",
)
@click.option(
    "--grain-pcre",
    "expr_form",
    flag_value="grain_pcre",
    help="Target by PCRE grain match.",
)
@click.option(
    "-I",
    "--pillar",
    "expr_form",
    flag_value="pillar",
    help="Target by pillar value.",
)
@click.option(
    "--pillar-pcre",
    "expr_form",
    flag_value="pillar_pcre",
    help="Target by PCRE pillar match.",
)
@click.option(
    "-R",
    "--range",
    "expr_form",
    flag_value="range",
    help="Target by range expression.",
)
@click.option(
    "-C",
    "--compound",
    "expr_form",
    flag_value="compound",
    help="Compound target expression.",
)
@click.option(
    "-N",
    "--nodegroup",
    "expr_form",
    flag_value="nodegroup",
    help="Named nodegroup target.",
)
@click.option(
    "--batch",
    default=None,
    help="Batch size (e.g. '50%', '10').",
)
@click.option(
    "-m",
    "--master",
    "master_config",
    default=lambda: os.environ.get(
        "MASTER_CONFIG",
        os.path.join(os.path.expanduser("~"), ".config", "pepper", "master"),
    ),
    help="Salt master config path (for outputters).",
    show_default=False,
)
# Positional arguments: target [command [args...]]
@click.argument("target", required=False)
@click.argument("command", required=False)
@click.argument("extra_args", nargs=-1, required=False)
@click.pass_context
def cli(
    ctx: click.Context,
    config_path: str,
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
    verbose: int,
    debug_http: bool,
    ignore_ssl_errors: bool,
    timeout: int,
    client: str,
    json_input: str | None,
    json_file: str | None,
    output_format: str | None,
    output_file: str | None,
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

    Examples:

        pepper '*' test.ping

        pepper -C 'G@os:Ubuntu' cmd.run 'uptime'

        pepper --client runner jobs.list_jobs

        pepper --client wheel key.list_all

        pepper --json '{"client":"local","tgt":"*","fun":"test.ping"}'
    """
    # ── Mutual exclusion for --fail-* flags ──
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

    # ── Build config context ──
    cfg = PepperConfig()
    cfg.verbose = verbose
    cfg.debug_http = debug_http
    cfg.ignore_ssl = ignore_ssl_errors
    cfg.userun = run_uri
    cfg.mktoken = make_token
    cfg.cache = cache_path
    cfg.setup_logging()

    # ── Resolve API URL ──
    if saltapi_url:
        cfg.api_url = saltapi_url
    else:
        profile = _load_profile(config_path, profile_name)
        cfg.api_url = (
            profile.get("SALTAPI_URL")
            or os.environ.get("SALTAPI_URL")
            or "https://localhost:8000"
        )

    # ── Build API client ──
    cfg.api = cfg.build_api()
    api = cfg.api

    # ── Authenticate ──
    _authenticate(cfg, username, password, eauth, non_interactive, token_expire)

    # ── Parse JSON input if provided ──
    json_lowstate = _parse_json_input(json_input, json_file)

    # ── Build lowstate ──
    if client.startswith("local") and batch:
        client = "local_batch"

    load = _build_lowstate(
        ctx,
        client,
        target,
        command,
        extra_args,
        expr_form,
        batch,
        api,
        json_lowstate,
    )

    # Add full_return for non-wheel clients
    for entry in load:
        if not entry.get("client", "").startswith("wheel"):
            entry["full_return"] = True

    # Add timeout to payload (tornado backend)
    if api.salt_version and timeout:
        for entry in load:
            if not entry.get("client", "").startswith("wheel"):
                entry["timeout"] = timeout

    # ── Execute ──
    try:
        if fail_incomplete:
            gen = _poll_for_returns(cfg, load, timeout, fail_incomplete)
            for _exit_code, partial in gen:
                _output(partial, output_file)
        else:
            # Set path based on /run VS /
            path = "/run" if cfg.userun else "/"
            if cfg.userun:
                for entry in load:
                    entry["token"] = cfg.auth.get("token")
            ret = api.low(load, path=path)
            _output(ret, output_file)
            exit_code = 0

        # ── Retcode validation ──
        if exit_code is not None and exit_code == 0:
            rc = PepperRetcode()
            validated: int = rc.validate(ctx.params, ret if not fail_incomplete else partial)
            if isinstance(validated, int) and validated != 0:
                exit_code = validated

        ctx.exit(exit_code if exit_code is not None else 0)

    except (PepperException, PepperAuthException, PepperArgumentsException) as exc:
        click.echo(f"Pepper error: {exc}", err=True)
        ctx.exit(1)
    except KeyboardInterrupt:
        ctx.exit(0)
    except Exception as exc:
        click.echo(str(exc), err=True)
        click.echo(
            "Uncaught Pepper error (increase verbosity for full traceback).",
            err=True,
        )
        logger.debug("Uncaught traceback:", exc_info=True)
        ctx.exit(1)


if __name__ == "__main__":
    cli()
