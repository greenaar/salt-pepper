"""Unit tests for salt-pepper with mocked HTTP."""

from __future__ import annotations

import json

import pytest
import responses

from salt_pepper import Pepper, PepperException


# ── Construction ────────────────────────────────────────────────────


class TestPepperInit:
    def test_default_url(self) -> None:
        p = Pepper()
        assert p.api_url == "https://localhost:8000"

    def test_custom_url(self) -> None:
        p = Pepper("https://salt.example.com:8080")
        assert p.api_url == "https://salt.example.com:8080"

    def test_invalid_url_no_scheme(self) -> None:
        with pytest.raises(PepperException, match="missing HTTP"):
            Pepper("localhost:8000")

    def test_ssl_verify_default(self) -> None:
        p = Pepper()
        assert p.ssl_verify is True

    def test_ignore_ssl(self) -> None:
        p = Pepper(ignore_ssl_errors=True)
        assert p.ssl_verify is False

    def test_debug_http_default(self) -> None:
        p = Pepper()
        assert p.debug_http is False


# ── Auth (login) ────────────────────────────────────────────────────


class TestPepperLogin:
    @responses.activate
    def test_login_success(self, api: Pepper) -> None:
        token_data = {
            "return": [
                {
                    "token": "abc123",
                    "expire": 9999999999.0,
                    "user": "testuser",
                    "eauth": "pam",
                    "perms": ["test.*"],
                }
            ]
        }

        responses.post(
            "https://salt-api.example.com:8000/login",
            json=token_data,
            status=200,
        )

        result = api.login(username="testuser", password="secret", eauth="pam")
        assert result["token"] == "abc123"
        assert result["user"] == "testuser"
        assert api.auth["token"] == "abc123"

    @responses.activate
    def test_login_failure(self, api: Pepper) -> None:
        responses.post(
            "https://salt-api.example.com:8000/login",
            status=401,
        )
        with pytest.raises(PepperException, match="Authentication denied"):
            api.login(username="bad", password="wrong", eauth="pam")

    @responses.activate
    def test_token_auth(self, api: Pepper) -> None:
        token_data = [{"token": "xyz789", "expire": 9999999999.0}]
        responses.post(
            "https://salt-api.example.com:8000/token",
            json=token_data,
            status=200,
        )
        result = api.token(username="user", password="pass", eauth="pam")
        assert result["token"] == "xyz789"
        assert api.auth["token"] == "xyz789"


# ── Client methods ──────────────────────────────────────────────────


