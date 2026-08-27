#!/bin/zsh
# SCRATCH — apply each earlier falsifier to the LIVE code at the new head and
# record what goes red. Restores the tree after each one.
set -u
B=/private/tmp/claude-501/-Users-jalaj-biknik/619773d0-ffb7-412e-8ac2-d03fcfb648b5/scratchpad/wt427new/hockey-scheduler/backend
cd "$B" || exit 1
LOG=/tmp/falsifiers.log
: > "$LOG"

run() {  # run <name> <modules...>
  local name=$1; shift
  echo "=== RUN $name : $* ===" >> "$LOG"
  (cd "$B/tests" && python3 -m unittest "$@" 2>&1 |
     grep -E "^(OK|FAILED|Ran |FAIL:|ERROR:)|AssertionError" | head -12) >> "$LOG"
  echo >> "$LOG"
}

restore() { (cd "$B/.." && git checkout -- . ); }

apply() { python3 - "$@" <<'PY'
import sys, pathlib
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path); s = p.read_text()
assert s.count(old) == 1, (path, s.count(old))
p.write_text(s.replace(old, new))
PY
}

SWEEP=test_authenticated_side_noninterference

# ---- F5: the private-game family honours ?side=away -----------------------
echo "##### F5 ?side=away honoured at web/server.py" >> "$LOG"
apply hockey_scheduler/web/server.py \
'            own_team = private_read.own_team or ""
            side_ids = private_read.side_ids' \
'            own_team = private_read.own_team or ""
            side_ids = private_read.side_ids
            import urllib.parse as _up
            _q = _up.parse_qs(self.path.split("?", 1)[1]
                              if "?" in self.path else "")
            if _q.get("side", [""])[0] == "away" and side_ids[1]:
                own_team = side_ids[1]'
run F5 "$SWEEP" test_lineup_side_projection test_side_provenance_guard
restore

# ---- F6: delete the official assignment check -----------------------------
echo "##### F6 official assignment check deleted (game_side_scope.py)" >> "$LOG"
apply hockey_scheduler/services/game_side_scope.py \
'        admitted = official_id is not None and any(
            a.official_id == official_id
            for a in store.assignments_for_game(game_id))' \
'        admitted = official_id is not None'
run F6 "$SWEEP" test_lineup_side_projection
restore

# ---- F7: restore the group-only submitted filter --------------------------
echo "##### F7 group-only submitted filter restored (service.py)" >> "$LOG"
apply hockey_scheduler/api/service.py \
'                if row["group"] == "selected" and not row["backed_out"]]' \
'                if row["group"] == "selected"]'
run F7 "$SWEEP" test_private_game_sibling_routes
restore

# ---- F8: default_side_permitted always True -------------------------------
echo "##### F8 default_side_permitted always True (lineup_visibility.py)" >> "$LOG"
apply hockey_scheduler/services/lineup_visibility.py \
'    return role not in _TEAM_SCOPED


def own_side(' \
'    return True


def own_side('
run F8 "$SWEEP" test_lineup_side_projection test_private_game_sibling_routes
restore

echo "##### CLEAN TREE CONTROL" >> "$LOG"
run CONTROL "$SWEEP"
echo DONE >> "$LOG"
