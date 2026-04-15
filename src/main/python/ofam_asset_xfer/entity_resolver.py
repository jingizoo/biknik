"""Maps Transfer-to-Entity DFF values to Oracle FA book type codes.

The DFF "Transfer to Entity" on a Fusion FA asset tells us which legal entity
the asset should be moved to.  This module resolves that entity name/code to
the corresponding FA book_type_code so we can determine whether a cross-book
or same-book transfer is needed.

Usage:
    resolver = EntityBookResolver({"US Entity": "US CORP BOOK", ...})
    book = resolver.resolve_target_book("US Entity")
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from .exceptions import ValidationError

log = logging.getLogger(__name__)


class EntityBookResolver:
    """Resolves a Transfer-to-Entity value to an FA book_type_code.

    Lookup is case-insensitive and strips whitespace.
    """

    def __init__(self, entity_book_map: Dict[str, str]):
        if not entity_book_map:
            raise ValidationError(
                "entity_book_map is required.  Provide a mapping of "
                "entity names/codes to FA book type codes."
            )
        # Normalise keys: upper + strip
        self._map: Dict[str, str] = {
            k.upper().strip(): v.strip() for k, v in entity_book_map.items()
        }

    @property
    def entity_book_map(self) -> Dict[str, str]:
        """Return a copy of the normalised entity-to-book mapping."""
        return dict(self._map)

    def resolve_target_book(self, entity_name: str) -> str:
        """Return the book_type_code for the given entity.

        Raises:
            ValidationError: if the entity is not in the mapping.
        """
        key = entity_name.upper().strip()
        if not key:
            raise ValidationError("Empty entity name provided")

        book = self._map.get(key)
        if not book:
            raise ValidationError(
                f"No book mapping for entity '{entity_name}'. "
                f"Available entities: {sorted(self._map.keys())}"
            )
        log.info("Entity resolved: '%s' → '%s'", entity_name, book)
        return book

    @staticmethod
    def from_json_file(path: str) -> "EntityBookResolver":
        """Load entity→book mapping from a JSON file.

        Expected format:  {"entity_name": "BOOK_TYPE_CODE", ...}
        """
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValidationError(
                f"Entity book map JSON must be a dict, got {type(data).__name__}"
            )
        return EntityBookResolver(data)

    @staticmethod
    def from_config(config: Dict[str, Any]) -> "EntityBookResolver":
        """Build from a config dict.

        Supports:
            {"entity_book_map": {"US Entity": "US CORP BOOK", ...}}
        or:
            {"entity_book_map_file": "/path/to/map.json"}
        """
        map_file = config.get("entity_book_map_file")
        if map_file:
            return EntityBookResolver.from_json_file(str(map_file))

        raw_map = config.get("entity_book_map")
        if isinstance(raw_map, dict) and raw_map:
            return EntityBookResolver(raw_map)

        raise ValidationError(
            "Config must contain 'entity_book_map' (dict) or 'entity_book_map_file' (path)."
        )
