"""Unit tests for CSV validation - run with: python -I tests/test_validation.py"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.validation import (  # noqa: E402
    EXPECTED_HEADER,
    ValidationError,
    validate_csv,
)

GOOD = (
    ",".join(EXPECTED_HEADER)
    + "\r\n"
    + "ST001,2026-10-01T00:00:00Z,12.3\r\n"
    + "ST001,2026-10-01T01:00:00Z,14.1\r\n"
).encode()


def expect_error(label: str, payload: bytes, station: str, month: str,
                 needle: str) -> None:
    try:
        validate_csv(payload, station, month)
    except ValidationError as exc:
        assert needle in str(exc), f"{label}: unexpected message {exc}"
        print(f"  ok  {label} -> {exc}")
        return
    raise AssertionError(f"{label}: expected ValidationError containing "
                         f"{needle!r}")


def main() -> int:
    result = validate_csv(GOOD, "ST001", "2026-10")
    assert result.rows == 2 and result.size_bytes == len(GOOD)
    print("  ok  valid CSV accepted")

    expect_error("empty", b"", "ST001", "2026-10", "empty")
    expect_error(
        "bad header",
        b"station,time,pm\r\nST001,2026-10-01T00:00:00Z,1\r\n",
        "ST001", "2026-10", "header",
    )
    expect_error(
        "station mismatch",
        GOOD.replace(b"ST001,2026-10-01T01", b"ST999,2026-10-01T01"),
        "ST001", "2026-10", "station mismatch",
    )
    expect_error(
        "wrong month",
        GOOD.replace(b"2026-10-01T01", b"2026-09-01T01"),
        "ST001", "2026-10", "outside",
    )
    expect_error(
        "non-utc offset",
        GOOD.replace(b"00:00:00Z", b"00:00:00+08:00"),
        "ST001", "2026-10", "UTC",
    )
    expect_error(
        "bad timestamp",
        GOOD.replace(b"2026-10-01T01:00:00Z", b"not-a-time"),
        "ST001", "2026-10", "invalid utc_time",
    )
    expect_error(
        "non-numeric pm25",
        GOOD.replace(b"14.1", b"n/a"),
        "ST001", "2026-10", "not numeric",
    )
    expect_error(
        "negative pm25",
        GOOD.replace(b"12.3", b"-5"),
        "ST001", "2026-10", "negative",
    )
    expect_error(
        "wrong column count",
        b"station,utc_time,pm25_ugm3\r\nST001,2026-10-01T00:00:00Z\r\n",
        "ST001", "2026-10", "3 columns",
    )
    expect_error(
        "no data rows",
        b"station,utc_time,pm25_ugm3\r\n\r\n",
        "ST001", "2026-10", "no data rows",
    )
    expect_error(
        "invalid utf8",
        b"station,utc_time,pm25_ugm3\r\n\xff\xfe,2026-10,1\r\n",
        "ST001", "2026-10", "UTF-8",
    )

    # BOM-prefixed UTF-8 must still work.
    bom = b"\xef\xbb\xbf" + GOOD
    result = validate_csv(bom, "ST001", "2026-10")
    assert result.rows == 2
    print("  ok  UTF-8 BOM tolerated")

    with tempfile.TemporaryDirectory() as tmp:
        assert Path(tmp).exists()
    print("ALL VALIDATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
