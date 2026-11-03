from datetime import timedelta

import pytest

from decapod.util import (
    estimate_tokens,
    extract_json,
    format_duration,
    one_line,
    parse_duration,
    truncate,
)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("30s", 30),
        ("5m", 300),
        ("1h30m", 5400),
        ("2d", 172800),
        ("1w", 604800),
        ("250ms", 0.25),
        ("45", 45),
        (12, 12),
        ("1h 5m", 3900),
    ],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text).total_seconds() == pytest.approx(seconds)


@pytest.mark.parametrize("bad", ["", "abc", "5x", "m5", "-3", "5m garbage", True])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_format_duration():
    assert format_duration(timedelta(seconds=42)) == "42s"
    assert format_duration(timedelta(minutes=3, seconds=4)) == "3m 04s"
    assert format_duration(timedelta(hours=1, minutes=5)) == "1h 05m"
    assert format_duration(timedelta(days=2, hours=3)) == "2d 03h"
    assert format_duration(-5) == "-5s"


def test_truncate_and_one_line():
    assert truncate("hello", 10) == "hello"
    out = truncate("x" * 100, 40)
    assert len(out) <= 40 and "truncated" in out
    assert truncate("abc", 0) == ""
    assert one_line("a\n  b\tc") == "a b c"
    assert one_line("x" * 50, 10).endswith("…")
    assert len(one_line("x" * 50, 10)) == 10


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 400) == 100


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Sure! Here you go:\n{"a": 1}\nHope that helps.',
        'prefix {"a": 1} {"b": 2}',
        '```\n{"a": 1}\n```',
    ],
)
def test_extract_json(text):
    assert extract_json(text) == {"a": 1}


def test_extract_json_handles_braces_in_strings():
    assert extract_json('noise {"s": "a } b {", "n": [1, {"x": 2}]} tail') == {
        "s": "a } b {",
        "n": [1, {"x": 2}],
    }


def test_extract_json_failure():
    with pytest.raises(ValueError):
        extract_json("no json here {broken")
