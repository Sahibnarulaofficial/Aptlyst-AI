"""Schemas for chatbots."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.chatbot import ChatbotStatus


class ChatbotCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    website_url: str | None = Field(default=None, max_length=2048)
    welcome_message: str | None = Field(default=None, max_length=1000)
    instructions: str | None = Field(default=None, max_length=5000)
    primary_color: str | None = Field(default="#3b82f6", max_length=7)
    status: ChatbotStatus = Field(default=ChatbotStatus.draft)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    @field_validator("website_url", mode="before")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        if not (stripped.startswith("http://") or stripped.startswith("https://")):
            raise ValueError("Website URL must start with http:// or https://")
        return stripped

    @field_validator("primary_color", mode="before")
    @classmethod
    def validate_hex_color(cls, v: str | None) -> str | None:
        if v is None:
            return "#3b82f6"
        stripped = v.strip()
        if not stripped:
            return "#3b82f6"
        if not stripped.startswith("#") or len(stripped) not in (4, 7):
            raise ValueError("Primary color must be a valid hex color code (e.g. #3b82f6)")
        return stripped


class ChatbotUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    website_url: str | None = Field(default=None, max_length=2048)
    welcome_message: str | None = Field(default=None, max_length=1000)
    instructions: str | None = Field(default=None, max_length=5000)
    primary_color: str | None = Field(default=None, max_length=7)
    status: ChatbotStatus | None = Field(default=None)

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("Name cannot be null")
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                raise ValueError("Name cannot be empty")
            return stripped
        return v

    @field_validator("website_url", mode="before")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        if not (stripped.startswith("http://") or stripped.startswith("https://")):
            raise ValueError("Website URL must start with http:// or https://")
        return stripped

    @field_validator("primary_color", mode="before")
    @classmethod
    def validate_hex_color(cls, v: str | None) -> str | None:
        if v is None:
            return "#3b82f6"
        stripped = v.strip()
        if not stripped:
            return "#3b82f6"
        if not stripped.startswith("#") or len(stripped) not in (4, 7):
            raise ValueError("Primary color must be a valid hex color code (e.g. #3b82f6)")
        return stripped

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: str | None) -> ChatbotStatus | None:
        if v is None:
            raise ValueError("Status cannot be null")
        return v


class ChatbotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    description: str | None
    website_url: str | None
    welcome_message: str | None
    instructions: str | None
    primary_color: str | None
    status: ChatbotStatus
    conversations_count: int = 0
    leads_count: int = 0
    last_active: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PublicChatbotConfig(BaseModel):
    """Minimal, privacy-safe chatbot configuration for public website widgets.

    Exposes strictly the visual branding and identity needed to render the chat widget.
    Never exposes internal instructions, organization IDs, or operational metrics.
    """
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    welcome_message: str | None = None
    primary_color: str | None = "#3b82f6"
