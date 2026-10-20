"""Schemas (Pydantic models) for organizations and membership."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.organization import MemberRole


class OrganizationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_id: uuid.UUID
    user_id: uuid.UUID
    role: MemberRole
    created_at: datetime


class OrganizationWithRoleOut(BaseModel):
    """Organization summary including the current user's role."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    role: MemberRole
    created_at: datetime
    updated_at: datetime
