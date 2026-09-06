"""
A Python library for working with Salt's REST API
(specifically the rest_cherrypy and rest_tornado netapi modules,
and the Alcali Django REST API).
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Literal

import requests

from salt_pepper.exceptions import PepperException

logger = logging.getLogger(__name__)

BackendName = Literal["cherrypy", "alcali"]


def construct_url(api_url: str, path: str) -> str:
    """
    Build the full URL for a salt-api resource path.

    Ensures exactly one slash between ``api_url`` and ``path``.
    """
    base = api_url.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def make_lowstate(
    client: str,
    tgt: str,
    fun: str,
    arg: list | None = None,
    kwarg: dict | None = None,
    expr_form: str = "glob",
    tgt_type: str | None = None,
    timeout: int | None = None,
    ret: str | None = None,
    batch: str | None = None,
    metadata: dict | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a lowstate dict shared across local/ssh clients."""
    low: dict[str, Any] = {
        "client": client,
        "tgt": tgt,
        "fun": fun,
    }
    if arg:
        low["arg"] = arg
    if kwarg:
        low["kwarg"] = kwarg
    low["expr_form"] = expr_form
    if tgt_type:
        low["tgt_type"] = tgt_type
    if timeout is not None:
        low["timeout"] = timeout
    if batch:
        low["batch"] = batch
    if ret:
        low["ret"] = ret
    if metadata:
        low["metadata"] = metadata
    low.update(extra)
    return low


