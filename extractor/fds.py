"""Xiaomi FDS sleep-detail objects: key derivation and binary parser.

Ported from alexgetmancom/miband-bot src/xiaomi/fds.ts (GPL-3.0).
"""

from __future__ import annotations

import gzip
import struct
import zlib
from dataclasses import dataclass, field

# Order in which bits of the 2-byte validity bitmap map to field types.
VALID_TYPES = [0, 1, 2, 6, 7, 8, 9, 10, 3, 4, 5]
FDS_SLEEP_DAILY_TYPE = 8
FDS_ALL_DAY_FILE_TYPE = 0
TIMEZONE_15MIN_LIMIT = 96
SECONDS_PER_15_MINUTES = 900


def normalize_timezone_to_15min(value: int) -> int:
    if abs(value) <= TIMEZONE_15MIN_LIMIT:
        return int(value)
    return int(value / SECONDS_PER_15_MINUTES)


def gen_data_id_key(
    timestamp: int,
    timezone_15min: int,
    daily_type: int = FDS_SLEEP_DAILY_TYPE,
    file_type: int = FDS_ALL_DAY_FILE_TYPE,
    data_type: int = 0,
    sport_type: int = 0,
) -> bytes:
    flags = ((data_type << 7) + (sport_type << 2) + (daily_type << 2) + file_type) & 0xFF
    return struct.pack("<Ib B", timestamp, timezone_15min, flags)


@dataclass
class AssistSeries:
    start_time: int
    interval: int
    values: list[int]


@dataclass
class SleepDetails:
    version: int
    report: dict[str, int | bool]
    heart_rate: list[tuple[int, int]] = field(default_factory=list)
    spo2: list[tuple[int, int]] = field(default_factory=list)


def _read_assist(payload: bytes, pos: int, version: int) -> tuple[AssistSeries | None, int]:
    if pos + 4 > len(payload):
        return None, pos
    interval, count = struct.unpack_from("<hh", payload, pos)
    pos += 4
    if count <= 0:
        return None, pos
    if pos + count + (4 if version >= 2 else 0) > len(payload):
        return None, pos
    start = 0
    if version >= 2:
        (start,) = struct.unpack_from("<I", payload, pos)
        pos += 4
    values = list(payload[pos : pos + count])
    return AssistSeries(start, interval, values), pos + count


def parse_all_day_sleep_bytes(payload: bytes) -> SleepDetails | None:
    if len(payload) < 9:
        return None
    try:
        version = payload[5]
        bitmap = payload[7:9]
        valid = {t: bool(bitmap[i // 8] & (1 << (7 - i % 8))) for i, t in enumerate(VALID_TYPES)}
        pos = 9
        report: dict[str, int | bool] = {"sleepFinish": payload[pos] == 1}
        report["deviceBedTime"], report["deviceWakeupTime"] = struct.unpack_from("<II", payload, pos + 1)
        pos += 9
        quality, efficiency = payload[pos], payload[pos + 1]
        pos += 2
        if valid[2]:
            report["sleepQuality"] = quality
        if valid[6]:
            report["sleepEfficiency"] = efficiency
        names = ("entrySleepDuration", "linBedDuration", "goBedTime", "leaveBedTime")
        for name, type_id, value in zip(names, (7, 8, 9, 10), struct.unpack_from("<IIII", payload, pos)):
            if valid[type_id]:
                report[name] = value
        pos += 16
        details = SleepDetails(version=version, report=report)
        if valid[3]:
            series, pos = _read_assist(payload, pos, version)
            if series:
                details.heart_rate = [
                    (series.start_time + i * series.interval, v)
                    for i, v in enumerate(series.values)
                    if 0 < v < 255
                ]
        if valid[4]:
            series, pos = _read_assist(payload, pos, version)
            if series:
                details.spo2 = [
                    (series.start_time + i * series.interval, v)
                    for i, v in enumerate(series.values)
                    if 0 < v <= 100
                ]
        return details
    except (struct.error, IndexError):
        return None


def decompress_if_needed(content: bytes) -> bytes:
    try:
        if content[:2] == b"\x1f\x8b":
            return gzip.decompress(content)
        if content[:1] == b"\x78" and content[1:2] in (b"\x9c", b"\x01"):
            return zlib.decompress(content)
    except (OSError, zlib.error):
        pass
    return content
