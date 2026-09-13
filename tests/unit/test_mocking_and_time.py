"""Unit tests for modootest HTTP mocking and time control utilities."""
import datetime
import re
import pytest
import requests

from modootest.developer.mocking import HttpMock, RecordedCall, mock_http
from modootest.developer.time import freeze_time


def test_mock_http_basic_get_json():
    with mock_http() as http:
        http.get("https://api.example.com/items", json_data=[{"id": 1, "name": "Test"}])
        resp = requests.get("https://api.example.com/items")
        assert resp.status_code == 200
        assert resp.json() == [{"id": 1, "name": "Test"}]
        assert resp.headers.get("Content-Type") == "application/json"

        http.assert_called("https://api.example.com/items", method="GET", count=1)


def test_mock_http_post_with_body_inspection():
    with mock_http() as http:
        http.post(
            "https://payment.example.com/charge",
            json_data={"transaction_id": "tx_123", "status": "approved"},
            status_code=201,
        )
        resp = requests.post(
            "https://payment.example.com/charge",
            json={"amount": 5000, "currency": "USD"},
            headers={"Authorization": "Bearer secret_token"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "approved"

        assert len(http.calls) == 1
        call = http.calls[0]
        assert call.method == "POST"
        assert call.url == "https://payment.example.com/charge"
        assert call.json() == {"amount": 5000, "currency": "USD"}
        assert "Authorization" in call.headers


def test_mock_http_regex_url_matching():
    with mock_http() as http:
        http.get(re.compile(r"^https://api\.example\.com/users/\d+$"), json_data={"found": True})

        resp1 = requests.get("https://api.example.com/users/42")
        assert resp1.status_code == 200
        assert resp1.json() == {"found": True}

        resp2 = requests.get("https://api.example.com/users/999")
        assert resp2.status_code == 200
        assert resp2.json() == {"found": True}

        assert len(http.calls) == 2


def test_mock_http_unhandled_url_raises_connection_refused():
    with mock_http() as http:
        http.get("https://allowed.example.com")
        with pytest.raises(ConnectionRefusedError, match="HttpMock blocked unhandled request"):
            requests.get("https://unhandled.example.com/endpoint")


def test_mock_http_assert_called_failure():
    with mock_http() as http:
        http.get("https://api.example.com/test", text="ok")
        requests.get("https://api.example.com/test")

        with pytest.raises(AssertionError, match="Expected 2 call\\(s\\)"):
            http.assert_called("https://api.example.com/test", count=2)

        with pytest.raises(AssertionError, match="Expected at least one call matching"):
            http.assert_called("https://other.example.com")


def test_mock_http_custom_callback():
    from requests.models import Response
    import io

    def dynamic_callback(request):
        res = Response()
        res.status_code = 202
        res.raw = io.BytesIO(b"dynamic payload")
        return res

    with mock_http() as http:
        http.add("POST", "https://api.example.com/custom", callback=dynamic_callback)
        resp = requests.post("https://api.example.com/custom")
        assert resp.status_code == 202
        assert resp.text == "dynamic payload"


def test_freeze_time_basic():
    frozen_target = "2026-05-15 14:30:00"
    with freeze_time(frozen_target):
        now = datetime.datetime.now()
        assert now.year == 2026
        assert now.month == 5
        assert now.day == 15
        assert now.hour == 14
        assert now.minute == 30

    # Ensure normal time resumes
    normal_now = datetime.datetime.now()
    assert normal_now.year >= 2026


# ============================================================================
# Regression Tests for Review Finding 5 (requests Transport Fidelity)
# ============================================================================


def test_mock_http_response_hooks_dispatch():
    from unittest.mock import MagicMock

    hook = MagicMock(return_value=None)
    with mock_http() as http:
        http.get("https://example.com/api", json_data={"status": "ok"})
        resp = requests.get("https://example.com/api", hooks={"response": hook})

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        hook.assert_called_once()
        # Verify first argument passed to hook is the Response object
        assert hook.call_args[0][0] is resp


def test_mock_http_automatic_redirect_resolution():
    with mock_http() as http:
        http.get(
            "https://example.com/old_endpoint",
            status_code=302,
            headers={"Location": "https://example.com/new_endpoint"},
        )
        http.get("https://example.com/new_endpoint", text="arrived at target")

        session = requests.Session()
        resp = session.get("https://example.com/old_endpoint")

        assert resp.status_code == 200
        assert resp.url == "https://example.com/new_endpoint"
        assert resp.text == "arrived at target"
        assert len(resp.history) == 1
        assert resp.history[0].url == "https://example.com/old_endpoint"
        assert resp.history[0].status_code == 302


def test_mock_http_set_cookie_persists_in_response_and_session():
    with mock_http() as http:
        http.get(
            "https://example.com/set_cookie",
            text="cookie set",
            headers={"Set-Cookie": "session_id=tok_98765; Path=/; Domain=example.com"},
        )

        session = requests.Session()
        resp = session.get("https://example.com/set_cookie")

        assert resp.status_code == 200
        assert resp.cookies.get("session_id") == "tok_98765"
        assert session.cookies.get("session_id") == "tok_98765"


def test_mock_http_streaming_consumption_and_close():
    with mock_http() as http:
        http.get("https://example.com/stream", text="chunk1chunk2chunk3")

        resp = requests.get("https://example.com/stream", stream=True)
        assert resp.status_code == 200
        chunk = resp.raw.read(6)
        assert chunk == b"chunk1"
        resp.close()
        assert resp.raw.closed


def test_mock_http_nested_scopes_and_cleanup():
    from requests.adapters import HTTPAdapter
    original_send = HTTPAdapter.send

    with mock_http() as outer_http:
        outer_http.get("https://example.com/common", text="outer")
        outer_http.get("https://example.com/outer_only", text="outer_exclusive")

        # Nested scope overrides common URL
        with mock_http() as inner_http:
            inner_http.get("https://example.com/common", text="inner")

            resp_inner = requests.get("https://example.com/common")
            assert resp_inner.text == "inner"

        # Back in outer scope, outer rule resumes
        resp_outer = requests.get("https://example.com/common")
        assert resp_outer.text == "outer"

    # Outside all mocks, original adapter send is fully restored
    assert HTTPAdapter.send is original_send


def test_mock_http_restoration_after_inner_and_outer_exceptions():
    from requests.adapters import HTTPAdapter
    original_send = HTTPAdapter.send

    # 1. Exception in inner scope restores outer mock, then outer exit restores original_send
    with mock_http() as outer_http:
        outer_http.get("https://example.com/item", text="outer")

        with pytest.raises(RuntimeError, match="inner explosion"):
            with mock_http() as inner_http:
                inner_http.get("https://example.com/item", text="inner")
                assert requests.get("https://example.com/item").text == "inner"
                raise RuntimeError("inner explosion")

        # Outer mock still handles request
        assert requests.get("https://example.com/item").text == "outer"

    assert HTTPAdapter.send is original_send

    # 2. Exception in outer scope restores original_send cleanly
    with pytest.raises(ValueError, match="outer explosion"):
        with mock_http() as outer_http:
            outer_http.get("https://example.com/item", text="outer")
            assert requests.get("https://example.com/item").text == "outer"
            raise ValueError("outer explosion")

    assert HTTPAdapter.send is original_send
