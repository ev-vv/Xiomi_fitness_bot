"""Xiaomi Health cloud API client.

Ported from alexgetmancom/miband-bot src/xiaomi/client.ts MiHealthClient (GPL-3.0).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from .auth import TokenExpiredError, XiaomiAuth, XiaomiError, XiaomiHttp
from .config import Config
from .crypto import b64url, decrypt_response, decode_fds_aes, encrypted_params
from .fds import decompress_if_needed, gen_data_id_key, normalize_timezone_to_15min

log = logging.getLogger(__name__)

AGGREGATED_PATH = "/app/v1/data/get_aggregated_fitness_data_by_time"
FITNESS_PATH = "/app/v1/data/get_fitness_data_by_time"
LATEST_PATH = "/app/v1/data/get_latest_fitness_data"
SPORT_RECORDS_PATH = "/app/v1/data/get_sport_records_by_watermark"
FDS_DOWNLOAD_PATH = "/healthapp/service/gen_download_url"


class APIError(XiaomiError):
    def __init__(self, message: str, code: int | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class DataNotSharedError(APIError):
    pass


@dataclass
class DataItem:
    """One record of get_*_fitness_data_* responses; `value` is the decoded JSON payload."""

    key: str
    time: int
    value: Any
    raw_value: str
    sid: str = ""
    tag: str = ""
    update_time: int = 0
    watermark: str = ""
    source_sid_list: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> DataItem:
        raw = item.get("value")
        raw_text = raw if isinstance(raw, str) else json.dumps(raw if raw is not None else {})
        try:
            value = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            value = raw_text
        return cls(
            key=str(item.get("key") or ""),
            time=int(item.get("time") or 0),
            value=value,
            raw_value=raw_text,
            sid=str(item.get("sid") or ""),
            tag=str(item.get("tag") or ""),
            update_time=int(item.get("update_time") or 0),
            watermark=str(item.get("watermark") or ""),
            source_sid_list=[str(x) for x in item.get("source_sid_list") or []],
        )


class MiHealthClient:
    def __init__(self, auth: XiaomiAuth, config: Config) -> None:
        self.auth = auth
        self.config = config
        self.http = XiaomiHttp(
            {"user-agent": config.user_agent, "region_tag": config.region, "handleparams": "true"},
            config.timeout_s,
        )

    @property
    def uid(self) -> int:
        return int(self.auth.token.user_id)

    def request(self, method: str, path: str, params: dict[str, Any] | None = None,
                retry: bool = True) -> dict[str, Any]:
        token = self.auth.token
        if not token.is_authenticated:
            raise XiaomiError("Xiaomi token is not authenticated; run `login`")
        self.http.cookies["cUserId"] = token.c_user_id
        self.http.cookies["serviceToken"] = token.service_token
        signing_path = "/service/gen_download_url" if path == FDS_DOWNLOAD_PATH else path
        encoded = encrypted_params(method, signing_path, token.ssecurity, params)
        url = f"{self.config.api_base}{path}"
        if method.upper() == "GET":
            resp = self.http.request("GET", url, params=encoded)
        else:
            resp = self.http.request(method, url, data=encoded)
        if resp.status_code == 401:
            if not retry or not token.can_refresh:
                raise TokenExpiredError("Xiaomi token expired; run `login` again")
            self.auth.refresh()
            return self.request(method, path, params, retry=False)
        if resp.status_code != 200:
            raise APIError(f"Xiaomi API {method} {path} failed: HTTP {resp.status_code}",
                           status=resp.status_code)
        result = decrypt_response(token.ssecurity, encoded["_nonce"], resp.text)
        if not isinstance(result, dict):
            raise APIError(f"Unexpected Xiaomi response: {str(result)[:200]}")
        code = int(result.get("code", -1))
        if code != 0:
            message = str(result.get("message") or result.get("msg") or result.get("desc") or "unknown error")
            if code == -4002004:
                raise DataNotSharedError(message, code=code)
            raise APIError(f"Xiaomi API error {code}: {message}", code=code)
        return result

    def _data_list(self, path: str, params: dict[str, Any]) -> list[DataItem]:
        result = self.request("GET", path, params).get("result") or {}
        return [DataItem.from_api(item) for item in result.get("data_list") or [] if isinstance(item, dict)]

    def get_aggregated(self, key: str, start: int, end: int, tag: str = "daily_report",
                       limit: int = 30) -> list[DataItem]:
        """Daily summaries (sleep incl. segment_details, heart_rate, spo2, steps, calories, ...)."""
        return self._data_list(AGGREGATED_PATH, {
            "relative_uid": self.uid, "key": key, "tag": tag,
            "start_time": start, "end_time": end, "limit": limit,
        })

    def get_fitness_data(self, key: str, start: int, end: int, limit: int = 1440) -> tuple[list[DataItem], bool]:
        """Point-level raw samples (heart_rate.bpm, spo2.spo2, stress.stress, ...) and the `has_more` flag."""
        result = self.request("GET", FITNESS_PATH, {
            "relative_uid": self.uid, "key": key, "start_time": start, "end_time": end, "limit": limit,
        }).get("result") or {}
        items = [DataItem.from_api(item) for item in result.get("data_list") or [] if isinstance(item, dict)]
        return items, result.get("has_more") is True

    def get_latest(self) -> dict[str, Any]:
        return self.request("GET", LATEST_PATH, {"relative_uid": self.uid}).get("result") or {}

    def iter_sport_records(self, watermark: int, limit: int = 50) -> Iterator[tuple[list[dict[str, Any]], int]]:
        """Yields (records, new_watermark) pages until the server reports no more data."""
        while True:
            result = self.request("GET", SPORT_RECORDS_PATH, {
                "relative_uid": self.uid, "watermark": watermark, "limit": limit,
            }).get("result") or {}
            records = [r for r in result.get("sport_records") or [] if isinstance(r, dict)]
            if not records:
                return
            new_watermark = max([watermark, *(int(r.get("watermark") or 0) for r in records)])
            yield records, new_watermark
            if result.get("has_more") is not True or new_watermark <= watermark:
                return
            watermark = new_watermark
            time.sleep(self.config.request_pause_s)

    def get_fds_download_info(self, did: str, timestamp: int, timezone: int) -> dict[str, Any] | None:
        """Signed download URL for an FDS object; `did` is the band id (sleep item `source_sid_list`)."""
        key = gen_data_id_key(timestamp, normalize_timezone_to_15min(timezone))
        suffix = f"{b64url(key)}_{b64url(hashlib.sha1(did.encode()).digest())}"
        result = self.request("GET", FDS_DOWNLOAD_PATH, {
            "did": did, "relative_uid": self.uid, "items": [{"timestamp": timestamp, "suffix": suffix}],
        }).get("result") or {}
        info = result.get(f"{suffix}_{timestamp}")
        return info if isinstance(info, dict) and info else None

    def download_sleep_details(self, did: str, timestamp: int, timezone: int) -> bytes | None:
        """Downloads and decrypts the FDS sleep-detail blob for a sleep segment ending at `timestamp`."""
        info = self.get_fds_download_info(did, timestamp, timezone)
        url = (info or {}).get("url")
        if not url:
            return None
        resp = httpx.get(url, timeout=self.config.timeout_s)
        if resp.status_code != 200:
            log.debug("FDS download returned HTTP %s", resp.status_code)
            return None
        obj_key = (info or {}).get("obj_key")
        if obj_key:
            return decode_fds_aes(resp.content, obj_key)
        return decompress_if_needed(resp.content)

    def close(self) -> None:
        self.http.close()
        self.auth.http.close()
