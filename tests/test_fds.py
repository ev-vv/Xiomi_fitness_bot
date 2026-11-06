import gzip
import json
import zlib
from pathlib import Path

import pytest

from extractor.fds import decompress_if_needed, normalize_timezone_to_15min, parse_all_day_sleep_bytes

V = json.loads((Path(__file__).parent / "fixtures" / "ts_fds_vectors.json").read_text("utf-8"))


@pytest.mark.parametrize("name", sorted(V))
def test_parser_matches_ts(name):
    details = parse_all_day_sleep_bytes(bytes.fromhex(V[name]["hex"]))
    expected = V[name]["expected"]
    assert details is not None
    assert details.report == expected["report"]
    assert [list(p) for p in details.heart_rate] == expected["records"]["heart_rate"]
    assert [list(p) for p in details.spo2] == expected["records"]["spo2"]


def test_v2_values_filtered_and_timed():
    details = parse_all_day_sleep_bytes(bytes.fromhex(V["v2"]["hex"]))
    assert details.version == 2
    assert details.heart_rate == [(1759970000, 55), (1759970120, 61), (1759970240, 70)]  # 0 and 255 dropped
    assert details.spo2 == [(1759970000, 97), (1759970600, 95)]  # 101 dropped


def test_short_or_truncated_payload():
    assert parse_all_day_sleep_bytes(b"\x00" * 5) is None
    truncated = bytes.fromhex(V["v2"]["hex"])[:40]
    details = parse_all_day_sleep_bytes(truncated)
    assert details is not None and details.heart_rate == []


@pytest.mark.parametrize("value,expected", [(12, 12), (-20, -20), (10800, 12), (-18000, -20), (0, 0)])
def test_normalize_timezone(value, expected):
    assert normalize_timezone_to_15min(value) == expected


def test_decompress():
    data = b"payload" * 10
    assert decompress_if_needed(gzip.compress(data)) == data
    assert decompress_if_needed(zlib.compress(data)) == data
    assert decompress_if_needed(data) == data
