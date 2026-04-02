"""
suggester.py — Suggester layer
==============================
Pluggable label suggestion system.  New suggesters slot in by subclassing
:class:`BaseSuggester` and implementing :meth:`~BaseSuggester.suggest`.

Role in the system:
    - Instantiated once at launch (selected via CLI flag ``--suggester``).
    - Called by ``api.py`` when serving a frame to the frontend.
    - When active, the frontend displays the suggestion as a subtle chip.
    - When the suggester returns ``None`` (or is toggled off), nothing is shown.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Maps CLI name → suggester class.  Register new suggesters here.
_REGISTRY: dict[str, type[BaseSuggester]] = {}


def register(name: str):
    """Class decorator that registers a suggester under *name*."""
    def decorator(cls: type[BaseSuggester]) -> type[BaseSuggester]:
        _REGISTRY[name] = cls
        return cls
    return decorator


def get_suggester(name: str) -> "BaseSuggester":
    """Instantiate and return the suggester registered under *name*.

    Args:
        name: The CLI name (e.g. ``"dummy"``).

    Returns:
        A fresh :class:`BaseSuggester` instance.

    Raises:
        ValueError: If *name* is not registered.
    """
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown suggester {name!r}. Available: {available}")
    return _REGISTRY[name]()


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class BaseSuggester(ABC):
    """Abstract base for all label suggesters.

    Subclasses must implement :meth:`suggest`.  Everything else is optional.
    """

    @abstractmethod
    def suggest(
        self,
        experiment_id: int,
        timestep: int,
        context: Optional[dict] = None,
    ) -> Optional[str]:
        """Return a suggested label string, or ``None`` if no suggestion.

        Args:
            experiment_id: The experiment the frame belongs to.
            timestep: The frame's timestep.
            context: Optional dict of extra information (e.g. neighbouring
                labels, physics parameters).  Suggesters may ignore this.

        Returns:
            One of the :data:`~labeler.db.LABELS` strings, or ``None``.
        """


# ---------------------------------------------------------------------------
# DummySuggester — always returns None
# ---------------------------------------------------------------------------

@register("dummy")
class DummySuggester(BaseSuggester):
    """No-op suggester.  Always returns ``None`` — nothing is shown in the UI.

    This is the default suggester used when no ``--suggester`` flag is given,
    or when the user toggles suggestions off at runtime.
    """

    def suggest(
        self,
        experiment_id: int,
        timestep: int,
        context: Optional[dict] = None,
    ) -> Optional[str]:
        return None
