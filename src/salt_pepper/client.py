"""
A Python library for working with Salt's REST API
(specifically the rest_cherrypy and rest_tornado netapi modules).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from salt_pepper.exceptions import PepperException

logger = logging.getLogger(__name__)


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

    Usage::

        api = Pepper("https://localhost:8000")
        api.login("saltdev", "saltdev", "pam")
        api.low([{"client": "local", "tgt": "*", "fun": "test.ping"}])
    """

    def __init__(
        self,
        api_url: str = "https://localhost:8000",
        debug_http: bool = False,
        ignore_ssl_errors: bool = False,
    ) -> None:
        """
        Initialize the class with the URL of the salt-api service.

        Args:
            api_url: URL of the salt-api (including protocol and port).
            debug_http: Enable HTTP-level debug logging.
            ignore_ssl_errors: Skip SSL certificate verification.
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
        self.auth: dict[str, Any] = {}
        self.salt_version: str | None = None

    # ------------------------------------------------------------------
    # Low-level HTTP helpers
    # ------------------------------------------------------------------

    def _session(self) -> requests.Session:
        """Build a requests.Session with standard headers."""
        s = requests.Session()
        s.verify = self.ssl_verify
        s.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        if self.auth and self.auth.get("token"):
            s.headers["X-Auth-Token"] = str(self.auth["token"])
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
        Authenticate with salt-api and return user permissions + token.

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
        return self.auth

    # ------------------------------------------------------------------
    # Client convenience methods
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
