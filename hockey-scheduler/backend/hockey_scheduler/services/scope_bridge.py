"""The single canonical→tenancy bridge for scheduling scope (#233 Slice C1b).

The competition-model rename made the domain canonical (``Program`` with
``operator_organization_id``; ``Season.program_id``; ``Team.program_id``), but
the tenancy/scheduling layer (``league_scope`` and the league-scoped setup
service) predates that rename and speaks its own frozen vocabulary — a scheduling
"scope id" (historically the umbrella-league id, now the Program id), its
``league_id`` result keys, and its ``league_organization_id`` claim key.

The locked decision is "entities-only, ONE conversion": the canonical competition
field/method names appear in exactly one place for tenancy — here. Every tenancy
call site reads a scope id or the store's scope entity through these tiny
accessors instead of touching ``.program_id`` / ``.operator_organization_id`` /
``store.get_program`` directly, so the seam can never drift and the rename cannot
leak the canonical names into the tenancy layer's own vocabulary.

These are deliberately thin: they name the one canonical attribute/method each
maps to and nothing else. The tenancy layer keeps its own variable/result-key
names (``league_id``, scope ids, claims) unchanged.
"""


def season_scope_id(season):
    """The scheduling scope id a Season belongs to (canonical ``program_id``)."""
    return season.program_id


def team_scope_id(team):
    """The scheduling scope id a Team belongs to (canonical ``program_id``)."""
    return team.program_id


def program_operator_id(program):
    """A scope entity's operating-organization id (canonical
    ``operator_organization_id``)."""
    return program.operator_organization_id


def get_scope_program(store, scope_id):
    """Resolve a scheduling scope id to its scope entity (canonical
    ``store.get_program``), or ``None`` when it names no scope."""
    return store.get_program(scope_id)
