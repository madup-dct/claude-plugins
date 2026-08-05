from __future__ import annotations

import unittest
from collections.abc import Iterable

from starlette.requests import Request

from mim_control_plane.http_body import (
    InvalidRequestBody,
    RequestBodyTooLarge,
    read_bounded_request_body,
)


class _Receive:
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = list(chunks)
        self.calls = 0

    async def __call__(self) -> dict[str, object]:
        self.calls += 1
        if not self._chunks:
            return {"type": "http.request", "body": b"", "more_body": False}
        chunk = self._chunks.pop(0)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": bool(self._chunks),
        }


def _request(
    *,
    receive: _Receive,
    content_lengths: tuple[bytes, ...] = (),
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> Request:
    headers = [
        *((b"content-length", value) for value in content_lengths),
        *extra_headers,
    ]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/test",
            "raw_path": b"/test",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 443),
        },
        receive,
    )


class BoundedRequestBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_declared_oversize_is_rejected_without_reading_stream(self) -> None:
        receive = _Receive((b"not-read",))

        with self.assertRaises(RequestBodyTooLarge):
            await read_bounded_request_body(
                _request(receive=receive, content_lengths=(b"6",)),
                max_bytes=5,
            )

        self.assertEqual(receive.calls, 0)

    async def test_chunked_oversize_stops_at_first_chunk_over_limit(self) -> None:
        receive = _Receive((b"abc", b"def", b"not-read"))

        with self.assertRaises(RequestBodyTooLarge):
            await read_bounded_request_body(
                _request(receive=receive),
                max_bytes=5,
            )

        self.assertEqual(receive.calls, 2)

    async def test_exact_limit_is_accepted(self) -> None:
        receive = _Receive((b"ab", b"cde"))

        body = await read_bounded_request_body(
            _request(receive=receive, content_lengths=(b"5",)),
            max_bytes=5,
        )

        self.assertEqual(body, b"abcde")
        self.assertEqual(receive.calls, 2)

    async def test_missing_content_length_allows_bounded_streaming(self) -> None:
        receive = _Receive((b"abc",))

        body = await read_bounded_request_body(
            _request(receive=receive),
            max_bytes=5,
        )

        self.assertEqual(body, b"abc")

    async def test_malformed_content_length_is_rejected_without_reading(self) -> None:
        for value in (b"", b"-1", b"+1", b" 1", b"1 ", b"1, 1", b"x"):
            with self.subTest(value=value):
                receive = _Receive((b"x",))
                with self.assertRaises(InvalidRequestBody):
                    await read_bounded_request_body(
                        _request(receive=receive, content_lengths=(value,)),
                        max_bytes=5,
                    )
                self.assertEqual(receive.calls, 0)

    async def test_duplicate_content_length_is_rejected_without_reading(self) -> None:
        receive = _Receive((b"x",))

        with self.assertRaises(InvalidRequestBody):
            await read_bounded_request_body(
                _request(receive=receive, content_lengths=(b"1", b"1")),
                max_bytes=5,
            )

        self.assertEqual(receive.calls, 0)

    async def test_content_length_with_transfer_encoding_is_rejected(self) -> None:
        receive = _Receive((b"x",))

        with self.assertRaises(InvalidRequestBody):
            await read_bounded_request_body(
                _request(
                    receive=receive,
                    content_lengths=(b"1",),
                    extra_headers=((b"transfer-encoding", b"chunked"),),
                ),
                max_bytes=5,
            )

        self.assertEqual(receive.calls, 0)

    async def test_declared_length_must_match_received_body(self) -> None:
        receive = _Receive((b"abc",))

        with self.assertRaises(InvalidRequestBody):
            await read_bounded_request_body(
                _request(receive=receive, content_lengths=(b"4",)),
                max_bytes=5,
            )


if __name__ == "__main__":
    unittest.main()
