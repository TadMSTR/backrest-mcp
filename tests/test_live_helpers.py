"""The live file's leak-check helper, tested without a live Backrest (audit p2 F-04)."""

from __future__ import annotations

import json

import pytest

from tests.test_live_conformance import _contains_secret


@pytest.mark.parametrize(
    "secret",
    ['pa"ss', "back\\slash", "tab\there", "pässwörd", "plain-secret-123"],
)
@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_finds_secret_however_json_encodes_it(secret, ensure_ascii):
    raw = json.dumps({"repos": [{"note": secret}]}, ensure_ascii=ensure_ascii)
    assert _contains_secret(raw, secret)


def test_plain_substring_alone_would_miss_an_escaped_secret():
    """The check this replaced: `secret in raw` is False for a JSON-escaped quote."""
    secret = 'pa"ss'
    raw = json.dumps({"x": secret})
    assert secret not in raw
    assert _contains_secret(raw, secret)


def test_absent_or_empty_secret():
    raw = json.dumps({"repos": [{"id": "atlas-forge"}]})
    assert not _contains_secret(raw, "not-there")
    assert not _contains_secret(raw, "")
