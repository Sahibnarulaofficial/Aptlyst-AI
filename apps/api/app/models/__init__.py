"""Centralized SQLAlchemy ORM model registry for Aptlyst AI.

Ensures every SQLAlchemy model referenced by string-based `relationship(...)`
definitions across the shared declarative `Base.registry` is imported in a
deterministic, circular-import-safe order before mapper configuration or ORM
operations occur (especially in standalone worker and CLI processes).
"""

from __future__ import annotations

import importlib

MODEL_MODULE_NAMES: tuple[str, ...] = (
    "app.models.organization",
    "app.models.chatbot",
    "app.models.conversation",
    "app.models.generation_attempt",
    "app.models.generation",
    "app.models.billing",
    "app.models.subscription",
    "app.models.payment",
)


def register_all_models() -> tuple[str, ...]:
    """Idempotently import every SQLAlchemy ORM module into Base.registry.

    Safe to call from standalone workers, CLI tools, migrations, and tests
    regardless of which model submodule was imported first.
    """
    for module_name in MODEL_MODULE_NAMES:
        importlib.import_module(module_name)
    return MODEL_MODULE_NAMES


register_all_models()

__all__ = [
    "MODEL_MODULE_NAMES",
    "register_all_models",
]
