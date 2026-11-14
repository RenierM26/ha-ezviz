"""Qualify an SDK candidate's real switch fallback through HA error classification.

Run from the repository root with a native HA interpreter and the candidate SDK
wheel installed in an isolated environment (or on PYTHONPATH). SDK 2.1.0 is an
intentional negative control: it erases the primary HTTP cause and fails here.
Only requests transport is mocked; SDK wrapping, fallback and HA classification
are real. This does not operate devices or modify the environment.
"""

from importlib import import_module
from unittest.mock import patch

from pyezvizapi import EzvizClient
from pyezvizapi.exceptions import HTTPError
import requests


def verify(primary: int, fallback: int) -> None:
    """Keep the first status even when the fallback has a contradictory status."""
    errors = import_module("custom_components.ezviz_cloud.errors")
    client = EzvizClient(token={"session_id": "synthetic", "api_url": "private.invalid"})
    responses = []
    for status in (primary, fallback):
        response = requests.Response()
        response.status_code = status
        response.url = "https://private.invalid/?token=SECRET_MARKER"
        response._content = b"SECRET_MARKER"
        responses.append(response)
    try:
        with patch.object(client._session, "request", side_effect=responses) as transport:
            try:
                client.switch_status("SYNTHETIC", 7, True)
            except HTTPError as error:
                key = "request_forbidden" if primary == 403 else "request_timeout"
                expected = (key, f"HTTP {primary}")
                actual = errors.error_category(error)
                assert actual == expected, (primary, fallback, actual, expected)
                translated = errors.request_error("Turn on light", error)
                assert translated.translation_key == key
                assert translated.translation_placeholders == {
                    "operation": "Turn on light", "detail": f"HTTP {primary}"
                }
            else:
                raise AssertionError("Both HTTP failures must reject the action")
            assert [call.kwargs["method"] for call in transport.call_args_list] == ["PUT", "POST"]
        print(f"PUT {primary} / POST {fallback}: {key}, HTTP {primary}")
    finally:
        client.close_session()


if __name__ == "__main__":
    for first, second in ((403, 403), (504, 504), (403, 504), (504, 403)):
        verify(first, second)
    print("PASS: all four real SDK fallback classifications retain the primary status")
