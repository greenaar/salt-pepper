"""Comprehensive unit tests for salt-pepper — full coverage across all modules."""

from __future__ import annotations

import json
from typing import Any

import pytest
import responses

from salt_pepper import Pepper
from salt_pepper.client import construct_url, make_lowstate
from salt_pepper.exceptions import PepperArgumentsException, PepperException
from salt_pepper.output import _render_table, _format_table, format_result
from salt_pepper.retcode import PepperRetcode

# ═══════════════════════════════════════════════════════════════════════
# construct_url
# ═══════════════════════════════════════════════════════════════════════


class TestConstructUrl:
    def test_basic(self) -> None:
        assert construct_url("https://salt:8000", "/") == "https://salt:8000/"

    def test_trailing_slash_on_base(self) -> None:
        assert construct_url("https://salt:8000/", "/login") == "https://salt:8000/login"

    def test_no_leading_slash_on_path(self) -> None:
        assert construct_url("https://salt:8000", "login") == "https://salt:8000/login"

    def test_double_slash(self) -> None:
        assert construct_url("https://salt:8000/", "login") == "https://salt:8000/login"

    def test_custom_path(self) -> None:
        assert construct_url("http://localhost:8080", "/api/run/") == "http://localhost:8080/api/run/"


# ═══════════════════════════════════════════════════════════════════════
# make_lowstate
# ═══════════════════════════════════════════════════════════════════════


class TestMakeLowstate:
    def test_required_only(self) -> None:
        low = make_lowstate("local", "*", "test.ping")
        assert low == {
            "client": "local",
            "tgt": "*",
            "fun": "test.ping",
            "expr_form": "glob",
        }

    def test_all_options(self) -> None:
        low = make_lowstate(
            "local", "*", "test.ping",
            arg=["arg1"],
            kwarg={"key": "val"},
            expr_form="compound",
            tgt_type="compound",
            timeout=60,
            ret="redis_returner",
            batch="25%",
            metadata={"source": "ci"},
        )
        assert low["client"] == "local"
        assert low["tgt"] == "*"
        assert low["fun"] == "test.ping"
        assert low["arg"] == ["arg1"]
        assert low["kwarg"] == {"key": "val"}
        assert low["expr_form"] == "compound"
        assert low["tgt_type"] == "compound"
        assert low["timeout"] == 60
        assert low["ret"] == "redis_returner"
        assert low["batch"] == "25%"
        assert low["metadata"] == {"source": "ci"}

    def test_extra_kwargs(self) -> None:
        low = make_lowstate("local", "*", "my.func", queue="myqueue")
        assert low["queue"] == "myqueue"

    def test_zero_timeout_included(self) -> None:
        """timeout=0 is a valid value (no timeout)."""
        low = make_lowstate("local", "*", "test.ping", timeout=0)
        assert low["timeout"] == 0

    def test_empty_arg_excluded(self) -> None:
        low = make_lowstate("local", "*", "test.ping", arg=[])
        assert "arg" not in low

    def test_empty_kwarg_excluded(self) -> None:
        low = make_lowstate("local", "*", "test.ping", kwarg={})
        assert "kwarg" not in low


# ═══════════════════════════════════════════════════════════════════════
# Pepper._request — 404 and stream paths
# ═══════════════════════════════════════════════════════════════════════