class Pepper:
    """
    A thin wrapper for making HTTP calls to the salt-api REST interface.

    Supports two backends:

    * ``cherrypy`` (default) — standard salt-api rest_cherrypy using
      CherryPy session cookies for authentication.
    * ``alcali`` — Alcali Django REST API using JWT Bearer tokens.

    Usage (cherrypy)::

        api = Pepper("https://localhost:8000")
        api.login("saltdev", "saltdev", "pam")
        api.low([{"client": "local", "tgt": "*", "fun": "test.ping"}])

    Usage (alcali)::

        api = Pepper("https://salt.example.com:8443", backend="alcali")
        api.alcali_login("saltdev", "secret")
        api.alcali_run("test.ping", tgt="*")
    """

    def __init__(
        self,
        api_url: str = "https://localhost:8000",
        debug_http: bool = False,
        ignore_ssl_errors: bool = False,
        backend: BackendName = "cherrypy",
        ssh_bastion: str | None = None,
        ssh_key: str | None = None,
        ssh_port: int = 22,
    ) -> None:
        """
        Initialize the class with the URL of the salt-api service.

        Args:
            api_url: URL of the salt-api (including protocol and port).
            debug_http: Enable HTTP-level debug logging.
            ignore_ssl_errors: Skip SSL certificate verification.
            backend: ``"cherrypy"`` (standard rest_cherrypy) or
                     ``"alcali"`` (Django REST API).
            ssh_bastion: SSH bastion host (``user@host``) to tunnel through.
            ssh_key: Path to SSH identity file for bastion auth.
            ssh_port: SSH port for the bastion (default: 22).
        """
        from urllib.parse import urlsplit

        split = urlsplit(api_url)
        if split.scheme not in ("http", "https"):
            raise PepperException(
                f"salt-api URL missing HTTP(s) protocol: {api_url}"
            )

        self.api_url = api_url
        self.debug_http = debug_http
        self.ssl_verify = not ignore_ssl_errors
        self.backend: BackendName = backend
        self.auth: dict[str, Any] = {}
        self.salt_version: str | None = None
        self._session_obj: requests.Session | None = None
        self.ssh_bastion: str | None = ssh_bastion
        self.ssh_key: str | None = ssh_key
        self.ssh_port: int = ssh_port
        self._tunnel_proc: subprocess.Popen[bytes] | None = None
        self._tunnel_local_port: int | None = None

        if self.ssh_bastion:
            self._start_ssh_tunnel()

    # ------------------------------------------------------------------
    # SSH bastion tunnel
    # ------------------------------------------------------------------

    def _start_ssh_tunnel(self) -> None:
        """Create an SSH local port forward through a bastion host.

        Spawns ``ssh -L`` in the background and rewrites ``api_url``
        to point at the local tunnel endpoint (``http://127.0.0.1:PORT``).
        The SSH connection itself provides encryption end-to-end.
        """
        import socket
        import subprocess
        from urllib.parse import urlparse

        # Allocate a free local port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        local_port = sock.getsockname()[1]
        sock.close()

        # Parse remote target from the original api_url
        parsed = urlparse(self.api_url)
        remote_host = parsed.hostname or "localhost"
        remote_port = parsed.port or (443 if parsed.scheme == "https" else 80)

        logger.info(
            "SSH tunnel: %s → 127.0.0.1:%d → %s:%d",
            self.ssh_bastion, local_port, remote_host, remote_port,
        )

        # Build SSH command
        cmd: list[str] = [
            "ssh", "-N",
            "-L", f"127.0.0.1:{local_port}:{remote_host}:{remote_port}",
            "-p", str(self.ssh_port),
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if self.ssh_key:
            cmd += ["-i", self.ssh_key]
        cmd.append(str(self.ssh_bastion))

        try:
            self._tunnel_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            raise PepperException(
                "SSH binary not found — install openssh-client or set "
                "PEPPER_SSH_BASTION to empty to skip tunnelling."
            )

        # Wait briefly for the tunnel to establish
        import time
        time.sleep(0.5)

        if self._tunnel_proc.poll() is not None:
            stderr = self._tunnel_proc.stderr.read() if self._tunnel_proc.stderr else b""
            self._tunnel_proc = None
            raise PepperException(
                f"SSH tunnel failed: {stderr.decode('utf-8', errors='replace').strip()}"
            )

        self._tunnel_local_port = local_port
        # Rewrite api_url to use the local tunnel (no SSL over loopback)
        self.api_url = f"http://127.0.0.1:{local_port}"

    def close(self) -> None:
        """Explicitly close the SSH tunnel and HTTP session."""
        if self._tunnel_proc:
            logger.info("Stopping SSH tunnel")
            try:
                self._tunnel_proc.terminate()
                self._tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._tunnel_proc.kill()
                self._tunnel_proc.wait(timeout=2)
            except Exception:
                pass
            self._tunnel_proc = None
            self._tunnel_local_port = None
        if self._session_obj:
            self._session_obj.close()
            self._session_obj = None

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Low-level HTTP helpers
    # ------------------------------------------------------------------

    def _session(self) -> requests.Session:
        """Build/return a persistent requests.Session with standard headers."""
        if self._session_obj is None:
            self._session_obj = requests.Session()
        s = self._session_obj
        s.verify = self.ssl_verify
        s.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        # CherryPy backend: auth is via session_id cookie (set by login),
        # not the X-Auth-Token header. The requests.Session stores cookies
        # automatically, so no token header is needed.
        # Exception: rest_tornado JWT mode uses X-Auth-Token.
        if self.auth.get("_login_method") == "jwt" and self.auth.get("token"):
            s.headers["X-Auth-Token"] = str(self.auth["token"])
        # Alcali backend: uses Authorization: Bearer header set by alcali_login().
        if self.backend == "alcali" and self.auth.get("token"):
            s.headers["Authorization"] = f"Bearer {self.auth['token']}"
        return s

    def _request(
        self,
        path: str,
        data: dict | None = None,
        stream: bool = False,
        method: str = "POST",
    ) -> requests.Response:
        """
        Send an HTTP request to salt-api.

        Args:
            path: URL path relative to api_url (e.g. ``/login``, ``/``).
            data: Optional POST body (will be JSON-serialised).
            stream: Whether to stream the response body.
            method: HTTP method (``"POST"`` or ``"GET"``).

        Returns:
            The requests.Response object.

        Raises:
            PepperException: On 401, 500 or network errors.
        """
        url = self._construct_url(path)
        sess = self._session()

        body = json.dumps(data) if data is not None else None

        try:
            resp = sess.request(method, url, data=body, stream=stream)

            if resp.status_code == 401:
                raise PepperException("Authentication denied", response_body=resp.text)
            if resp.status_code == 500:
                raise PepperException("Server error", response_body=resp.text)
            if resp.status_code == 404:
                raise PepperException(
                    f"Resource not found: {path}", response_body=resp.text,
                )

            # Capture salt-version header for feature detection
            if not self.salt_version and "x-salt-version" in resp.headers:
                self.salt_version = resp.headers["x-salt-version"]

            resp.raise_for_status()

        except requests.RequestException as exc:
            logger.debug("Request failed", exc_info=True)
            if isinstance(exc, PepperException):
                raise
            raise PepperException(str(exc)) from exc

        return resp

    def _request_json(
        self, path: str, data: Any = None
    ) -> Any:
        """Send a request and decode the JSON response."""
        resp = self._request(path, data=data)
        try:
            return resp.json()
        except ValueError as exc:
            raise PepperException(
                "Unable to parse the server response."
            ) from exc

    def req(self, path: str, data: Any = None) -> Any:
        """Send a request to salt-api and return parsed JSON.

        This is the primary low-level method used internally by all
        client methods.

        Args:
            path: URL path (e.g. ``/``, ``/run``).
            data: Optional POST payload.

        Returns:
            Parsed JSON response.
        """
        return self._request_json(path, data)

    def req_get(self, path: str) -> Any:
        """
        Send a GET request and return the JSON body.

        Args:
            path: URL path (e.g. ``/keys``).

        Returns:
            Parsed JSON response.
        """
        url = self._construct_url(path)
        sess = self._session()
        try:
            resp = sess.get(url)
            if resp.status_code == 401:
                raise PepperException("Authentication denied", response_body=resp.text)
            if resp.status_code == 500:
                raise PepperException("Server error", response_body=resp.text)
            if resp.status_code == 404:
                raise PepperException(
                    f"Resource not found: {path}", response_body=resp.text,
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            if isinstance(exc, PepperException):
                raise
            raise PepperException(str(exc)) from exc
        return resp.json()

    def req_stream(self, path: str) -> requests.Response:
        """
        Open a streaming connection to salt-api (e.g. ``/events``).

        Returns:
            A ``requests.Response`` with ``stream=True``. Close it
            after use.
        """
        if not self.auth or not self.auth.get("token"):
            raise PepperException("Authentication required")
        return self._request(path, stream=True, method="GET")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _send_auth(self, path: str, **kwargs: Any) -> Any:
        return self._request_json(path, kwargs)

    def login(
        self,
        username: str | None = None,
        password: str | None = None,
        eauth: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Authenticate with salt-api (rest_cherrypy) and return
        user permissions + token.

        Uses CherryPy session cookies for subsequent requests.
        The ``session_id`` cookie from the login response is
        automatically preserved by ``requests.Session``.

        Returns:
            The ``return[0]`` dict (contains ``token``, ``expire``,
            ``perms``, ``user``, ``eauth``).
        """
        creds: dict[str, Any] = {}
        if username is not None:
            creds["username"] = username
        if password is not None:
            creds["password"] = password
        if eauth is not None:
            creds["eauth"] = eauth
        creds.update(kwargs)

        result = self._send_auth("/login", **creds)
        self.auth = result.get("return", [{}])[0]
        if "token" in self.auth:
            self.auth["_login_method"] = "session"
        return self.auth

    def token(self, **kwargs: Any) -> dict[str, Any]:
        """
        Get an eauth token for use with the ``/run`` URL.

        Returns:
            The first element of the response (a dict with
            ``token``, ``expire``, etc.).
        """
        self.auth = self._send_auth("/token", **kwargs)[0]
        self.auth["_login_method"] = "token"
        return self.auth

    def jwt_login(
        self,
        jwt_token: str,
    ) -> dict[str, Any]:
        """
        Authenticate using a pre-obtained JWT token.

        Salt's rest_tornado module supports JWT (JSON Web Token)
        authentication when configured with ``auth.method: jwt``.
        This sets the token directly on the session without a separate
        ``/login`` call — the token is sent as ``X-Auth-Token``
        on every request.

        Args:
            jwt_token: The JWT string to use as the auth token.

        Returns:
            A minimal auth dict carrying the JWT.
        """
        self.auth = {
            "token": jwt_token,
            "eauth": "jwt",
            "_login_method": "jwt",
        }
        self._update_auth_headers()
        return self.auth

    # ------------------------------------------------------------------
    # Alcali backend (JWT Bearer auth)
    # ------------------------------------------------------------------

    def alcali_login(
        self,
        username: str,
        password: str,
    ) -> dict[str, Any]:
        """
        Authenticate with the Alcali Django REST API using JWT.

        Obtains a JWT token from ``/api/token/`` and sets the
        ``Authorization: Bearer`` header on the session. Subsequent
        requests to non-``/api/run/`` endpoints (minions, keys, etc.)
        use a standard JSON POST.

        Args:
            username: Alcali username.
            password: Alcali password.

        Returns:
            The full JWT response (includes ``access`` and ``refresh``
            tokens, ``username``, ``id``, ``email``, ``is_staff``).

        Raises:
            PepperException: On authentication failure.
        """
        url = self._construct_url("/api/token/")
        sess = self._session()

        try:
            resp = sess.post(
                url,
                json={"username": username, "password": password},
            )
            if resp.status_code == 401:
                raise PepperException(
                    "Alcali authentication denied", response_body=resp.text,
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            if isinstance(exc, PepperException):
                raise
            raise PepperException(f"Alcali authentication failed: {exc}") from exc

        data = resp.json()
        self.auth = {
            "token": data["access"],
            "refresh_token": data.get("refresh"),
            "username": data.get("username"),
            "user_id": data.get("id"),
            "email": data.get("email"),
            "is_staff": data.get("is_staff", False),
            "eauth": "jwt",
            "_login_method": "alcali_jwt",
        }
        # Set the Bearer header on our session
        self._update_auth_headers()
        return data

    def _update_auth_headers(self) -> None:
        """Refresh session headers from current auth state."""
        if self._session_obj is not None:
            s = self._session_obj
            # Clear stale Bearer header
            s.headers.pop("Authorization", None)
            s.headers.pop("X-Auth-Token", None)

            if self.backend == "alcali" and self.auth.get("token"):
                s.headers["Authorization"] = f"Bearer {self.auth['token']}"

    def alcali_run(
        self,
        command: str,
        tgt: str = "*",
        client: str = "local",
        raw: bool = True,
    ) -> Any:
        """
        Execute a Salt command through the Alcali ``/api/run/`` endpoint.

        The command is sent as form-encoded data (matching the Alcali
        Vue.js frontend's format).

        Args:
            command: Salt function to execute (e.g. ``"test.ping"``).
            tgt: Target specification (default ``"*"``).
            client: Salt client (``"local"`` or ``"runner"``).
            raw: If True, request raw unformatted output.

        Returns:
            The parsed JSON response from Alcali.

        Raises:
            PepperException: On failure.
        """
        url = self._construct_url("/api/run/")
        sess = self._session()

        # Build the command string in the format Alcali expects
        if client == "runner":
            cmd_str = command
        elif tgt == "*":
            cmd_str = f"'{command}'" if " " in command else command
        else:
            # Alcali CLI-style: salt target function args
            cmd_str = command

        form_data: dict[str, str] = {"command": cmd_str}
        if raw:
            form_data["raw"] = "true"
        if client == "local":
            form_data["tgt"] = tgt

        try:
            resp = sess.post(url, data=form_data)
            if resp.status_code == 401:
                raise PepperException(
                    "Authentication denied",
                    response_body=resp.text,
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            if isinstance(exc, PepperException):
                raise
            raise PepperException(str(exc)) from exc

        try:
            return resp.json()
        except ValueError:
            # Alcali may return raw text on 500
            return {"error": resp.text}

    # ------------------------------------------------------------------
    # Client convenience methods (cherrypy backend)
    # ------------------------------------------------------------------

    def low(self, lowstate: list[dict], path: str = "/") -> Any:
        """
        Execute a command through salt-api and return the response.

        Args:
            lowstate: A list of lowstate dictionaries.
            path: URL path (default ``/`` or ``/run``).

        Returns:
            The raw server response.
        """
        return self._request_json(path, lowstate)

    # ── common lowstate builder ──

    @staticmethod
    def _make_lowstate(
        client: str,
        tgt: str,
        fun: str,
        arg: list | None = None,
        kwarg: dict | None = None,
        expr_form: str = "glob",
        tgt_type: str | None = None,
        timeout: int | None = None,
        ret: str | None = None,
        batch: str | None = None,
        metadata: dict | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Build a lowstate dict shared across local/ssh clients."""
        return make_lowstate(
            client, tgt, fun,
            arg=arg, kwarg=kwarg, expr_form=expr_form,
            tgt_type=tgt_type, timeout=timeout, ret=ret,
            batch=batch, metadata=metadata,
            **extra,
        )

    def local(
        self,
        tgt: str,
        fun: str,
        arg: list | None = None,
        kwarg: dict | None = None,
        expr_form: str = "glob",
        tgt_type: str | None = None,
        timeout: int | None = None,
        ret: str | None = None,
        metadata: dict | None = None,
    ) -> Any:
        """
        Run a command using the ``local`` client.

        Args:
            tgt: Target specification.
            fun: Salt function to execute.
            arg: Positional arguments.
            kwarg: Keyword arguments.
            expr_form: Target expression form (default ``glob``).
            tgt_type: Alias for ``expr_form`` (preferred in newer Salt).
            timeout: Timeout in seconds.
            ret: Returner name.
            metadata: Arbitrary dict attached to the job for external
                      systems (Salt >= 2017.7).
        """
        low = self._make_lowstate(
            "local", tgt, fun,
            arg=arg, kwarg=kwarg, expr_form=expr_form,
            tgt_type=tgt_type, timeout=timeout, ret=ret,
            metadata=metadata,
        )
        return self.low([low])

    def local_async(
        self,
        tgt: str,
        fun: str,
        arg: list | None = None,
        kwarg: dict | None = None,
        expr_form: str = "glob",
        tgt_type: str | None = None,
        timeout: int | None = None,
        ret: str | None = None,
        metadata: dict | None = None,
    ) -> Any:
        """Run a command using the ``local_async`` client."""
        low = self._make_lowstate(
            "local_async", tgt, fun,
            arg=arg, kwarg=kwarg, expr_form=expr_form,
            tgt_type=tgt_type, timeout=timeout, ret=ret,
            metadata=metadata,
        )
        return self.low([low])

    def local_batch(
        self,
        tgt: str,
        fun: str,
        arg: list | None = None,
        kwarg: dict | None = None,
        expr_form: str = "glob",
        tgt_type: str | None = None,
        batch: str = "50%",
        ret: str | None = None,
        metadata: dict | None = None,
    ) -> Any:
        """Run a command using the ``local_batch`` client."""
        low = self._make_lowstate(
            "local_batch", tgt, fun,
            arg=arg, kwarg=kwarg, expr_form=expr_form,
            tgt_type=tgt_type, batch=batch, ret=ret,
            metadata=metadata,
        )
        return self.low([low])

    def ssh(
        self,
        tgt: str,
        fun: str,
        arg: list | None = None,
        kwarg: dict | None = None,
        expr_form: str = "glob",
        tgt_type: str | None = None,
        timeout: int | None = None,
        ret: str | None = None,
        metadata: dict | None = None,
    ) -> Any:
        """Run a command using the ``ssh`` client (salt-ssh via salt-api)."""
        low = self._make_lowstate(
            "ssh", tgt, fun,
            arg=arg, kwarg=kwarg, expr_form=expr_form,
            tgt_type=tgt_type, timeout=timeout, ret=ret,
            metadata=metadata,
        )
        return self.low([low])

    def queue(
        self,
        fun: str,
        arg: list | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Run a command using the ``queue`` client.

        The queue client manages Salt's job queue (enqueue, list, etc.).

        Usage::

            pepper.queue("queues.insert", queue="myqueue", items=[...])
            pepper.queue("queues.list")
        """
        low: dict[str, Any] = {
            "client": "queue",
            "fun": fun,
        }
        if arg:
            low["arg"] = arg
        low.update(kwargs)
        return self.low([low])

    def lookup_jid(self, jid: str) -> Any:
        """
        Look up the results of a previously-submitted job.

        Wraps :meth:`runner` with ``jobs.lookup_jid``.
        """
        return self.runner("jobs.lookup_jid", jid=jid)

    def runner(
        self,
        fun: str,
        arg: list | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Run a command using the ``runner`` client.

        Usage::

            pepper.runner("jobs.lookup_jid", jid="12345")
        """
        low: dict[str, Any] = {
            "client": "runner",
            "fun": fun,
        }
        if arg:
            low["arg"] = arg
        low.update(kwargs)
        return self.low([low])

    def wheel(
        self,
        fun: str,
        arg: list | None = None,
        kwarg: dict | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Run a command using the ``wheel`` client.

        Usage::

            pepper.wheel("key.accept", match="myminion")
        """
        low: dict[str, Any] = {
            "client": "wheel",
            "fun": fun,
        }
        if arg:
            low["arg"] = arg
        if kwarg:
            low["kwarg"] = kwarg
        low.update(kwargs)
        return self.low([low])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _construct_url(self, path: str) -> str:
        """Build the full URL for a salt-api resource path."""
        return construct_url(self.api_url, path)
