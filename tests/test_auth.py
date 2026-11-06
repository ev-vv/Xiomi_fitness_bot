"""passToken refresh against a mocked Xiaomi account/STS server (no network)."""

import json

import httpx
import pytest

from extractor.auth import STS_URL, AuthToken, TokenExpiredError, XiaomiAuth

LOCATION = STS_URL + "?d=dev&nonce=1"


def make_auth(sts_token: str | None) -> XiaomiAuth:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "account.xiaomi.com":
            body = {"code": 0, "ssecurity": "c2VjdXJpdHk=", "nonce": "1", "location": LOCATION, "cUserId": "cu2"}
            return httpx.Response(200, text="&&&START&&&" + json.dumps(body))
        headers = [("set-cookie", f"serviceToken={sts_token}; path=/")] if sts_token else []
        return httpx.Response(200, text="ok", headers=headers)

    token = AuthToken(user_id="1", c_user_id="cu1", service_token="old", ssecurity="s-old",
                      pass_token="pt", device_id="dev")
    auth = XiaomiAuth(token)
    auth.http._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return auth


def test_refresh_accepts_token_set_by_sts():
    auth = make_auth("new")
    auth.refresh()
    assert (auth.token.service_token, auth.token.c_user_id, auth.token.ssecurity) == ("new", "cu2", "c2VjdXJpdHk=")


def test_refresh_rolls_back_when_token_unchanged():
    auth = make_auth("old")
    with pytest.raises(TokenExpiredError):
        auth.refresh()
    assert (auth.token.service_token, auth.token.c_user_id, auth.token.ssecurity) == ("old", "cu1", "s-old")


def test_http_retries_connect_errors(monkeypatch):
    monkeypatch.setattr("extractor.auth.time.sleep", lambda _: None)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ConnectError("Temporary failure in name resolution", request=request)
        return httpx.Response(200, text="ok")

    auth = make_auth(None)
    auth.http._client = httpx.Client(transport=httpx.MockTransport(handler))
    assert auth.http.request("GET", STS_URL).text == "ok"
    assert len(calls) == 3
