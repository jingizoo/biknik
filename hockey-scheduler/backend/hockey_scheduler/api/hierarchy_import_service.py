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

    @catch
    def get_hierarchy_import_codes(self) -> dict:
        """Existing persisted external codes for the import wizard's
        Program/League/Venue pickers (#260 review — Q2/Q3/Q6).

        Read-only: an incremental import's wizard can offer already-set-up
        entities alongside whatever codes are in the current upload draft.
        This is never a second source of truth — the wizard only uses these
        codes to decide which sheets/rows to show; every actual write still
        goes through the one canonical dry-run/commit path.
        """
        programs = [p for p in self.store.all_programs() if p.external_ref]
        programs_by_id = {p.id: p for p in self.store.all_programs()}
        seasons_by_id = {s.id: s for s in self.store.all_seasons()}
        leagues = [lg for lg in self.store.all_leagues() if lg.external_ref]
        venues = [v for v in self.store.all_venues() if v.external_ref]

        league_rows = []
        for lg in leagues:
            # #283: League.season_id dropped — a League now belongs directly to a
            # Program and participates in Seasons via LeagueSeason rows. Emit one
            # picker row per Season the League plays in (none -> a single row with
            # no season_code), with the Program taken from the League directly.
            program = programs_by_id.get(lg.program_id)
            program_code = program.external_ref if program is not None else None
            seasons_for_league = [
                seasons_by_id.get(ls.season_id)
                for ls in self.store.league_seasons_for_league(lg.id)] or [None]
            for season in seasons_for_league:
                league_rows.append({
                    "code": lg.external_ref, "name": lg.name,
                    "season_code": season.external_ref if season else None,
                    "program_code": program_code,
                })

        return {
            "programs": [{"code": p.external_ref, "name": p.name}
                        for p in programs],
            "leagues": league_rows,
            "venues": [{"code": v.external_ref, "name": v.name}
                      for v in venues],
        }

    @staticmethod
    def _has_hierarchy_payload(sheets_csv: dict) -> bool:
        body = sheets_csv or {}
        # #260 Slice F added a hierarchy `players` sheet whose CSV key
        # (`players_csv`) collides with the legacy teams+players importer's
        # own `players_csv` — the mere presence of that key can no longer
        # disambiguate which importer a submission means. `import_type:
        # "hierarchy"` is the only unambiguous signal now; every hierarchy
        # caller (frontend, e2e, tests) already sends it explicitly.
        return body.get("import_type") == "hierarchy"

    @staticmethod
    def _parse_hierarchy_payload(sheets_csv: dict) -> dict:
        body = sheets_csv or {}
        # `players_csv` is deliberately excluded: it's a legitimate sheet in
        # BOTH importers now (#260), so its presence alone is never a
        # collision — only genuinely hierarchy-exclusive legacy keys are.
        legacy_keys = (
            "teams_csv", "officials_csv",
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
        # today (#273): from the setup service's injected clock, so the
        # dry-run's future-birthdate answer matches the commit's exactly.
        return validate_hierarchy_import(
            self._parse_hierarchy_payload(sheets_csv), self.store,
            today=self.setup.clock().date())

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
