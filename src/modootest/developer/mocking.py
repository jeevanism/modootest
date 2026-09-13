"""HTTP mocking utilities for modootest tests with zero mandatory external dependencies."""
from contextlib import contextmanager
from email.message import EmailMessage
import http.client
import io
import json
import re
from typing import Any, Callable, Dict, List, Optional, Pattern, Union
from unittest.mock import patch


class RecordedCall:
    """Represents an intercepted HTTP request made during a test."""

    def __init__(self, request: Any):
        self.request = request
        self.method: str = getattr(request, "method", "GET").upper()
        self.url: str = getattr(request, "url", "")
        self.headers: Dict[str, str] = dict(getattr(request, "headers", {}))
        self.body: Any = getattr(request, "body", None)

    def json(self) -> Any:
        """Parse request body as JSON."""
        if not self.body:
            return None
        if isinstance(self.body, bytes):
            return json.loads(self.body.decode("utf-8"))
        if isinstance(self.body, str):
            return json.loads(self.body)
        return json.loads(str(self.body))

    def __repr__(self) -> str:
        return f"<RecordedCall {self.method} {self.url}>"


class MockRoute:
    """Registered route rule in HttpMock."""

    def __init__(
        self,
        method: str,
        url_or_pattern: Union[str, Pattern[str]],
        status_code: int = 200,
        json_data: Optional[Any] = None,
        text: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        callback: Optional[Callable[[Any], Any]] = None,
    ):
        self.method = method.upper()
        self.url_or_pattern = url_or_pattern
        self.status_code = status_code
        self.json_data = json_data
        self.text = text
        self.headers = headers or {}
        self.callback = callback

    def matches(self, method: str, url: str) -> bool:
        if self.method != "*" and self.method != method.upper():
            return False
        if isinstance(self.url_or_pattern, str):
            return self.url_or_pattern == url
        if isinstance(self.url_or_pattern, re.Pattern):
            return bool(self.url_or_pattern.search(url))
        return False


class _MockOriginalResponse:
    """Wraps an email.message.EmailMessage to support requests.cookies.extract_cookies_to_jar."""

    def __init__(self, msg: EmailMessage):
        self.msg = msg


class HttpMock:
    """Zero-dependency HTTP mock interceptor for requests-based calls operating at the adapter layer."""

    def __init__(self):
        self._routes: List[MockRoute] = []
        self.calls: List[RecordedCall] = []

    def add(
        self,
        method: str,
        url_or_pattern: Union[str, Pattern[str]],
        status_code: int = 200,
        json_data: Optional[Any] = None,
        text: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        callback: Optional[Callable[[Any], Any]] = None,
    ) -> "HttpMock":
        """Register a mock route."""
        route = MockRoute(
            method=method,
            url_or_pattern=url_or_pattern,
            status_code=status_code,
            json_data=json_data,
            text=text,
            headers=headers,
            callback=callback,
        )
        self._routes.append(route)
        return self

    def get(self, url: Union[str, Pattern[str]], **kwargs: Any) -> "HttpMock":
        return self.add("GET", url, **kwargs)

    def post(self, url: Union[str, Pattern[str]], **kwargs: Any) -> "HttpMock":
        return self.add("POST", url, **kwargs)

    def put(self, url: Union[str, Pattern[str]], **kwargs: Any) -> "HttpMock":
        return self.add("PUT", url, **kwargs)

    def delete(self, url: Union[str, Pattern[str]], **kwargs: Any) -> "HttpMock":
        return self.add("DELETE", url, **kwargs)

    def patch(self, url: Union[str, Pattern[str]], **kwargs: Any) -> "HttpMock":
        return self.add("PATCH", url, **kwargs)

    def head(self, url: Union[str, Pattern[str]], **kwargs: Any) -> "HttpMock":
        return self.add("HEAD", url, **kwargs)

    def _handle_adapter_send(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            from requests.models import Response
            from requests.structures import CaseInsensitiveDict
            from requests.utils import get_encoding_from_headers
            from requests.cookies import extract_cookies_to_jar
        except ImportError as exc:
            raise RuntimeError("HTTP mocking requires 'requests' package to be installed.") from exc

        recorded = RecordedCall(request)
        self.calls.append(recorded)

        # Find matching route in reverse registration order (latest rule takes precedence)
        for route in reversed(self._routes):
            if route.matches(recorded.method, recorded.url):
                if route.callback is not None:
                    res = route.callback(request)
                    if isinstance(res, Response):
                        return res

                resp = Response()
                resp.status_code = route.status_code
                resp.url = recorded.url
                resp.request = request

                resp_headers = dict(route.headers)
                if route.json_data is not None:
                    raw_bytes = json.dumps(route.json_data).encode("utf-8")
                    if "Content-Type" not in resp_headers and "content-type" not in resp_headers:
                        resp_headers["Content-Type"] = "application/json"
                elif route.text is not None:
                    raw_bytes = route.text.encode("utf-8")
                    if "Content-Type" not in resp_headers and "content-type" not in resp_headers:
                        resp_headers["Content-Type"] = "text/plain; charset=utf-8"
                else:
                    raw_bytes = b""

                resp.headers = CaseInsensitiveDict(resp_headers)
                resp.encoding = get_encoding_from_headers(resp.headers)

                raw = io.BytesIO(raw_bytes)
                msg = EmailMessage()
                for k, v in resp_headers.items():
                    msg[k] = v
                raw._original_response = _MockOriginalResponse(msg)
                resp.raw = raw

                resp.reason = http.client.responses.get(route.status_code, "OK")
                extract_cookies_to_jar(resp.cookies, request, resp.raw)

                return resp

        registered = [f"{r.method} {r.url_or_pattern}" for r in self._routes]
        raise ConnectionRefusedError(
            f"HttpMock blocked unhandled request: {recorded.method} {recorded.url}\n"
            f"Registered routes: {registered or 'None'}"
        )

    def assert_called(
        self,
        url_or_pattern: Optional[Union[str, Pattern[str]]] = None,
        method: Optional[str] = None,
        count: Optional[int] = None,
    ) -> None:
        """Assert that an expected HTTP call was recorded."""
        matching = []
        for call in self.calls:
            if method and call.method != method.upper():
                continue
            if url_or_pattern:
                if isinstance(url_or_pattern, str) and call.url != url_or_pattern:
                    continue
                if isinstance(url_or_pattern, re.Pattern) and not url_or_pattern.search(call.url):
                    continue
            matching.append(call)

        if count is not None:
            if len(matching) != count:
                raise AssertionError(
                    f"Expected {count} call(s) matching (method={method}, url={url_or_pattern}), "
                    f"found {len(matching)} call(s)."
                )
        else:
            if not matching:
                raise AssertionError(
                    f"Expected at least one call matching (method={method}, url={url_or_pattern}), "
                    f"but none was found among: {self.calls}."
                )


@contextmanager
def mock_http():
    """Context manager intercepting outbound HTTP requests at the adapter transport boundary.

    Operates below requests.Session.send to preserve Session response hook dispatch,
    automatic redirect resolution, cookie extraction, and response lifecycle handling.
    Uses scoped unittest.mock.patch.object without global mutable registries.
    """
    try:
        from requests.adapters import HTTPAdapter
    except ImportError as exc:
        raise RuntimeError("HTTP mocking requires 'requests' package to be installed.") from exc

    mock_instance = HttpMock()

    def _adapter_send_handler(adapter_self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        return mock_instance._handle_adapter_send(request, *args, **kwargs)

    with patch.object(HTTPAdapter, "send", _adapter_send_handler):
        yield mock_instance