class TestPepperLocal:
    @responses.activate
    def test_local_basic(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        expected_return = {"return": [{"minion-1": True}]}

        responses.post(
            "https://salt-api.example.com:8000/",
            json=expected_return,
            status=200,
        )

        result = api.local(tgt="*", fun="test.ping")
        assert result == expected_return

        # Verify the request body
        req = responses.calls[0].request
        body = json.loads(req.body)
        assert body[0]["client"] == "local"
        assert body[0]["tgt"] == "*"
        assert body[0]["fun"] == "test.ping"
        assert body[0]["expr_form"] == "glob"

    @responses.activate
    def test_local_with_args(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        expected_return = {"return": [{"minion-1": {"stdout": "ok"}}]}

        responses.post(
            "https://salt-api.example.com:8000/",
            json=expected_return,
            status=200,
        )

        result = api.local(tgt="*", fun="cmd.run", arg=["uptime"])
        assert result == expected_return

        req = responses.calls[0].request
        body = json.loads(req.body)
        assert body[0]["arg"] == ["uptime"]

    @responses.activate
    def test_local_with_tgt_type(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )

        api.local(tgt="G@os:Ubuntu", fun="test.ping", tgt_type="compound")
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["expr_form"] == "glob"
        assert body[0]["tgt_type"] == "compound"


class TestPepperLocalAsync:
    @responses.activate
    def test_local_async(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        expected_return = {"return": [{"jid": "2025090512000012345", "minions": ["minion-1"]}]}

        responses.post(
            "https://salt-api.example.com:8000/",
            json=expected_return,
            status=200,
        )

        result = api.local_async(tgt="*", fun="state.sls", arg=["webserver"])
        assert result == expected_return
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "local_async"


class TestPepperLocalBatch:
    @responses.activate
    def test_local_batch(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )

        api.local_batch(tgt="*", fun="test.ping", batch="25%")
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "local_batch"
        assert body[0]["batch"] == "25%"


class TestPepperRunner:
    @responses.activate
    def test_runner(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        expected_return = {"return": [{"jobs": []}]}

        responses.post(
            "https://salt-api.example.com:8000/",
            json=expected_return,
            status=200,
        )

        result = api.runner("jobs.list_jobs")
        assert result == expected_return

        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "runner"
        assert body[0]["fun"] == "jobs.list_jobs"

    @responses.activate
    def test_runner_with_kwargs(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )

        api.runner("jobs.lookup_jid", jid="12345")
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["jid"] == "12345"


class TestPepperWheel:
    @responses.activate
    def test_wheel(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        expected_return = {"return": [{"data": {"return": {"minions": ["m1"]}}}]}

        responses.post(
            "https://salt-api.example.com:8000/",
            json=expected_return,
            status=200,
        )

        result = api.wheel("key.list_all")
        assert result == expected_return

        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "wheel"
        assert body[0]["fun"] == "key.list_all"


class TestPepperLookupJid:
    @responses.activate
    def test_lookup_jid(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{"minion-1": True}]},
            status=200,
        )

        result = api.lookup_jid("2025090512000012345")
        assert result is not None

        body = json.loads(responses.calls[0].request.body)
        assert body[0]["fun"] == "jobs.lookup_jid"
        assert body[0]["jid"] == "2025090512000012345"


class TestPepperLow:
    @responses.activate
    def test_low(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        lowstate = [{"client": "local", "tgt": "*", "fun": "test.ping"}]
        expected = {"return": [{"minion-1": True}]}

        responses.post(
            "https://salt-api.example.com:8000/",
            json=expected,
            status=200,
        )

        result = api.low(lowstate)
        assert result == expected

    @responses.activate
    def test_low_custom_path(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        responses.post(
            "https://salt-api.example.com:8000/run",
            json={"return": [{}]},
            status=200,
        )

        api.low([{"client": "local", "tgt": "*", "fun": "test.ping"}], path="/run")
        assert "/run" in responses.calls[0].request.url


# ── HTTP helpers ────────────────────────────────────────────────────


class TestPepperReq:
    @responses.activate
    def test_req_get(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        responses.get(
            "https://salt-api.example.com:8000/keys",
            json={"return": [{"minions": ["m1", "m2"]}]},
            status=200,
        )
        result = api.req_get("/keys")
        assert result["return"][0]["minions"] == ["m1", "m2"]

    @responses.activate
    def test_req_401(self, api: Pepper) -> None:
        responses.post(
            "https://salt-api.example.com:8000/",
            status=401,
        )
        with pytest.raises(PepperException, match="Authentication denied"):
            api.req("/", [{"client": "local", "tgt": "*", "fun": "test.ping"}])

    @responses.activate
    def test_req_500(self, api: Pepper) -> None:
        responses.post(
            "https://salt-api.example.com:8000/",
            status=500,
        )
        with pytest.raises(PepperException, match="Server error"):
            api.req("/")


class TestPepperStream:
    @responses.activate
    def test_req_stream(self, api: Pepper) -> None:
        api.auth = {"token": "test123"}
        responses.get(
            "https://salt-api.example.com:8000/events",
            body=b'data: {"tag": "foo", "data": {}}\n\n',
            status=200,
            content_type="text/event-stream",
        )
        resp = api.req_stream("/events")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("Content-Type", "")

    @responses.activate
    def test_req_stream_no_auth(self, api: Pepper) -> None:
        with pytest.raises(PepperException, match="Authentication required"):
            api.req_stream("/events")


# ── Auth header propagation ─────────────────────────────────────────


class TestAuthHeaders:
    @responses.activate
    def test_no_x_auth_token_for_cherrypy(self, api: Pepper) -> None:
        """CherryPy backend authenticates via session cookie, not X-Auth-Token."""
        api.auth = {"token": "mysecrettoken"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.req("/")
        req_headers = responses.calls[0].request.headers
        # X-Auth-Token should NOT be present — CherryPy uses session_id cookie
        assert "X-Auth-Token" not in req_headers

    @responses.activate
    def test_content_type(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.req("/")
        req_headers = responses.calls[0].request.headers
        assert req_headers.get("Content-Type") == "application/json"
        assert req_headers.get("Accept") == "application/json"


# ── Alcali backend ──────────────────────────────────────────────────


class TestPepperAlcali:
    @responses.activate
    def test_alcali_login(self) -> None:
        api = Pepper("https://alcali.example.com:8443", backend="alcali")
        jwt_response = {
            "refresh": "eyJrefresh",
            "access": "eyJaccess",
            "username": "saltuser",
            "id": 2,
            "email": "robot@example.com",
            "is_staff": True,
        }
        responses.post(
            "https://alcali.example.com:8443/api/token/",
            json=jwt_response,
            status=200,
        )

        result = api.alcali_login("saltuser", "secret")
        assert result["access"] == "eyJaccess"
        assert api.auth["token"] == "eyJaccess"
        assert api.auth["_login_method"] == "alcali_jwt"

    @responses.activate
    def test_alcali_login_failure(self) -> None:
        api = Pepper("https://alcali.example.com:8443", backend="alcali")
        responses.post(
            "https://alcali.example.com:8443/api/token/",
            status=401,
        )
        with pytest.raises(PepperException, match="Alcali authentication denied"):
            api.alcali_login("bad", "creds")

    @responses.activate
    def test_alcali_login_sets_bearer(self) -> None:
        api = Pepper("https://alcali.example.com:8443", backend="alcali")
        responses.post(
            "https://alcali.example.com:8443/api/token/",
            json={"access": "my.jwt.token", "refresh": "r"},
            status=200,
        )
        api.alcali_login("u", "p")
        responses.get(
            "https://alcali.example.com:8443/api/keys/",
            json=[{"minion_id": "m1"}],
            status=200,
        )
        api.req_get("/api/keys/")
        req_headers = responses.calls[1].request.headers
        assert req_headers.get("Authorization") == "Bearer my.jwt.token"
