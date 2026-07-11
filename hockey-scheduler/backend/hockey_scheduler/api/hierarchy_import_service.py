"""API facade layer for the hierarchy import (#174 PR E2).

The HTTP server already has a League-Admin-only transactional import envelope at
``/api/import/commit/teams-players``. To preserve existing routes while the
stdlib router remains intentionally explicit, hierarchy requests declare
``import_type: hierarchy`` and use ``dry_run: true`` for preview. Existing
teams/players requests continue through the base facade unchanged.
"""

from ..domain.errors import ValidationError
from ..services.hierarchy_import import (
    HIERARCHY_CSV_KEYS,
    HIERARCHY_TEMPLATES,
    commit_hierarchy_import,
    validate_hierarchy_import,
)
from ..services.import_validator import parse_csv_text
from .league_scoped_service import ApiService as _BaseApiService
from .service import catch


class ApiService(_BaseApiService):
    """API facade with idempotent hierarchy import dispatch."""

    @staticmethod
    def hierarchy_import_templates() -> dict:
        return dict(HIERARCHY_TEMPLATES)

    @staticmethod
    def _has_hierarchy_payload(sheets_csv: dict) -> bool:
        body = sheets_csv or {}
        return body.get("import_type") == "hierarchy" or any(
            body.get(key) for key in HIERARCHY_CSV_KEYS)

    @staticmethod
    def _parse_hierarchy_payload(sheets_csv: dict) -> dict:
        body = sheets_csv or {}
        legacy_keys = (
            "teams_csv", "players_csv", "officials_csv",
            "official_availability_csv", "rinks_csv", "ice_slots_csv")
        mixed = [key for key in legacy_keys if body.get(key)]
        if mixed:
            raise ValidationError(
                "Hierarchy import cannot be mixed with existing onboarding "
                f"sheets: {', '.join(mixed)}.")
        parsed = {}
        for key in HIERARCHY_CSV_KEYS:
            text = body.get(key)
            if not text:
                continue
            if not isinstance(text, str):
                raise ValidationError(f"{key} must be a CSV text string.")
            parsed[key[:-4]] = parse_csv_text(text)
        return parsed

    @catch
    def get_hierarchy_import_dry_run(self, sheets_csv: dict) -> dict:
        return validate_hierarchy_import(
            self._parse_hierarchy_payload(sheets_csv), self.store)

    @catch
    def commit_hierarchy_import(self, sheets_csv: dict,
                                actor_id=None) -> dict:
        sheets = self._parse_hierarchy_payload(sheets_csv)
        return commit_hierarchy_import(self.setup, sheets, actor_id=actor_id)

    @catch
    def commit_teams_players_import(self, season_id: str, sheets_csv: dict,
                                    actor_id=None) -> dict:
        if self._has_hierarchy_payload(sheets_csv):
            if bool((sheets_csv or {}).get("dry_run")):
                return self.get_hierarchy_import_dry_run(sheets_csv)
            return self.commit_hierarchy_import(sheets_csv, actor_id=actor_id)
        return super().commit_teams_players_import(
            season_id, sheets_csv, actor_id=actor_id)
