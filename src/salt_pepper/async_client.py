"""
Async client for Salt's REST API via ``httpx``.

Provides the same interface as :class:`salt_pepper.client.Pepper` but
with ``async`` methods.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from salt_pepper.client import construct_url, make_lowstate
from salt_pepper.exceptions import PepperException

logger = logging.getLogger(__name__)


class AsyncPepper:
    """
    Async HTTP client for salt-api (uses ``httpx.AsyncClient``).

    Usage::

        async with AsyncPepper("https://localhost:8000") as api:
            await api.login("saltdev", "saltdev", "pam")
            result = await api.local(tgt="*", fun="test.ping")
    """

    def __init__(
        self,
        api_url: str = "https://localhost:8000",
        debug_http: bool = False,
        ignore_ssl_errors: bool = False,
    ) -> None:
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
        self._client: httpx.AsyncClient | None = None

    # ── Context manager ──────────────────────────────────────────

    async def __aenter__(self) -> AsyncPepper:
        await self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── HTTP helpers ─────────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }
            if self.auth and self.auth.get("token"):
                headers["X-Auth-Token"] = str(self.auth["token"])

            self._client = httpx.AsyncClient(
                verify=self.ssl_verify,
                headers=headers,
            )
        return self._client

    def _update_headers(self) -> None:
        """Refresh auth headers on the existing client (if open)."""
        if not self._client:
            return
        if self.auth and self.auth.get("token"):
            self._client.headers["X-Auth-Token"] = str(self.auth["token"])
        elif "X-Auth-Token" in self._client.headers:
            del self._client.headers["X-Auth-Token"]

    async def _request(
        self,
        path: str,
        data: dict | None = None,
        stream: bool = False,
        method: str = "POST",
    ) -> httpx.Response:
        url = construct_url(self.api_url, path)  # reuse the URL builder
        client = await self._ensure_client()

        body = json.dumps(data) if data is not None else None

        try:
            resp = await client.request(method, url, content=body)

            if resp.status_code == 401:
                raise PepperException("Authentication denied")
            if resp.status_code == 500:
                raise PepperException("Server error")
            if resp.status_code == 404:
                raise PepperException(
                    f"Resource not found: {path}"
                )

            if not self.salt_version and "x-salt-version" in resp.headers:
                self.salt_version = resp.headers["x-salt-version"]

            resp.raise_for_status()

        except (httpx.HTTPError, PepperException) as exc:
            logger.debug("Async request failed", exc_info=True)
            if isinstance(exc, PepperException):
                raise
            raise PepperException(str(exc)) from exc

        return resp

    async def _request_json(
        self, path: str, data: Any = None
    ) -> Any:
        resp = await self._request(path, data=data)
        try:
            return resp.json()
        except ValueError as exc:
            raise PepperException(
                "Unable to parse the server response."
            ) from exc

    async def req(self, path: str, data: Any = None) -> Any:
        """Send a POST request and return parsed JSON."""
        return await self._request_json(path, data)

    async def req_get(self, path: str) -> Any:
        """Send a GET request and return parsed JSON."""
        client = await self._ensure_client()
        url = construct_url(self.api_url, path)
        try:
            resp = await client.get(url)
            if resp.status_code == 401:
                raise PepperException("Authentication denied")
            if resp.status_code == 500:
                raise PepperException("Server error")
            if resp.status_code == 404:
                raise PepperException(
                    f"Resource not found: {path}"
                )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            if isinstance(exc, PepperException):
                raise
            raise PepperException(str(exc)) from exc
        return resp.json()

    async def req_stream(self, path: str) -> httpx.Response:
        """Open a streaming GET connection (e.g. ``/events``)."""
        if not self.auth or not self.auth.get("token"):
            raise PepperException("Authentication required")
        return await self._request(path, stream=True, method="GET")

    # ── Auth ────────────────────────────────────────────────────

    async def _send_auth(self, path: str, **kwargs: Any) -> Any:
        return await self._request_json(path, kwargs)

    async def login(
        self,
        username: str | None = None,
        password: str | None = None,
        eauth: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        creds: dict[str, Any] = {}
        if username is not None:
            creds["username"] = username
        if password is not None:
            creds["password"] = password
        if eauth is not None:
            creds["eauth"] = eauth
        creds.update(kwargs)

        result = await self._send_auth("/login", **creds)
        self.auth = result.get("return", [{}])[0]
        self._update_headers()
        return self.auth

    async def token(self, **kwargs: Any) -> dict[str, Any]:
        self.auth = (await self._send_auth("/token", **kwargs))[0]
        self._update_headers()
        return self.auth

    async def jwt_login(self, jwt_token: str) -> dict[str, Any]:
        """Authenticate with a JWT token (rest_tornado only)."""
        self.auth = {"token": jwt_token, "eauth": "jwt"}
        self._update_headers()
        return self.auth

    # ── Client methods ──────────────────────────────────────────

    async def low(
        self, lowstate: list[dict], path: str = "/"
    ) -> Any:
        return await self._request_json(path, lowstate)

    async def local(
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
        low = make_lowstate(
            "local", tgt, fun,
            arg=arg, kwarg=kwarg, expr_form=expr_form,
            tgt_type=tgt_type, timeout=timeout, ret=ret,
            metadata=metadata,
        )
        return await self.low([low])

    async def local_async(
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
        low = make_lowstate(
            "local_async", tgt, fun,
            arg=arg, kwarg=kwarg, expr_form=expr_form,
            tgt_type=tgt_type, timeout=timeout, ret=ret,
            metadata=metadata,
        )
        return await self.low([low])

    async def local_batch(
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
        low = make_lowstate(
            "local_batch", tgt, fun,
            arg=arg, kwarg=kwarg, expr_form=expr_form,
            tgt_type=tgt_type, batch=batch, ret=ret,
            metadata=metadata,
        )
        return await self.low([low])

    async def ssh(
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
        low = make_lowstate(
            "ssh", tgt, fun,
            arg=arg, kwarg=kwarg, expr_form=expr_form,
            tgt_type=tgt_type, timeout=timeout, ret=ret,
            metadata=metadata,
        )
        return await self.low([low])

    async def queue(
        self,
        fun: str,
        arg: list | None = None,
        **kwargs: Any,
    ) -> Any:
        low: dict[str, Any] = {"client": "queue", "fun": fun}
        if arg:
            low["arg"] = arg
        low.update(kwargs)
        return await self.low([low])

    async def lookup_jid(self, jid: str) -> Any:
        return await self.runner("jobs.lookup_jid", jid=jid)

    async def runner(
        self,
        fun: str,
        arg: list | None = None,
        **kwargs: Any,
    ) -> Any:
        low: dict[str, Any] = {"client": "runner", "fun": fun}
        if arg:
            low["arg"] = arg
        low.update(kwargs)
        return await self.low([low])

    async def wheel(
        self,
        fun: str,
        arg: list | None = None,
        kwarg: dict | None = None,
        **kwargs: Any,
    ) -> Any:
        low: dict[str, Any] = {"client": "wheel", "fun": fun}
        if arg:
            low["arg"] = arg
        if kwarg:
            low["kwarg"] = kwarg
        low.update(kwargs)
        return await self.low([low])
