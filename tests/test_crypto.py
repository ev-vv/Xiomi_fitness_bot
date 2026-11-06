"""Crypto checks against vectors produced by the original TypeScript implementation (tests/fixtures)."""

import json
from pathlib import Path

from Crypto.Cipher import ARC4

from extractor.crypto import (
    b64d, decode_fds_aes, decrypt_response, encrypted_params, make_nonce, rc4, signed_nonce,
)
from extractor.fds import gen_data_id_key

V = json.loads((Path(__file__).parent / "fixtures" / "ts_crypto_vectors.json").read_text("utf-8"))


def test_rc4_matches_ts():
    assert rc4(b"key", b"plaintext data").hex() == V["rc4_hex"]


def test_rc4_matches_reference_arc4_with_drop():
    key, data = b"0123456789abcdef", bytes(range(200))
    reference = ARC4.new(key, drop=1024).encrypt(data)
    assert rc4(key, data) == reference
    assert rc4(key, reference) == data


def test_nonce_layout_matches_ts():
    assert make_nonce(b"\xab" * 8, 1_760_000_000_000) == V["encrypted"]["_nonce"]


def test_signed_nonce_matches_ts():
    assert signed_nonce(V["ssecurity"], V["encrypted"]["_nonce"]) == V["snonce"]


def test_encrypted_params_match_ts():
    out = encrypted_params(
        "GET", "/app/v1/data/get_fitness_data_by_time", V["ssecurity"], V["params"], nonce=V["encrypted"]["_nonce"]
    )
    assert out == V["encrypted"]


def test_decrypt_response_matches_ts():
    result = decrypt_response(V["ssecurity"], V["encrypted"]["_nonce"], V["response_body"])
    assert result["code"] == 0
    assert result["result"]["data_list"][0]["value"] == '{"bpm":61}'


def test_fds_aes_base64_and_binary_payloads():
    fds = V["fds"]
    assert decode_fds_aes(fds["cipher_b64"].encode(), fds["obj_key"]).decode() == fds["plain"]
    assert decode_fds_aes(b64d(fds["cipher_b64"]), fds["obj_key"]).decode() == fds["plain"]
    quoted = f'"{fds["cipher_b64"].replace("+", "-").replace("/", "_").rstrip("=")}"'.encode()
    assert decode_fds_aes(quoted, fds["obj_key"]).decode() == fds["plain"]  # real FDS response shape


def test_fds_key_matches_ts():
    assert gen_data_id_key(1760000000, 12).hex() == V["key6"]
