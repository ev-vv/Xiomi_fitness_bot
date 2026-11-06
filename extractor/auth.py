"""Xiaomi account auth: QR login, passToken refresh, token storage.

Ported from alexgetmancom/miband-bot src/xiaomi/client.ts XiaomiAuth (GPL-3.0).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .crypto import sha1_b64

log = logging.getLogger(__name__)

STS_URL = "https://sts-hlth.io.mi.com/healthapp/sts"
QR_URL = "https://account.xiaomi.com/longPolling/loginUrl"
CONNECT_RETRIES = 4
SERVICE_LOGIN_URL = "https://account.xiaomi.com/pass/serviceLogin"
LOGIN_UA = (
    "Dalvik/2.1.0 (Linux; U; Android 12; V2284A Build/ab8c0d1.1) APP/mi.health APPV/353001 "
    "MK/VjIyODRB SDKV/5.3.0.release.68 CPN/com.mi.health PassportSDK/"
)


class XiaomiError(Exception):
    pass


class AuthError(XiaomiError):
    pass


class TokenExpiredError(XiaomiError):
    pass


@dataclass
class AuthToken:
    user_id: str = ""
    c_user_id: str = ""
    service_token: str = ""
    ssecurity: str = ""
    pass_token: str = ""
    device_id: str = field(default_factory=lambda: f"an_{secrets.token_hex(16)}")

    @classmethod
    def load(cls, path: Path) -> AuthToken:
        payload = json.loads(path.read_text("utf-8"))
        return cls(**{k: str(payload.get(k) or "") for k in cls.__dataclass_fields__})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".token-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @property
    def is_authenticated(self) -> bool:
        return bool(self.service_token and self.ssecurity)

    @property
    def can_refresh(self) -> bool:
        return bool(self.pass_token and self.user_id)


def parse_mi_response(text: str) -> dict[str, Any]:
    body = text.removeprefix("&&&START&&&")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise XiaomiError(f"Xiaomi response is not JSON: {body[:200]}") from exc
    return data if isinstance(data, dict) else {}


class XiaomiHttp:
    """httpx wrapper with a host-agnostic cookie jar and no auto-redirects (mirrors the TS client)."""

    def __init__(self, headers: dict[str, str], timeout: float = 30.0) -> None:
        self.cookies: dict[str, str] = {}
        self._client = httpx.Client(headers=headers, timeout=timeout, follow_redirects=False)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", None) or {})
        if self.cookies:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        # The request never left the host on these errors (flaky DNS behind a VPN), so a retry is safe.
        for attempt in range(CONNECT_RETRIES):
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt == CONNECT_RETRIES - 1:
                    raise
                log.warning("Xiaomi %s: %s, retrying", urllib.parse.urlparse(url).netloc, exc)
                time.sleep(2 ** attempt)
        self._client.cookies.clear()
        for raw in response.headers.get_list("set-cookie"):
            name, sep, value = raw.split(";", 1)[0].partition("=")
            if sep and name:
                self.cookies[name.strip()] = value.strip()
        return response

    def close(self) -> None:
        self._client.close()


class XiaomiAuth:
    def __init__(self, token: AuthToken | None = None, token_path: Path | None = None,
                 client_sign: str | None = None, timeout: float = 30.0) -> None:
        self.token = token or AuthToken()
        self.token_path = token_path
        self.client_sign = client_sign
        self.http = XiaomiHttp(
            {"user-agent": LOGIN_UA, "content-type": "application/x-www-form-urlencoded"}, timeout
        )

    @classmethod
    def from_file(cls, path: Path, **kwargs: Any) -> XiaomiAuth:
        return cls(AuthToken.load(path), token_path=path, **kwargs)

    def save(self) -> None:
        if not self.token_path:
            raise AuthError("Token path is not configured")
        self.token.save(self.token_path)

    def login_qr(self, on_qr: Callable[[str, str], None] | None = None, max_wait: int = 300) -> AuthToken:
        self.http.cookies["deviceId"] = self.token.device_id
        params = {
            "_qrsize": "480",
            "qs": "%3Fsid%3Dmiothealth%26_json%3Dtrue",
            "callback": STS_URL,
            "_hasLogo": "false",
            "sid": "miothealth",
            "serviceParam": "",
            "_locale": os.environ.get("MIFIT_LOCALE", "ru_RU"),
            "_dc": str(int(time.time() * 1000)),
        }
        resp = self.http.request("GET", QR_URL, params=params)
        if resp.status_code != 200:
            raise XiaomiError(f"QR request failed: {resp.status_code}")
        qr = parse_mi_response(resp.text)
        image, login_url, polling_url = qr.get("qr", ""), qr.get("loginUrl", ""), qr.get("lp", "")
        if not image or not polling_url:
            raise XiaomiError("Xiaomi did not return a QR polling URL")
        if on_qr:
            on_qr(image, login_url)
        deadline = time.monotonic() + min(int(qr.get("timeout") or max_wait), max_wait)
        resp = None
        while time.monotonic() < deadline:
            try:
                resp = self.http.request("GET", polling_url, timeout=60.0)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError as exc:
                log.debug("QR polling failed: %s", exc)
            time.sleep(2)
        if resp is None or resp.status_code != 200:
            raise XiaomiError("Xiaomi QR login timed out")
        data = parse_mi_response(resp.text)
        self.token.ssecurity = str(data.get("ssecurity") or "")
        self.token.user_id = str(data.get("userId") or "")
        self.token.pass_token = str(data.get("passToken") or "")
        self.token.c_user_id = str(data.get("cUserId") or "")
        location = str(data.get("location") or "")
        if location:
            self.token.service_token = self._follow_for_service_token(location)
        if not self.token.service_token:
            raise XiaomiError("Xiaomi login did not return serviceToken")
        self.sts_exchange()
        return self.token

    def _follow_for_service_token(self, url: str) -> str:
        redirect = self.http.request("GET", url)
        location = redirect.headers.get("location") or url
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        return self.http.cookies.get("serviceToken") or (query.get("serviceToken") or [""])[0]

    def sts_exchange(self) -> None:
        params = {
            "d": self.token.device_id,
            "ticket": "0",
            "pwd": "0",
            "p_ts": str(int(time.time() * 1000)),
            "fid": "0",
            "p_lm": "2",
            "p_ur": "CN",
            "sid": "hlth.io.mi.com",
        }
        if self.client_sign:
            params["clientSign"] = self.client_sign
        resp = self.http.request("GET", STS_URL, params=params)
        if resp.status_code == 200 and resp.text.strip() == "ok" and self.http.cookies.get("serviceToken"):
            self.token.service_token = self.http.cookies["serviceToken"]

    def refresh(self) -> AuthToken:
        if not self.token.can_refresh:
            raise TokenExpiredError("Xiaomi passToken credentials are missing; run `login` again")
        self.http.cookies.update(
            passToken=self.token.pass_token, deviceId=self.token.device_id, userId=self.token.user_id
        )
        resp = self.http.request("GET", SERVICE_LOGIN_URL, params={"_json": "true", "sid": "miothealth"})
        if resp.status_code != 200:
            raise TokenExpiredError(f"Xiaomi token refresh failed: {resp.status_code}")
        data = parse_mi_response(resp.text)
        ssecurity = str(data.get("ssecurity") or "")
        if not ssecurity:
            raise TokenExpiredError("Xiaomi serviceLogin did not return ssecurity")
        # sts_exchange() overwrites token.service_token, so keep the old values for comparison/rollback.
        previous = (self.token.c_user_id, self.token.service_token, self.token.ssecurity)
        c_user_id = str(data.get("cUserId") or self.token.c_user_id)
        if c_user_id:
            self.http.cookies["cUserId"] = c_user_id
        new_service_token = ""
        location = str(data.get("location") or "")
        if location:
            sign = urllib.parse.quote(sha1_b64(f"nonce={data.get('nonce', '')}&{ssecurity}"), safe="/")
            new_service_token = self._follow_for_service_token(f"{location}&clientSign={sign}")
        self.sts_exchange()
        new_service_token = new_service_token or self.http.cookies.get("serviceToken", "")
        if not new_service_token or new_service_token == previous[1]:
            self.token.c_user_id, self.token.service_token, self.token.ssecurity = previous
            raise TokenExpiredError("Xiaomi token refresh did not return a new serviceToken")
        self.token.c_user_id = c_user_id
        self.token.service_token = new_service_token
        self.token.ssecurity = ssecurity
        if self.token_path:
            self.save()
        return self.token
