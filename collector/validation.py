"""Download integrity validation.

A downloaded file is only promoted from its private staging directory once:
  1. it is fully readable as UTF-8 CSV with the expected header;
  2. every data row carries the requested station id;
  3. every data row's UTC sampling time falls inside the requested month.
Anything else raises ValidationError and the staging file is discarded.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

EXPECTED_HEADER = ["station", "utc_time", "pm25_ugm3"]


class ValidationError(Exception):
    """Raised when a downloaded CSV fails integrity checks."""


@dataclass(frozen=True)
class ValidationResult:
    rows: int
    size_bytes: int


def _parse_row_time(value: str) -> tuple[int, int]:
    """Return (year, month) for an ISO-8601 UTC timestamp."""
    v = value.strip()
    # Accept ...Z and ...+00:00 style suffixes.
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(v)
    except ValueError as exc:
        raise ValidationError(f"invalid utc_time value: {value!r}") from exc
    if dt.utcoffset() is not None and dt.utcoffset().total_seconds() != 0:
        raise ValidationError(
            f"utc_time must be UTC, got offset in {value!r}"
        )
    return dt.year, dt.month


def validate_csv(payload: bytes, station: str, month: str) -> ValidationResult:
    """Validate a fully downloaded CSV payload in memory.

    `month` is the requested UTC month as YYYY-MM.
    """
    if not payload:
        raise ValidationError("downloaded file is empty")

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"file is not valid UTF-8: {exc}") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValidationError("file contains no header row") from exc

    normalized = [h.strip().lower() for h in header]
    if normalized != EXPECTED_HEADER:
        raise ValidationError(
            f"unexpected header {header!r}, expected {EXPECTED_HEADER!r}"
        )

    expected_year, expected_month = (int(part) for part in month.split("-"))
    row_count = 0
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not cell.strip() for cell in row):
            continue  # tolerate a single trailing blank line
        if len(row) != 3:
            raise ValidationError(
                f"line {line_no}: expected 3 columns, got {len(row)}"
            )
        row_station, row_time, row_pm25 = (cell.strip() for cell in row)
        if row_station != station:
            raise ValidationError(
                f"line {line_no}: station mismatch: file has {row_station!r}, "
                f"requested {station!r}"
            )
        row_year, row_month = _parse_row_time(row_time)
        if (row_year, row_month) != (expected_year, expected_month):
            raise ValidationError(
                f"line {line_no}: sampling time {row_time!r} outside "
                f"requested UTC month {month}"
            )
        try:
            pm25 = float(row_pm25)
        except ValueError as exc:
            raise ValidationError(
                f"line {line_no}: PM2.5 value {row_pm25!r} is not numeric"
            ) from exc
        if pm25 < 0:
            raise ValidationError(
                f"line {line_no}: negative PM2.5 value {pm25}"
            )
        row_count += 1

    if row_count == 0:
        raise ValidationError("file contains no data rows")

    return ValidationResult(rows=row_count, size_bytes=len(payload))
