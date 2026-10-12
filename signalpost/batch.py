"""Batch I/O: read and validate Norwegian organisation numbers from JSONL."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_ORG_RE = re.compile(r"^\d{9}$")


@dataclass
class BatchInput:
    """One normalised input row."""

    organisation_number: str
    raw: dict = field(default_factory=dict)


def normalize_org_number(raw: object) -> str | None:
    """Return the 9-digit org number or None if it is not a valid one.

    Norwegian organisation numbers are exactly nine digits (spaces/dashes
    tolerated). Anything else is *not* a company we can research — the caller
    still emits one envelope (state FAILED on the identity claim), never drops
    the row.
    """
    if raw is None:
        return None
    if not isinstance(raw, (str, int)):
        return None  # dicts/rows are handled by read_batch, not here
    digits = re.sub(r"\D", "", str(raw))
    return digits if _ORG_RE.match(digits) else None


def read_batch(path: str) -> list[BatchInput]:
    """Read a JSONL batch of organisation numbers.

    Accepts rows shaped as {"organisation_number": "123456789"}, a bare
    {"org": ...}, or a plain 9-digit string per line. Blank lines are skipped.
    Raises FileNotFoundError for a missing file, ValueError for bad JSON.
    """
    inputs: list[BatchInput] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from None
            if isinstance(row, dict):
                raw = (
                    row.get("organisation_number")
                    or row.get("org")
                    or row.get("orgnr")
                    or row.get("id")
                )
            else:
                raw = row
            inputs.append(BatchInput(organisation_number=normalize_org_number(raw) or "", raw=row if isinstance(row, dict) else {"organisation_number": raw}))
    return inputs
