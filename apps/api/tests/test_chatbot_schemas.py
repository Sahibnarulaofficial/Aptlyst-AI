"""Unit tests for Chatbot Pydantic schema input validation."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.chatbot import ChatbotCreate, ChatbotUpdate, DEFAULT_PRIMARY_COLOR


@pytest.mark.parametrize(
    "raw_color,expected",
    [
        ("#3b82f6", "#3b82f6"),
        ("#123", "#123"),
        ("#ABC", "#ABC"),
        ("#00ff88", "#00ff88"),
        ("#1A2B3C", "#1A2B3C"),
        ("  #10b981  ", "#10b981"),
        (None, DEFAULT_PRIMARY_COLOR),
        ("", DEFAULT_PRIMARY_COLOR),
        ("   ", DEFAULT_PRIMARY_COLOR),
    ],
)
def test_chatbot_schemas_accept_valid_hex_colors(raw_color: str | None, expected: str) -> None:
    """Valid 3-digit and 6-digit hex colors, plus empty/None defaults, are accepted consistently."""
    created = ChatbotCreate(name="Color Bot", primary_color=raw_color)
    updated = ChatbotUpdate(primary_color=raw_color)

    assert created.primary_color == expected
    assert updated.primary_color == expected


@pytest.mark.parametrize(
    "invalid_color",
    [
        "#zzzzzz",
        "#ggg",
        "#12g",
        "#12345z",
        "#12345",
        "#1234567",
        "#!@#$%^",
        "#      ",
        "3b82f6",
        "blue",
        "rgb(59, 130, 246)",
        "javascript:alert(1)",
        "#123456;background:url(https://evil.example)",
        123456,
        ["#3b82f6"],
        {"hex": "#3b82f6"},
        True,
    ],
)
def test_chatbot_schemas_reject_invalid_or_unsafe_hex_colors(invalid_color: Any) -> None:
    """Malformed, non-hex, CSS-injection, and non-string primary_color values raise ValidationError."""
    with pytest.raises(ValidationError):
        ChatbotCreate(name="Bad Color Bot", primary_color=invalid_color)

    with pytest.raises(ValidationError):
        ChatbotUpdate(primary_color=invalid_color)


@pytest.mark.parametrize("bad_url", [123, ["https://example.com"], "ftp://example.com", "javascript:alert(1)"])
def test_chatbot_schemas_reject_invalid_website_url_types_and_schemes(bad_url: Any) -> None:
    """Invalid schemes and non-string website_url inputs raise clean ValidationErrors rather than AttributeError."""
    with pytest.raises(ValidationError):
        ChatbotCreate(name="Bad URL Bot", website_url=bad_url)

    with pytest.raises(ValidationError):
        ChatbotUpdate(website_url=bad_url)
