"""Org-number validation: exactly 9 digits (spaces/dashes tolerated)."""

import pytest

from signalpost.batch import normalize_org_number, read_batch


@pytest.mark.parametrize("raw,expected", [
    ("917755736", "917755736"),
    (917755736, "917755736"),
    ("917 755 736", "917755736"),
    ("917-755-736", "917755736"),
    ("  920100000\n", "920100000"),
    ({"organisation_number": "917755736"}, None),  # dicts are not org numbers
])
def test_normalize_valid(raw, expected):
    assert normalize_org_number(raw) == expected


@pytest.mark.parametrize("raw", [
    "91775573",        # 8 digits
    "9177557361",      # 10 digits
    "91775573a",       # letter (only 8 digits after strip)
    "", "   ", None,
    "12345678-90x",    # 10 digits after strip -> invalid
])
def test_normalize_invalid(raw):
    assert normalize_org_number(raw) is None


def test_read_batch_mixed_rows(tmp_path):
    batch = tmp_path / "batch.jsonl"
    batch.write_text(
        '{"organisation_number": "917755736"}\n'
        '{"org": "920-100-000"}\n'
        '"123456789"\n'
        "\n"  # blank lines skipped
        '{"organisation_number": "bad"}\n',
        encoding="utf-8",
    )
    inputs = read_batch(str(batch))
    assert [i.organisation_number for i in inputs] == [
        "917755736", "920100000", "123456789", "",
    ]
    # Invalid rows still produce a BatchInput (the agent emits a FAILED envelope).
    assert inputs[-1].organisation_number == ""


def test_read_batch_missing_file():
    with pytest.raises(FileNotFoundError):
        read_batch("/nonexistent/batch.jsonl")


def test_read_batch_bad_json(tmp_path):
    batch = tmp_path / "bad.jsonl"
    batch.write_text("not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        read_batch(str(batch))
