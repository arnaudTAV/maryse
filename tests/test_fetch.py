import pytest

from prepress_mcp.fetch import FetchError, validate_url


def test_rejects_http():
    with pytest.raises(FetchError):
        validate_url("http://example.com/a.pdf")


def test_rejects_credentials():
    with pytest.raises(FetchError):
        validate_url("https://user:pw@example.com/a.pdf")


def test_rejects_loopback():
    with pytest.raises(FetchError):
        validate_url("https://127.0.0.1/a.pdf")


def test_rejects_private_host():
    with pytest.raises(FetchError):
        validate_url("https://10.0.0.5/a.pdf")


def test_allows_public_when_flagged(monkeypatch):
    # allow_private bypasses DNS/SSRF checks entirely.
    assert validate_url("https://10.0.0.5/a.pdf", allow_private=True)
