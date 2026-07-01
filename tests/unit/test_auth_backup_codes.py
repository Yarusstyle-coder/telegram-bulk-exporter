"""Backup code generation + verification tests."""

from __future__ import annotations

from src.auth.backup_codes import generate_backup_codes, hash_code, verify_code


def test_generate_produces_unique_codes() -> None:
    codes = generate_backup_codes(10)
    assert len(codes) == 10
    assert len(set(codes)) == 10


def test_code_format() -> None:
    codes = generate_backup_codes(1)
    code = codes[0]
    # XXXX-XXXX-XXXX
    assert len(code) == 14
    parts = code.split("-")
    assert len(parts) == 3
    assert all(len(p) == 4 for p in parts)
    assert all(ch in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for p in parts for ch in p)


def test_verify_finds_matching_hash() -> None:
    codes = generate_backup_codes(3)
    hashes = [hash_code(c) for c in codes]
    idx = verify_code(codes[1], hashes)
    assert idx == 1


def test_verify_case_insensitive_and_ignores_dashes() -> None:
    codes = generate_backup_codes(1)
    hashes = [hash_code(codes[0])]
    lower_no_dashes = codes[0].lower().replace("-", "")
    assert verify_code(lower_no_dashes, hashes) == 0


def test_verify_rejects_bad_code() -> None:
    codes = generate_backup_codes(3)
    hashes = [hash_code(c) for c in codes]
    assert verify_code("1234-5678-9ABC", hashes) is None


def test_verify_rejects_empty() -> None:
    codes = generate_backup_codes(1)
    hashes = [hash_code(codes[0])]
    assert verify_code("", hashes) is None
    assert verify_code("   ", hashes) is None


def test_verify_skips_consumed_hashes() -> None:
    codes = generate_backup_codes(3)
    hashes = [hash_code(c) for c in codes]
    # Simulate code 1 consumed by caller blanking its hash.
    hashes_blanked = hashes.copy()
    hashes_blanked[1] = ""
    assert verify_code(codes[1], hashes_blanked) is None
    # Other codes still work.
    assert verify_code(codes[0], hashes_blanked) == 0
    assert verify_code(codes[2], hashes_blanked) == 2