class TestPepperRequestEdgeCases:
    @responses.activate
    def test_request_404(self, api: Pepper) -> None:
        responses.post(
            "https://salt-api.example.com:8000/nope",
            status=404,
        )
        api.auth = {"token": "t"}
        with pytest.raises(
            PepperException, match="Resource not found: /nope"
        ):
            api.req("/nope")

    @responses.activate
    def test_request_500_raises(self, api: Pepper) -> None:
        responses.post(
            "https://salt-api.example.com:8000/",
            status=500,
        )
        api.auth = {"token": "t"}
        with pytest.raises(PepperException, match="Server error"):
            api.req("/")

    @responses.activate
    def test_request_network_error(self, api: Pepper) -> None:
        """Non-HTTP error (connection refused, DNS failure, etc.)."""
        responses.post(
            "https://salt-api.example.com:8000/",
            body=requests.ConnectionError("Connection refused"),
        )
        api.auth = {"token": "t"}
        with pytest.raises(PepperException):
            api.req("/")

    @responses.activate
    def test_request_json_decode_error(self, api: Pepper) -> None:
        responses.post(
            "https://salt-api.example.com:8000/",
            body=b"not json",
            content_type="text/plain",
        )
        api.auth = {"token": "t"}
        with pytest.raises(
            PepperException, match="Unable to parse the server response"
        ):
            api.req("/")

    @responses.activate
    def test_salt_version_header(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
            headers={"x-salt-version": "3006.0"},
        )
        api.req("/")
        assert api.salt_version == "3006.0"

    @responses.activate
    def test_req_get_raises_404(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.get(
            "https://salt-api.example.com:8000/keys",
            status=404,
        )
        with pytest.raises(
            PepperException, match="Resource not found: /keys"
        ):
            api.req_get("/keys")

    @responses.activate
    def test_req_get_network_error(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.get(
            "https://salt-api.example.com:8000/keys",
            body=requests.ConnectionError("Oops"),
        )
        with pytest.raises(PepperException):
            api.req_get("/keys")

    @responses.activate
    def test_req_stream_unauthorized(self, api: Pepper) -> None:
        """req_stream raises when no auth token is set."""
        with pytest.raises(
            PepperException, match="Authentication required"
        ):
            api.req_stream("/events")

    @responses.activate
    def test_req_stream_authorized(self, api: Pepper) -> None:
        api.auth = {"token": "valid_token"}
        responses.get(
            "https://salt-api.example.com:8000/events",
            body=b'data: {"tag": "foo"}\n\n',
            status=200,
            content_type="text/event-stream",
        )
        resp = api.req_stream("/events")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════
# Pepper.close / __del__
# ═══════════════════════════════════════════════════════════════════════


class TestPepperLifecycle:
    def test_close_noop_when_no_tunnel(self) -> None:
        api = Pepper("https://salt.example.com:8000")
        api.close()  # should not raise
        assert api._tunnel_proc is None

    def test_close_with_session(self) -> None:
        api = Pepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        # force session creation
        _ = api._session()
        assert api._session_obj is not None
        api.close()
        assert api._session_obj is None


# ═══════════════════════════════════════════════════════════════════════
# Pepper client methods — fuller coverage
# ═══════════════════════════════════════════════════════════════════════


class TestPepperClientFull:
    @responses.activate
    def test_local_with_timeout_and_ret(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.local(tgt="*", fun="test.ping", timeout=30, ret="redis")
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["timeout"] == 30
        assert body[0]["ret"] == "redis"

    @responses.activate
    def test_local_batch_full(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.local_batch(
            tgt="*", fun="state.apply",
            arg=["webserver"],
            kwarg={"pillar": {"env": "prod"}},
            tgt_type="compound",
            batch="10",
            ret="postgres",
            metadata={"by": "ci"},
        )
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "local_batch"
        assert body[0]["arg"] == ["webserver"]
        assert body[0]["kwarg"] == {"pillar": {"env": "prod"}}
        assert body[0]["tgt_type"] == "compound"
        assert body[0]["batch"] == "10"
        assert body[0]["ret"] == "postgres"
        assert body[0]["metadata"] == {"by": "ci"}

    @responses.activate
    def test_local_async_full(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{"jid": "12345"}]},
            status=200,
        )
        result = api.local_async(
            tgt="*", fun="state.sls", arg=["webserver"],
            kwarg={"test": True}, tgt_type="list",
            timeout=120, ret="redis", metadata={"env": "staging"},
        )
        assert result["return"][0]["jid"] == "12345"
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "local_async"
        assert body[0]["arg"] == ["webserver"]

    @responses.activate
    def test_ssh_full(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.ssh(
            tgt="host-1", fun="cmd.run", arg=["uptime"],
            kwarg={"shell": "/bin/bash"}, tgt_type="glob",
            timeout=30, ret="mysql", metadata={"task": "health"},
        )
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "ssh"
        assert body[0]["arg"] == ["uptime"]
        assert body[0]["kwarg"] == {"shell": "/bin/bash"}
        assert body[0]["timeout"] == 30
        assert body[0]["metadata"] == {"task": "health"}

    @responses.activate
    def test_runner_with_arg(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.runner("state.orch", arg=["orchestration.my_orch"])
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["fun"] == "state.orch"
        assert body[0]["arg"] == ["orchestration.my_orch"]

    @responses.activate
    def test_wheel_with_kwarg(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.wheel("key.accept", match="my_minion")
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "wheel"
        assert body[0]["fun"] == "key.accept"
        assert body[0]["match"] == "my_minion"

    def test__construct_url(self, api: Pepper) -> None:
        assert api._construct_url("/run") == "https://salt-api.example.com:8000/run"

    @responses.activate
    def test_low_sends_raw_body(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        lowstate = [{"client": "local", "tgt": "*", "fun": "test.ping"}]
        api.low(lowstate)
        body = json.loads(responses.calls[0].request.body)
        assert body == lowstate

    @responses.activate
    def test_lookup_jid_delegates_to_runner(self, api: Pepper) -> None:
        api.auth = {"token": "t"}
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.lookup_jid("2025090512000012345")
        body = json.loads(responses.calls[0].request.body)
        assert body[0]["client"] == "runner"
        assert body[0]["fun"] == "jobs.lookup_jid"
        assert body[0]["jid"] == "2025090512000012345"


# ═══════════════════════════════════════════════════════════════════════
# Pepper auth edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestPepperAuthEdgeCases:
    @responses.activate
    def test_login_no_return_key(self, api: Pepper) -> None:
        """login() gracefully handles missing 'return' key."""
        responses.post(
            "https://salt-api.example.com:8000/login",
            json={},
            status=200,
        )
        result = api.login(username="u", password="p", eauth="pam")
        assert result == {}

    @responses.activate
    def test_login_with_token_expire(self, api: Pepper) -> None:
        responses.post(
            "https://salt-api.example.com:8000/login",
            json={
                "return": [{"token": "abc", "expire": 999999, "user": "me"}]
            },
            status=200,
        )
        result = api.login(username="u", password="p", eauth="pam", token_expire=300)
        assert result["token"] == "abc"
        # verify the token_expire was sent
        body = json.loads(responses.calls[0].request.body)
        assert body["token_expire"] == 300

    @responses.activate
    def test_token_method(self, api: Pepper) -> None:
        responses.post(
            "https://salt-api.example.com:8000/token",
            json=[{"token": "tkn", "expire": 999}],
            status=200,
        )
        result = api.token(username="u", password="p", eauth="pam")
        assert result["token"] == "tkn"
        assert api.auth["_login_method"] == "token"

    def test_jwt_login(self, api: Pepper) -> None:
        result = api.jwt_login("my.jwt")
        assert result["token"] == "my.jwt"
        assert result["eauth"] == "jwt"
        assert result["_login_method"] == "jwt"

    @responses.activate
    def test_jwt_sets_x_auth_token(self, api: Pepper) -> None:
        api.jwt_login("my.jwt")
        responses.post(
            "https://salt-api.example.com:8000/",
            json={"return": [{}]},
            status=200,
        )
        api.req("/")
        headers = responses.calls[0].request.headers
        assert headers.get("X-Auth-Token") == "my.jwt"

    def test_update_auth_headers_clears_bearer(self, api: Pepper) -> None:
        """_update_auth_headers removes stale Authorization header."""
        # Force session creation, then set a stale header
        sess = api._session()
        sess.headers["Authorization"] = "Bearer stale"
        api.backend = "cherrypy"
        api._update_auth_headers()
        assert "Authorization" not in sess.headers

    def test_update_auth_headers_sets_bearer_for_alcali(self, api: Pepper) -> None:
        api.backend = "alcali"
        api.auth = {"token": "my_jwt_token"}
        sess = api._session()
        # Clear whatever _session set up
        sess.headers.pop("Authorization", None)
        api._update_auth_headers()
        assert sess.headers.get("Authorization") == "Bearer my_jwt_token"


# ═══════════════════════════════════════════════════════════════════════
# Alcali backend — fuller coverage
# ═══════════════════════════════════════════════════════════════════════


class TestAlcaliFull:
    @responses.activate
    def test_alcali_login_network_error(self) -> None:
        api = Pepper("https://alcali.example.com:8443", backend="alcali")
        responses.post(
            "https://alcali.example.com:8443/api/token/",
            body=requests.ConnectionError("DNS failure"),
        )
        with pytest.raises(
            PepperException, match="Alcali authentication failed"
        ):
            api.alcali_login("u", "p")

    @responses.activate
    def test_alcali_run_local(self) -> None:
        api = Pepper("https://alcali.example.com:8443", backend="alcali")
        api.auth = {"token": "jwt", "_login_method": "alcali_jwt"}
        responses.post(
            "https://alcali.example.com:8443/api/run/",
            json={"return": [{"minion-1": True}]},
            status=200,
        )
        result = api.alcali_run("test.ping", tgt="*", client="local")
        assert result["return"][0]["minion-1"] is True

    @responses.activate
    def test_alcali_run_runner(self) -> None:
        api = Pepper("https://alcali.example.com:8443", backend="alcali")
        api.auth = {"token": "jwt", "_login_method": "alcali_jwt"}
        responses.post(
            "https://alcali.example.com:8443/api/run/",
            json={"return": [{"jobs": []}]},
            status=200,
        )
        result = api.alcali_run("jobs.list_jobs", client="runner")
        assert "jobs" in result["return"][0]

    @responses.activate
    def test_alcali_run_unauthorized(self) -> None:
        api = Pepper("https://alcali.example.com:8443", backend="alcali")
        api.auth = {"token": "bad", "_login_method": "alcali_jwt"}
        responses.post(
            "https://alcali.example.com:8443/api/run/",
            status=401,
        )
        with pytest.raises(
            PepperException, match="Authentication denied"
        ):
            api.alcali_run("test.ping")

    @responses.activate
    def test_alcali_run_valueerror_fallback(self) -> None:
        """Alcali may return non-JSON on server errors (200 but bad body)."""
        api = Pepper("https://alcali.example.com:8443", backend="alcali")
        api.auth = {"token": "jwt", "_login_method": "alcali_jwt"}
        responses.post(
            "https://alcali.example.com:8443/api/run/",
            body=b"Internal Server Error",
            content_type="text/plain",
            status=200,
        )
        result = api.alcali_run("test.ping")
        assert "error" in result
        assert "Internal Server Error" in result["error"]

    @responses.activate
    def test_alcali_login_401(self) -> None:
        api = Pepper("https://alcali.example.com:8443", backend="alcali")
        responses.post(
            "https://alcali.example.com:8443/api/token/",
            status=401,
        )
        with pytest.raises(
            PepperException, match="Alcali authentication denied"
        ):
            api.alcali_login("bad", "creds")


# ═══════════════════════════════════════════════════════════════════════
# Retcode validation — full coverage
# ═══════════════════════════════════════════════════════════════════════


# Minimal option-like object for PepperRetcode.validate
class _Opts:
    def __init__(self, **kwargs: bool) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestPepperRetcode:
    def _minion_result(self, success: dict[str, int]) -> list[dict]:
        """Build a salt-api return list like ``[{"minion-1": {"retcode": 0}, ...}]``."""
        return [
            {
                name: {"retcode": code}
                for name, code in success.items()
            }
        ]

    # ── validate dispatch ──

    def test_validate_no_flags_returns_zero(self) -> None:
        rc = PepperRetcode()
        assert rc.validate(_Opts(), {}) == 0

    def test_validate_fail_any_via_dispatch(self) -> None:
        rc = PepperRetcode()
        result = self._minion_result({"m1": 0, "m2": 1})
        assert rc.validate(_Opts(fail_any=True), result) == 1

    # ── _retcodes ──

    def test_retcodes_non_list_returns_empty(self) -> None:
        assert PepperRetcode._retcodes({}) == []

    def test_retcodes_empty_list_returns_empty(self) -> None:
        assert PepperRetcode._retcodes([]) == []

    def test_retcodes_non_dict_first_element(self) -> None:
        assert PepperRetcode._retcodes(["string"]) == []

    def test_retcodes_skips_non_dict_values(self) -> None:
        result = [{"m1": "not a dict"}]
        assert PepperRetcode._retcodes(result) == []

    def test_retcodes_skips_none_retcode(self) -> None:
        result = [{"m1": {"retcode": None}}]
        assert PepperRetcode._retcodes(result) == []

    def test_retcodes_mixed(self) -> None:
        result = [{"m1": {"retcode": 0}, "m2": {"retcode": 1}, "m3": {"retcode": None}}]
        assert PepperRetcode._retcodes(result) == [0, 1]

    # ── validate_fail_any ──

    def test_fail_any_all_zero(self) -> None:
        result = self._minion_result({"m1": 0, "m2": 0})
        assert PepperRetcode.validate_fail_any(result) == 0

    def test_fail_any_one_nonzero(self) -> None:
        result = self._minion_result({"m1": 0, "m2": 2})
        assert PepperRetcode.validate_fail_any(result) == 2

    def test_fail_any_no_retcodes(self) -> None:
        assert PepperRetcode.validate_fail_any([]) == 0

    # ── validate_fail_any_none ──

    def test_fail_any_none_all_zero(self) -> None:
        result = self._minion_result({"m1": 0})
        assert PepperRetcode.validate_fail_any_none(result) == 0

    def test_fail_any_none_nonzero(self) -> None:
        result = self._minion_result({"m1": 3})
        assert PepperRetcode.validate_fail_any_none(result) == 3

    def test_fail_any_none_no_retcodes_returns_minus1(self) -> None:
        assert PepperRetcode.validate_fail_any_none([]) == -1

    # ── validate_fail_all ──

    def test_fail_all_all_nonzero(self) -> None:
        result = self._minion_result({"m1": 1, "m2": 2})
        assert PepperRetcode.validate_fail_all(result) == 1

    def test_fail_all_some_zero(self) -> None:
        result = self._minion_result({"m1": 0, "m2": 1})
        assert PepperRetcode.validate_fail_all(result) == 0

    def test_fail_all_all_zero(self) -> None:
        result = self._minion_result({"m1": 0, "m2": 0})
        assert PepperRetcode.validate_fail_all(result) == 0

    def test_fail_all_no_retcodes(self) -> None:
        assert PepperRetcode.validate_fail_all([]) == 0

    # ── validate_fail_all_none ──

    def test_fail_all_none_all_nonzero(self) -> None:
        result = self._minion_result({"m1": 4, "m2": 5})
        assert PepperRetcode.validate_fail_all_none(result) == 4

    def test_fail_all_none_some_zero(self) -> None:
        result = self._minion_result({"m1": 0, "m2": 1})
        assert PepperRetcode.validate_fail_all_none(result) == 0

    def test_fail_all_none_no_retcodes_returns_minus1(self) -> None:
        assert PepperRetcode.validate_fail_all_none([]) == -1

    def test_fail_all_none_all_zero(self) -> None:
        result = self._minion_result({"m1": 0})
        assert PepperRetcode.validate_fail_all_none(result) == 0

    def test_fail_all_none_empty_after_filter(self) -> None:
        # all retcodes are None → filtered to empty list
        result = [{"m1": {"retcode": None}}]
        assert PepperRetcode.validate_fail_all_none(result) == -1


# ═══════════════════════════════════════════════════════════════════════
# Output formatters — full coverage
# ═══════════════════════════════════════════════════════════════════════


class TestFormatTableEdgeCases:
    def test_table_unwrap_return(self) -> None:
        """dict with 'return' key is unwrapped first."""
        data = {"return": [{"name": "m1", "status": "up"}]}
        result = format_result(data, fmt="table")
        assert "m1" in result
        assert "up" in result

    def test_table_unwrap_data(self) -> None:
        """dict with 'data' key is unwrapped after return."""
        data = {"data": [{"name": "m1", "status": "up"}]}
        result = format_result(data, fmt="table")
        assert "m1" in result

    def test_table_dict_of_dicts(self) -> None:
        """minion → result dict of dicts becomes a table with minion column."""
        data = {"m1": {"status": "up"}, "m2": {"status": "down"}}
        result = format_result(data, fmt="table")
        assert "m1" in result
        assert "m2" in result
        assert "minion" in result
        assert "up" in result
        assert "down" in result

    def test_table_dict_of_dicts_mixed_values(self) -> None:
        """Singleton dict of scalar becomes a two-column table."""
        data = {"m1": "just a string"}
        result = format_result(data, fmt="table")
        assert "m1" in result
        assert "just a string" in result
        assert "minion" in result
        assert "return" in result

    def test_table_empty_rows(self) -> None:
        assert _render_table([]) == "(empty)"

    def test_table_single_row(self) -> None:
        rows = [{"name": "m1", "status": "up"}]
        result = _render_table(rows)
        assert "+" in result
        assert "name" in result
        assert "m1" in result

    def test_table_empty_columns(self) -> None:
        assert _render_table([{}]) == "(empty)"

    def test_table_mixed_keys(self) -> None:
        rows = [
            {"name": "m1", "status": "up"},
            {"name": "m2", "extra": "info"},
        ]
        result = _render_table(rows)
        assert "name" in result
        assert "status" in result
        assert "extra" in result
        assert "(empty)" not in result

    def test_format_result_json_default_str(self) -> None:
        """default=str handles non-serialisable types."""
        result = format_result({"value": object()}, fmt="json")
        assert "value" in result

    def test_format_result_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown output format"):
            format_result({}, fmt="xml")

    def test_yaml_output(self) -> None:
        result = format_result({"a": 1, "b": 2}, fmt="yaml")
        assert "a:" in result
        assert "b:" in result

    @responses.activate
    def test_table_unwrap_both_return_and_data(self, api: Pepper) -> None:
        """return takes priority over data."""
        data = {"return": [{"minion": "val"}], "data": "should_ignore"}
        result = format_result(data, fmt="table")
        # return is a list of dicts → table format
        assert "minion" in result
        assert "val" in result


# ═══════════════════════════════════════════════════════════════════════
# Pepper (cherrypy) init edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestPepperInitEdgeCases:
    def test_debug_http_enabled(self) -> None:
        api = Pepper(debug_http=True)
        assert api.debug_http is True

    def test_backend_alcali(self) -> None:
        api = Pepper(backend="alcali")
        assert api.backend == "alcali"

    def test_empty_api_url_raises(self) -> None:
        with pytest.raises(
            PepperException, match="missing HTTP"
        ):
            Pepper("")

    def test_no_ssh_creates_no_tunnel(self) -> None:
        api = Pepper()
        assert api._tunnel_proc is None


# ═══════════════════════════════════════════════════════════════════════
# AsyncPepper — full coverage (via mock, no real HTTP)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestAsyncPepperFull:
    async def test_init_with_url(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        assert api.api_url == "https://salt.example.com:8000"
        await api.aclose()

    async def test_init_invalid_url(self) -> None:
        with pytest.raises(PepperException, match="missing HTTP"):
            AsyncPepper("no-scheme")

    async def test_init_ignore_ssl(self) -> None:
        api = AsyncPepper(ignore_ssl_errors=True)
        assert api.ssl_verify is False
        await api.aclose()

    async def test_ensure_client_creates_session(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        client = await api._ensure_client()
        assert client is api._client
        assert client is not None
        assert client.headers["Accept"] == "application/json"
        assert client.headers["Content-Type"] == "application/json"
        assert client.headers["X-Requested-With"] == "XMLHttpRequest"
        await api.aclose()

    async def test_ensure_client_with_auth_token(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "my_jwt"}
        client = await api._ensure_client()
        assert client.headers.get("X-Auth-Token") == "my_jwt"
        await api.aclose()

    async def test_update_headers_adds_token(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        await api._ensure_client()
        api.auth = {"token": "new_token"}
        api._update_headers()
        assert api._client.headers["X-Auth-Token"] == "new_token"
        await api.aclose()

    async def test_update_headers_removes_token(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "will_be_cleared"}
        await api._ensure_client()
        api.auth = {}
        api._update_headers()
        assert "X-Auth-Token" not in api._client.headers
        await api.aclose()

    async def test_update_headers_noop_no_client(self) -> None:
        api = AsyncPepper()
        api._update_headers()  # should not raise

    @pytest.mark.parametrize("status,expected", [
        (401, "Authentication denied"),
        (404, "Resource not found"),
        (500, "Server error"),
    ])
    async def test_request_http_errors(self, status, expected) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        mock_resp = unittest.mock.AsyncMock(spec=httpx.Response)
        mock_resp.status_code = status
        mock_resp.headers = {}
        # httpx raises HTTPStatusError on raise_for_status() for 4xx/5xx
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=unittest.mock.Mock(), response=mock_resp,
        )
        with unittest.mock.patch.object(api, "_ensure_client") as mock_ec:
            mock_client = unittest.mock.AsyncMock()
            mock_client.request.return_value = mock_resp
            mock_ec.return_value = mock_client
            with pytest.raises(PepperException, match=expected):
                await api._request("/")
        await api.aclose()

    async def test_request_json_decode_error(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        mock_resp = unittest.mock.AsyncMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.side_effect = ValueError("bad json")
        with unittest.mock.patch.object(api, "_ensure_client") as mock_ec:
            mock_client = unittest.mock.AsyncMock()
            mock_client.request.return_value = mock_resp
            mock_ec.return_value = mock_client
            with pytest.raises(
                PepperException, match="Unable to parse"
            ):
                await api._request_json("/")
        await api.aclose()

    async def test_req_get_401(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        mock_resp = unittest.mock.AsyncMock(spec=httpx.Response)
        mock_resp.status_code = 401
        mock_resp.headers = {}
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=unittest.mock.Mock(), response=mock_resp,
        )
        with unittest.mock.patch.object(api, "_ensure_client") as mock_ec:
            mock_client = unittest.mock.AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_ec.return_value = mock_client
            with pytest.raises(PepperException, match="Authentication denied"):
                await api.req_get("/keys")
        await api.aclose()

    async def test_req_get_500(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        mock_resp = unittest.mock.AsyncMock(spec=httpx.Response)
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=unittest.mock.Mock(), response=mock_resp,
        )
        with unittest.mock.patch.object(api, "_ensure_client") as mock_ec:
            mock_client = unittest.mock.AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_ec.return_value = mock_client
            with pytest.raises(PepperException, match="Server error"):
                await api.req_get("/keys")
        await api.aclose()

    async def test_req_get_404(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        mock_resp = unittest.mock.AsyncMock(spec=httpx.Response)
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=unittest.mock.Mock(), response=mock_resp,
        )
        with unittest.mock.patch.object(api, "_ensure_client") as mock_ec:
            mock_client = unittest.mock.AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_ec.return_value = mock_client
            with pytest.raises(PepperException, match="Resource not found"):
                await api.req_get("/keys")
        await api.aclose()

    async def test_req_stream_no_auth(self) -> None:
        api = AsyncPepper()
        with pytest.raises(
            PepperException, match="Authentication required"
        ):
            await api.req_stream("/events")
        await api.aclose()

    async def test_login(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        with unittest.mock.patch.object(
            api, "_request_json", new=unittest.mock.AsyncMock()
        ) as mock_rj:
            mock_rj.return_value = {"return": [{"token": "t1", "user": "me"}]}
            result = await api.login(username="u", password="p", eauth="pam")
        assert result["token"] == "t1"
        await api.aclose()

    async def test_login_no_return(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        with unittest.mock.patch.object(
            api, "_request_json", new=unittest.mock.AsyncMock()
        ) as mock_rj:
            mock_rj.return_value = {}
            result = await api.login(username="u", password="p", eauth="pam")
        assert result == {}
        await api.aclose()

    async def test_token(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        with unittest.mock.patch.object(
            api, "_request_json", new=unittest.mock.AsyncMock()
        ) as mock_rj:
            mock_rj.return_value = [{"token": "t2", "expire": 999}]
            result = await api.token(username="u", password="p", eauth="pam")
        assert result["token"] == "t2"
        await api.aclose()

    async def test_jwt_login(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        result = await api.jwt_login("my.jwt")
        assert result["token"] == "my.jwt"
        await api.aclose()

    async def test_local(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        with unittest.mock.patch.object(
            api, "low", new=unittest.mock.AsyncMock()
        ) as mock_low:
            mock_low.return_value = {"return": [{"m1": True}]}
            result = await api.local(tgt="*", fun="test.ping")
        assert result["return"][0]["m1"] is True
        await api.aclose()

    async def test_local_async(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        with unittest.mock.patch.object(
            api, "low", new=unittest.mock.AsyncMock()
        ) as mock_low:
            mock_low.return_value = {"return": [{"jid": "123"}]}
            result = await api.local_async(tgt="*", fun="state.sls", arg=["webserver"])
        assert result["return"][0]["jid"] == "123"
        await api.aclose()

    async def test_local_batch(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        with unittest.mock.patch.object(
            api, "low", new=unittest.mock.AsyncMock()
        ) as mock_low:
            mock_low.return_value = {"return": [{}]}
            await api.local_batch(tgt="*", fun="test.ping", batch="10")
        await api.aclose()

    async def test_ssh(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        with unittest.mock.patch.object(
            api, "low", new=unittest.mock.AsyncMock()
        ) as mock_low:
            await api.ssh(tgt="host-1", fun="test.ping")
        await api.aclose()

    async def test_queue(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        with unittest.mock.patch.object(
            api, "low", new=unittest.mock.AsyncMock()
        ) as mock_low:
            mock_low.return_value = {"return": [{}]}
            result = await api.queue("queues.list")
        assert result is not None
        await api.aclose()

    async def test_runner(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        with unittest.mock.patch.object(
            api, "low", new=unittest.mock.AsyncMock()
        ) as mock_low:
            mock_low.return_value = {"return": [{"jobs": []}]}
            result = await api.runner("jobs.list_jobs")
        assert result["return"][0]["jobs"] == []
        await api.aclose()

    async def test_wheel(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        with unittest.mock.patch.object(
            api, "low", new=unittest.mock.AsyncMock()
        ) as mock_low:
            mock_low.return_value = {"return": [{"data": {}}]}
            result = await api.wheel("key.list_all")
        assert result["return"][0]["data"] == {}
        await api.aclose()

    async def test_lookup_jid(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        with unittest.mock.patch.object(
            api, "low", new=unittest.mock.AsyncMock()
        ) as mock_low:
            mock_low.return_value = {"return": [{"m1": True}]}
            result = await api.lookup_jid("2025090512000012345")
        assert result is not None
        await api.aclose()

    async def test_context_manager(self) -> None:
        async with AsyncPepper("https://salt.example.com:8000") as api:
            assert api._client is not None
        assert api._client is None

    async def test_req_stream_authorized(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        mock_resp = unittest.mock.AsyncMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.headers = {}
        with unittest.mock.patch.object(api, "_ensure_client") as mock_ec:
            mock_client = unittest.mock.AsyncMock()
            mock_client.request.return_value = mock_resp
            mock_ec.return_value = mock_client
            resp = await api.req_stream("/events")
        assert resp.status_code == 200
        await api.aclose()

    async def test_aclose_noop(self) -> None:
        api = AsyncPepper()
        await api.aclose()  # should not raise

    async def test_low(self) -> None:
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        with unittest.mock.patch.object(
            api, "_request_json", new=unittest.mock.AsyncMock()
        ) as mock_rj:
            mock_rj.return_value = {"return": [{}]}
            result = await api.low(
                [{"client": "local", "tgt": "*", "fun": "test.ping"}]
            )
        assert result is not None
        await api.aclose()

    @pytest.mark.parametrize("method,status,expected", [
        ("_request", 401, "Authentication denied"),
        ("_request", 500, "Server error"),
        ("req_get", 401, "Authentication denied"),
        ("req_get", 500, "Server error"),
    ])
    async def test_request_network_errors(self, method, status, expected) -> None:
        """Non-HTTP errors (connection, timeout, etc.)."""
        api = AsyncPepper("https://salt.example.com:8000")
        api.auth = {"token": "t"}
        with unittest.mock.patch.object(api, "_ensure_client") as mock_ec:
            mock_client = unittest.mock.AsyncMock()
            if method == "req_get":
                mock_client.get.side_effect = httpx.ConnectError("Connection refused")
            else:
                mock_client.request.side_effect = httpx.ConnectError("Connection refused")
            mock_ec.return_value = mock_client
            with pytest.raises(PepperException):
                if method == "req_get":
                    await api.req_get("/keys")
                else:
                    await api._request("/")
        await api.aclose()


# need the AsyncPepper class imported
from salt_pepper.async_client import AsyncPepper  # noqa: E402, F811

# need requests for error simulation
import requests  # noqa: E402, F811
import unittest.mock  # noqa: E402, F811
import httpx  # noqa: E402, F811
