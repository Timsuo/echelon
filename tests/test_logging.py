import logging

from app.logging_setup import SecretFormatter


def test_secret_redaction_including_response():
    formatter = SecretFormatter(["token-secret", "api-secret"])
    record = logging.LogRecord("test", logging.ERROR, "", 0,
                               "raw_response=%r", ("token-secret api-secret sk-unknown",), None)
    formatted = formatter.format(record)
    assert "token-secret" not in formatted
    assert "api-secret" not in formatted
    assert "sk-unknown" not in formatted


def test_escaped_token_redaction():
    secret = "token\nwith'quote"
    formatter = SecretFormatter([secret])
    record = logging.LogRecord("test", logging.ERROR, "", 0, "%r", (secret,), None)
    assert "token" not in formatter.format(record)
