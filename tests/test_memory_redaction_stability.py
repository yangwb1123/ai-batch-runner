"""Repeated memory/Gateway redaction preserves markers without exposing suffixes."""

import pytest

from pbatch.memory_records import redact, redact_content


@pytest.mark.parametrize("source", [
    "password=PRIVATE_VALUE", "token='PRIVATE_VALUE'", "token=PRIVATE_VALUE]",
    "Authorization: Bearer PRIVATE_VALUE", '"api_key": "PRIVATE_VALUE"',
    "password=[REDACTED]PRIVATE_VALUE", "password=[REDACTED]",
])
def test_repeated_secret_redaction_is_stable_and_keeps_private_values_hidden(source):
    first = redact(source)
    assert "PRIVATE_VALUE" not in first
    current = first
    for _ in range(8):
        current = redact(current)
        assert current == first


def test_nested_command_output_remains_identical_after_each_transport_boundary():
    value = {"output_text": "failure\npassword=PRIVATE_VALUE\n<script>plain text</script>",
             "rows": [{"text": "token=PRIVATE_VALUE"}], "access_token": "PRIVATE_VALUE"}
    once = redact_content(value)
    assert once["output_text"] == "failure\npassword=[REDACTED]\n<script>plain text</script>"
    assert "PRIVATE_VALUE" not in repr(once)
    assert redact_content(redact_content(once)) == once
