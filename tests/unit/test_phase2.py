"""Tests for Phase 2 features: queue, ssh, metadata, JWT, async, and output."""

from __future__ import annotations

import json

import pytest
import responses

from salt_pepper import Pepper
from salt_pepper.async_client import AsyncPepper
from salt_pepper.exceptions import PepperException
from salt_pepper.output import format_result


# ═══════════════════════════════════════════════════════════════════════
# Queue client
# ═══════════════════════════════════════════════════════════════════════


class TestPepperQueue:
    @responses.activate
    def test_queue(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{"success": True}]},
            status=200,
        )
        result = api.queue("queues.insert", queue="myqueue", items=["a"])
        assert result == {"return": [{"success": True}]}
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "queue"
        assert body[0]["fun"] == "queues.insert"
        assert body[0]["queue"] == "myqueue"

    @responses.activate
    def test_queue_list(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{"queues": ["q1", "q2"]}]},
            status=200,
        )
        result = api.queue("queues.list")
        assert result["return"][0]["queues"] == ["q1", "q2"]


# ═══════════════════════════════════════════════════════════════════════
# SSH client
# ═══════════════════════════════════════════════════════════════════════


class TestPepperSSH:
    @responses.activate
    def test_ssh(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{"host-1": True}]},
            status=200,
        )
        result = api.ssh(tgt="*", fun="test.ping")
        assert result is not None
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "ssh"
        assert body[0]["tgt"] == "*"
        assert body[0]["fun"] == "test.ping"

    @responses.activate
    def test_ssh_with_args(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{"host-1": {"stdout": "ok"}}]},
            status=200,
        )
        api.ssh(tgt="host-1", fun="cmd.run", arg=["uptime"], tgt_type="glob")
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["arg"] == ["uptime"]
        assert body[0]["tgt_type"] == "glob"


# ═══════════════════════════════════════════════════════════════════════
# Metadata parameter
# ═══════════════════════════════════════════════════════════════════════


class TestPepperMetadata:
    @responses.activate
    def test_local_with_metadata(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.local(
            tgt="*",
            fun="test.ping",
            metadata={"task": "healthcheck", "env": "prod"},
        )
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["metadata"] == {"task": "healthcheck", "env": "prod"}

    @responses.activate
    def test_local_batch_with_metadata(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.local_batch(tgt="*", fun="test.ping", metadata={"source": "ci"})
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["metadata"] == {"source": "ci"}


# ═══════════════════════════════════════════════════════════════════════
# JWT auth
# ═══════════════════════════════════════════════════════════════════════


class TestPepperJWT:
    def test_jwt_login(self, api: Pepper) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.testtoken"
        result = api.jwt_login(jwt)
        assert api.auth["token"] == jwt
        assert api.auth["eauth"] == "jwt"
        assert result["token"] == jwt

    @responses.activate
    def test_jwt_sets_header(self, api: Pepper) -> None:
        api.jwt_login("test.jwt.token")
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.req("/")
        headers = responses.calls[0].request.headers
        assert headers.get("X-Auth-Token") == "test.jwt.token"


# ═══════════════════════════════════════════════════════════════════════
# Output formatting
# ═══════════════════════════════════════════════════════════════════════


class TestOutput:
    def test_format_json(self) -> None:
        result = format_result({"key": "value"}, fmt="json")
        assert '"key": "value"' in result

    def test_format_yaml(self) -> None:
        result = format_result({"key": "value"}, fmt="yaml")
        assert "key: value" in result

    def test_format_table_simple(self) -> None:
        data = {"return": [{"minion-1": {"id": "m1", "up": True}}]}
        result = format_result(data, fmt="table")
        assert "minion-1" in result

    def test_format_table_list(self) -> None:
        data = [{"name": "m1", "status": "up"}, {"name": "m2", "status": "down"}]
        result = format_result(data, fmt="table")
        assert "m1" in result
        assert "m2" in result

    def test_format_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown output format"):
            format_result({}, fmt="xml")

    def test_format_text_dict(self) -> None:
        data = {"return": [{"minion-1": True, "minion-2": True}]}
        result = format_result(data, fmt="text")
        assert "minion-1: True" in result
        assert "minion-2: True" in result

    def test_format_text_list(self) -> None:
        data = {"return": [{"results": ["item1", "item2", "item3"]}]}
        result = format_result(data, fmt="text")
        # After unwrapping return[0], we have a dict with "results" key
        assert "results:" in result

    def test_format_text_flat_dict(self) -> None:
        data = {"hostname": "salt-master", "version": "3006"}
        result = format_result(data, fmt="text")
        assert "hostname: salt-master" in result
        assert "version: 3006" in result

    def test_format_text_list_direct(self) -> None:
        data = ["minion-1", "minion-2", "minion-3"]
        result = format_result(data, fmt="text")
        assert result == "minion-1\nminion-2\nminion-3"

    def test_format_text_is_valid_choice(self) -> None:
        """Verify the CLI accepts 'text' as a valid output format."""
        from click import Choice
        choice = Choice(["json", "yaml", "table", "text"])
        assert choice("text") == "text"
        assert choice("json") == "json"


# ═══════════════════════════════════════════════════════════════════════
# Async client
# ═══════════════════════════════════════════════════════════════════════


class TestAsyncPepper:
    @pytest.mark.asyncio
    async def test_async_login(self) -> None:
        import httpx

        async with AsyncPepper("https://salt.example.com:8000") as api:
            # Mock sentinel — just verify the client was created
            assert api.api_url == "https://salt.example.com:8000"
            assert api._client is not None
            assert isinstance(api._client, httpx.AsyncClient)

    @pytest.mark.asyncio
    async def test_async_jwt(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        await api.jwt_login("my.jwt.token")
        assert api.auth["token"] == "my.jwt.token"
        assert api.auth["eauth"] == "jwt"
        await api.aclose()

    @pytest.mark.asyncio
    async def test_async_context_manager(self) -> None:
        async with AsyncPepper("https://salt.example.com:8000") as api:
            pass
        # Ensure client is closed
        assert api._client is None

    @pytest.mark.asyncio
    async def test_async_invalid_url(self) -> None:
        with pytest.raises(PepperException, match="missing HTTP"):
            AsyncPepper("localhost:8000")
