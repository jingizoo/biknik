// THE SHARED FIXTURE BOUNDARY for #409's explicit-selection contract.
//
// Why this file exists. Nearly every browser journey opened with the same
// copy-pasted block:
//
//     const post = async (p, b) => (await fetch(p, { method: "POST", ... })).json();
//     const program = await post("/api/v2/setup/program", { name: "..." });
//     const season  = await post("/api/v2/setup/season", { program_id: program.id, ... });
//     const league  = await post("/api/v2/setup/league", { season_id: season.id, ... });
//     ...
//
// Two defects, both shared, and neither is a production defect:
//
//  1. NO EXPLICIT SELECTION. The chain leaned on the server INFERRING context
//     from the parent it named (or from the account that had just minted that
//     parent). #409 removes that inference, so the Season create is refused and
//     every descendant with it. This is one fixture boundary, repeated ~40
//     times — not forty product bugs.
//  2. NO RESPONSE ASSERTION. `post()` decoded the body and threw the STATUS
//     away, so a refusal's perfectly well-formed error body was indistinguishable
//     from a success until an `undefined` id surfaced somewhere far downstream —
//     as a 15s `waitForFunction` timeout, an `AttributeError: 'NoneType'` inside
//     a SQLite injector, or a `KeyError` naming an environment variable. The
//     diagnosis cost of a refused create was enormous and always paid at the
//     wrong line.
//
// Fixing (1) alone would be a trap: a journey could then pass because SOME
// earlier call happened to leave a usable row saved, which is the very
// inference #409 exists to remove. So this module makes both explicit and makes
// both ASSERTED: `selectContext` reads the selection BACK and fails if it is not
// exactly what was asked for, and `create` fails at its own line, naming itself.
//
// THE AXIS BOUNDARIES this module is written against (ApiService's
// `_CREATE_CONSUMED_AXES` is the authority; keep these in step with it):
//
//   ZERO-AXIS ROOT  organization, program, club
//                   No selection needed — nothing to be explicit about.
//   PROGRAM-AXIS    season, venue, rink, ice_slot, official, team, player
//                   Needs a PERSISTED Program. `selectProgram(...)` is enough.
//   SEASON-OWNED    league, league_season, division, game, registration,
//                   season_venue_access
//                   Needs a PERSISTED Program AND the persisted Season it
//                   writes into. `selectProgramSeason(...)`.
//
// AND THE OTHER TABLE, which is NOT that one. A create is judged by
// `_CREATE_CONSUMED_AXES`; an ARCHIVE / REOPEN / DELETE — anything routed
// through `setup_guarded_mutation` — is judged by `_mutation_context_error`
// against a DIFFERENT classification, and the two disagree on real kinds. The
// clearest disagreement, and the one that produced a real failure here:
//
//     kind      CREATE rule            MUTATION rule
//     season    PROGRAM-AXIS           SEASON-OWNED  (the record IS the axis)
//     league    SEASON-OWNED           PROGRAM-AXIS  (permanent across Seasons)
//
// So `selectProgram(...)` is exactly right before creating a Season and exactly
// WRONG before archiving one, and a journey that reached for the create helper
// out of habit would be refused with a message about a rule it had already
// satisfied. `selectForMutation(what, kind, ...)` below takes the KIND and
// applies the mutation table, so the choice is made from the production rule
// rather than from which helper was nearest to hand.
//
// So the bootstrap is three explicit acts, in this order, and no fewer:
//
//     create the Program                     (zero-axis root)
//     selectProgram(program.id)              <-- persists the Program-only choice
//     create the Season                      (Program-axis)
//     selectProgramSeason(program.id, s.id)  <-- persists both axes
//     create Leagues / Divisions / registrations ...
//
// A KNOWN LIMIT OF THIS MODULE, recorded here because three journeys depend on
// it and the gap is invisible at each individual call site.
//
// `selectProgram` is reachable from a FIXTURE in every state. It is NOT
// reachable from an OPERATOR in one of them: an installation holding exactly
// ONE Program with ZERO Seasons. `contextEntries` emits one entry per Program
// plus one per Season, so that state yields a single entry;
// `renderContextSwitcher`'s `single` branch then sets `select.hidden = true`
// and paints a non-interactive chip; and `setActiveContext` — the only thing
// that PERSISTS an ActiveContext row — is wired to exactly two handlers,
// `#ctx-select.onchange` and `#ctx-league-select.onchange` (app.js:13461 and
// :13481), both of which are hidden in that state. Measured, driving only
// shipped controls: zero interactive controls in `#context-switcher`, and a
// full reload does not help. Meanwhile `GET /api/context` answers
// `program_id: <the Program>` from the FALLBACK resolver, so the app tells the
// operator the Program is already selected while nothing is saved.
//
// The consequence is that a first Season cannot be created through the shipped
// UI: the Season drawer is a PROGRAM-AXIS create and is refused 409
// `active_context_required` — "Select a Program before creating records in
// it." — with no control able to satisfy it.
//
// So a journey that calls `selectProgram` in that exact state is issuing a
// selection an operator cannot, and is therefore proving the surfaces BELOW
// the bootstrap while CONCEALING the bootstrap itself. Three journeys do this
// today (allowed-venues.js, player-edit.js, v1-dependency-proof.js). That is a
// deliberate, recorded trade, not an accident — but it is only sound while the
// affordance gap is tracked as a product defect. Journeys whose SUBJECT is the
// bootstrap (season-dates.js) must NOT paper over it.
//
// This limit does not apply once the install holds a second entry — another
// Program, or a Season — because the real `#ctx-select` then renders and its
// "Program overview (no season)" option is genuinely selectable.
//
// WHY `setActiveContext` AND NOT A RAW `POST /api/context`. Every consumer of
// this module goes on to drive real UI on the SAME page — Setup cards, the
// hierarchy tree, drawers, the scheduler. `setActiveContext` is the app's own
// switch pipeline, the exact function `#ctx-select`'s onchange calls: it
// invalidates context-scoped mutations, bumps `contextRevision`, abandons focus
// work for the departing tuple and re-renders. A raw fetch would move the SERVER
// while the client went on believing the old tuple — a state no operator can
// reach — so a journey passing that way would prove nothing about the product
// and would hide any regression in that invalidation.

// THE MUTATION AXIS TABLE, mirroring ApiService's
// `_SEASON_OWNED_TARGET_KINDS` / `_PROGRAM_AXIS_TARGET_KINDS` (service.py).
// Kept as data, and kept TOTAL, so a kind that production classifies but this
// module does not is a loud fixture error naming the kind rather than a silent
// fall-through to the weaker rule.
//
// The two BRIDGE pseudo-kinds are folded exactly as `_mutation_axis_targets`
// folds them, to the parent kind they are JUDGED BY — `registration` by its
// LeagueSeason and `season_venue_access` by its Season. Both land Season-owned,
// but they are listed explicitly rather than left to a default, because a
// default would also silently absorb a future bridge that folds the other way.
const MUTATION_SEASON_OWNED = new Set([
  "season", "league_season", "division", "game",
  "registration", "season_venue_access",   // bridges, folded to the above
]);
const MUTATION_PROGRAM_AXIS = new Set([
  "program", "league", "team", "player", "club", "official",
  "venue", "rink", "ice_slot", "organization",
]);

const FIXTURE_SOURCE = `
(() => {
  if (window.hsFixture) return;   // idempotent: addInitScript + evaluate
  const MUTATION_SEASON_OWNED = new Set(${JSON.stringify([...MUTATION_SEASON_OWNED])});
  const MUTATION_PROGRAM_AXIS = new Set(${JSON.stringify([...MUTATION_PROGRAM_AXIS])});
  const jsonPost = async (path, body) => {
    const r = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    // A refusal answers well-formed JSON; a 500 may not. Never let the decode
    // itself be the thing that throws, or the status is lost with it.
    let decoded = null;
    try { decoded = await r.json(); } catch (_) { decoded = null; }
    return { status: r.status, body: decoded };
  };
  const readContext = async () => {
    const r = await fetch("/api/context", { credentials: "same-origin" });
    let decoded = null;
    try { decoded = await r.json(); } catch (_) { decoded = null; }
    return { status: r.status, body: decoded || {} };
  };
  window.hsFixture = {
    post: jsonPost,
    // Every create is ASSERTED before its id can be used. A refused create no
    // longer yields an id-less body that gets threaded into the next parent.
    async create(what, path, body) {
      const { status, body: got } = await jsonPost(path, body);
      if (status !== 200 || !got || got.error || !got.id) {
        throw new Error("fixture create of " + what + " (" + path + ") failed: "
          + status + " " + JSON.stringify(got));
      }
      return got;
    },
    // Non-create fixture calls (register/remove/commit endpoints) that still
    // must not be allowed to fail silently. No id is required — only success.
    async call(what, path, body) {
      const { status, body: got } = await jsonPost(path, body);
      if (status !== 200 || !got || got.error) {
        throw new Error("fixture call " + what + " (" + path + ") failed: "
          + status + " " + JSON.stringify(got));
      }
      return got;
    },
    // THE RELOAD-BOUNDARY SELECTION. Same assertions as \`selectContext\` — the
    // write echo AND the read-back — but it does NOT call \`setActiveContext\`,
    // so it starts no render pass.
    //
    // Use it ONLY where the very next thing the journey does is a full page
    // load (\`freshLoad\`/\`reenter\`/\`page.goto\`). There the client-side desync
    // \`selectContext\` exists to prevent cannot occur: nothing keeps rendering
    // against the old tuple, because the whole page is about to be rebuilt
    // from the persisted one. This is NOT a softer selection — it persists and
    // proves exactly the same row.
    //
    // It exists because driving \`setActiveContext\` from pure DATA SETUP has a
    // real cost: the render it starts runs CONCURRENTLY with the rest of the
    // fixture, so a block that goes on to archive a Season, remove a
    // registration or switch persona is mutating rows out from under an
    // in-flight pass, and the app then reports refusals that are correct
    // answers to questions the fixture never meant to ask. Where the journey
    // keeps driving the SAME page, \`selectContext\` is still the right one.
    async selectPersisted(what, programId, seasonId) {
      const wantProgram = programId || null;
      const wantSeason = seasonId || null;
      const { status, body: echoed } = await jsonPost(
        "/api/context", { program_id: wantProgram, season_id: wantSeason });
      if (status !== 200 || !echoed || echoed.error) {
        throw new Error(what + ": the selection was REFUSED: " + status + " "
          + JSON.stringify(echoed));
      }
      if ((echoed.program_id || null) !== wantProgram
          || (echoed.season_id || null) !== wantSeason) {
        throw new Error(what + ": the server committed a DIFFERENT tuple than "
          + "was asked for — wanted program=" + wantProgram + " season="
          + wantSeason + ", committed program=" + (echoed.program_id || null)
          + " season=" + (echoed.season_id || null));
      }
      const seen = await readContext();
      if (seen.status !== 200 || !seen.body
          || (seen.body.program_id || null) !== wantProgram
          || (seen.body.season_id || null) !== wantSeason) {
        throw new Error(what + ": the active context did not read back as the "
          + "explicit selection — wanted program=" + wantProgram + " season="
          + wantSeason + ", got " + JSON.stringify(seen.body));
      }
      return seen.body;
    },
    // A DROP-IN for the copy-pasted \`post()\` anti-helper, for the journeys
    // that carry dozens of its call sites. It is not a softer \`create\`: it
    // asserts the STATUS the original threw away, so a 409 refusal fails at
    // its own line instead of surfacing later as an undefined id. It does not
    // demand an \`id\`, because the same helper is used for register/remove/
    // commit calls that legitimately return none — where an id IS expected,
    // prefer \`create\`, which additionally requires it.
    //
    // It also intercepts \`POST /api/context\`, the one path that must NEVER be
    // a raw fetch: a raw context POST moves the SERVER while the page goes on
    // believing the old tuple, and every consumer here drives real UI next.
    // Routing it through \`selectContext\` makes it the app's own switch AND
    // makes it asserted, so a selection that silently failed to persist can no
    // longer be what a later create rides on.
    async auto(path, body) {
      const b = body || {};
      if (path.split("?")[0].endsWith("/api/context")) {
        // \`auto\` is the DATA-SETUP helper, and its consumers reload before
        // driving UI again, so this is the RELOAD-BOUNDARY selection: equally
        // asserted, but it starts no render pass for the rest of the fixture
        // to race against. Call \`selectContext\` directly wherever a journey
        // keeps working the SAME page.
        return window.hsFixture.selectPersisted(
          "select " + JSON.stringify(
            { program: b.program_id || null, season: b.season_id || null }),
          b.program_id || null, b.season_id || null);
      }
      const { status, body: got } = await jsonPost(path, b);
      if (status !== 200 || !got || got.error) {
        throw new Error("fixture POST " + path + " failed: " + status + " "
          + JSON.stringify(got));
      }
      return got;
    },
    // THE EXPLICIT SELECTION, driven through the app's own switch pipeline and
    // then proved TWO ways. Both are needed, and the first is the load-bearing
    // one:
    //
    //  (a) THE WRITE ECHO. \`POST /api/context\` is answered by
    //      \`set_active_context\`, i.e. by the code path that PERSISTS the row,
    //      and its body renders the tuple that was actually committed. A 200
    //      whose echo matches is direct evidence the ActiveContext row now
    //      holds these axes.
    //
    //  (b) the subsequent \`GET /api/context\` agreeing with it.
    //
    // (b) ALONE WOULD BE A FALSE ORACLE, which is exactly the trap this whole
    // issue is about: \`get_active_context\` resolves through
    // \`resolve_with_league\`, the FALLBACK resolver. On a one-Program install
    // it happily reports that Program with nothing saved at all, so a GET that
    // "reads back correctly" is consistent with no selection ever having been
    // made — "an equal value is not a derived one", in the production code's
    // own words. The echo is what distinguishes them, so a missing echo is a
    // hard failure here even when the GET looks perfect.
    async selectContext(what, programId, seasonId, via) {
      const wantProgram = programId || null;
      const wantSeason = seasonId || null;
      const driver = via || "app";
      if (driver !== "app" && driver !== "api") {
        throw new Error(what + ": unknown selection driver \\"" + via + "\\"");
      }
      let committed;
      if (driver === "api") {
        // OUT-OF-BAND actor: issue the write directly. The echo is read from
        // this response, so the load-bearing half of the oracle below is
        // exactly the same evidence — \`POST /api/context\` is answered by
        // \`set_active_context\`, the code path that PERSISTS the row, whoever
        // called it.
        const direct = await jsonPost("/api/context",
          { program_id: wantProgram, season_id: wantSeason });
        committed = { status: direct.status, body: direct.body };
      } else {
        // Observe the app's own POST rather than issuing one: \`fetch\` is looked
        // up globally at call time inside app.js's \`post()\`, so wrapping it here
        // captures the real switch request without replacing it. Restored in a
        // \`finally\` so a throw cannot leave the page running on a patched fetch.
        const realFetch = window.fetch;
        const echoes = [];
        window.fetch = async function (input, init) {
          const response = await realFetch.call(this, input, init);
          const url = typeof input === "string" ? input : ((input && input.url) || "");
          const method = ((init && init.method) || "GET").toUpperCase();
          if (method === "POST" && url.split("?")[0].endsWith("/api/context")) {
            let echoed = null;
            try { echoed = await response.clone().json(); } catch (_) { echoed = null; }
            echoes.push({ status: response.status, body: echoed });
          }
          return response;
        };
        try {
          await setActiveContext(wantProgram, wantSeason);
        } finally {
          window.fetch = realFetch;
        }
        committed = echoes[echoes.length - 1];
      }
      if (!committed) {
        throw new Error(what + ": no POST /api/context was issued, so nothing "
          + "was PERSISTED — a later create that succeeds would be riding on "
          + "inferred context, not on this selection");
      }
      if (committed.status !== 200 || !committed.body || committed.body.error) {
        throw new Error(what + ": the selection was REFUSED: "
          + committed.status + " " + JSON.stringify(committed.body));
      }
      if ((committed.body.program_id || null) !== wantProgram
          || (committed.body.season_id || null) !== wantSeason) {
        throw new Error(what + ": the server committed a DIFFERENT tuple than "
          + "was asked for — wanted program=" + wantProgram + " season="
          + wantSeason + ", committed program="
          + (committed.body.program_id || null) + " season="
          + (committed.body.season_id || null));
      }
      // setActiveContext RETURNS EARLY when another switch is already in
      // flight, queueing this one behind it, so a read can lag the commit by a
      // round trip. Converge with a bounded wait and then assert HARD — the
      // assertion is not softened, only given the chance to observe a settled
      // server instead of racing one.
      const deadline = Date.now() + 5000;
      let seen = await readContext();
      while (Date.now() < deadline
             && (seen.status !== 200
                 || (seen.body.program_id || null) !== wantProgram
                 || (seen.body.season_id || null) !== wantSeason)) {
        await new Promise((res) => setTimeout(res, 50));
        seen = await readContext();
      }
      if (seen.status !== 200 || !seen.body || seen.body.error) {
        throw new Error(what + ": could not read the active context back: "
          + seen.status + " " + JSON.stringify(seen.body));
      }
      if ((seen.body.program_id || null) !== wantProgram
          || (seen.body.season_id || null) !== wantSeason) {
        throw new Error(what + ": the active context did not read back as the "
          + "explicit selection — wanted program=" + wantProgram
          + " season=" + wantSeason + ", got program="
          + (seen.body.program_id || null) + " season="
          + (seen.body.season_id || null));
      }
      return seen.body;
    },
    // The Program-only selection: enough for a PROGRAM-AXIS create (Season,
    // Team, Venue, Rink, Ice slot, Official, Player). Season is explicitly
    // cleared, never merely left alone, so a leftover Season from an earlier
    // block cannot be what a later two-axis create silently rides on.
    selectProgram(what, programId, via) {
      return window.hsFixture.selectContext(what, programId, null, via);
    },
    // Both axes: required by every SEASON-OWNED create (League, Division,
    // Game, registration, Season venue access). The Season selected here must
    // be the SAME Season the create writes into — the comparison is against
    // the parent's own Season edge, not merely against "some" Season.
    selectProgramSeason(what, programId, seasonId, via) {
      if (!seasonId) {
        throw new Error(what + ": selectProgramSeason needs a real Season id; "
          + "use selectProgram() for a Program-axis create");
      }
      return window.hsFixture.selectContext(what, programId, seasonId, via);
    },
    // THE RELOAD-BOUNDARY PAIR, for fixture DATA SETUP — same axes, same
    // assertions, no render pass. See \`selectPersisted\` for when each applies.
    selectPersistedProgram(what, programId) {
      return window.hsFixture.selectPersisted(what, programId, null);
    },
    selectPersistedProgramSeason(what, programId, seasonId) {
      if (!seasonId) {
        throw new Error(what + ": selectPersistedProgramSeason needs a real "
          + "Season id; use selectPersistedProgram() for a Program-axis create");
      }
      return window.hsFixture.selectPersisted(what, programId, seasonId);
    },
    // THE MUTATION SHAPE — archive / reopen / delete, i.e. anything routed
    // through \`setup_guarded_mutation\` rather than through a create. It takes
    // the target KIND and applies the MUTATION table, which is a different
    // table from the create one and disagrees with it on \`season\` and
    // \`league\` (see this file's header). Passing the kind, rather than
    // choosing a helper, is what keeps a journey honest about WHICH rule it is
    // satisfying.
    //
    // \`via\` selects the driver, and the default is the strict one:
    //
    //   "app" (default) — the app's own \`setActiveContext\` pipeline, for the
    //       usual case where this page goes on to drive real UI. Same reasoning
    //       as \`selectContext\`: moving the server while the client believes
    //       the old tuple is a state no operator can reach.
    //   "api" — a direct \`POST /api/context\`, for an OUT-OF-BAND actor block:
    //       a page whose logged-in identity was swapped by an API call so the
    //       CLIENT is not the actor, or a second page used purely as an API
    //       actor that asserts no UI at all. There \`setActiveContext\` is not
    //       the faithful choice but the misleading one — it would re-render and
    //       rewrite \`location.hash\` as the wrong persona, disturbing state the
    //       journey deliberately set up. The ORACLE IS NOT WEAKENED by this:
    //       both the write echo and the read-back convergence below run
    //       identically either way. Only the client-side invalidation is
    //       skipped, which is sound exactly when no UI is driven from this
    //       selection before the next full reload — the caller asserts that by
    //       naming "api".
    selectForMutation(what, kind, programId, seasonId, via) {
      const folded = (kind || "").replace(/-/g, "_");
      const seasonOwned = MUTATION_SEASON_OWNED.has(folded);
      if (!seasonOwned && !MUTATION_PROGRAM_AXIS.has(folded)) {
        throw new Error(what + ": unknown mutation kind \\"" + kind + "\\" — it "
          + "is not in either axis class, so this fixture cannot say which "
          + "selection the mutation gate will require. Classify it here the "
          + "way ApiService classifies it, rather than guessing a helper");
      }
      if (!programId) {
        throw new Error(what + ": every guarded mutation needs an explicit "
          + "saved Program (kind \\"" + folded + "\\")");
      }
      if (seasonOwned && !seasonId) {
        throw new Error(what + ": \\"" + folded + "\\" is SEASON-OWNED for "
          + "mutations, so archiving/reopening/deleting it needs an explicit "
          + "saved Program AND Season — a Program-only selection is refused "
          + "even though CREATING one of these needs only the Program");
      }
      return window.hsFixture.selectContext(
        what, programId, seasonId || null, via);
    },
  };
})();
`;

// Install the in-page helpers on `page`. Safe to call more than once, and
// `addInitScript` keeps them present across reloads and same-page navigations
// (several journeys reload to prove a selection survived one).
async function installContextFixture(page) {
  await page.addInitScript(FIXTURE_SOURCE);
  await page.evaluate(FIXTURE_SOURCE);
}

// Node-side conveniences, for the (common) case where the selection happens
// BETWEEN two `page.evaluate` blocks rather than inside one. They are the same
// asserted helpers, so a journey can mix both styles without two contracts.
// `via` is the driver documented on `selectForMutation` below: "app" (the
// default) drives the app's own switch pipeline; "api" is for an out-of-band
// actor page that asserts no UI from this selection. Existing callers pass
// nothing and keep the strict default.
async function selectProgram(page, what, programId, via) {
  return page.evaluate(
    ([w, p, v]) => window.hsFixture.selectProgram(w, p, v),
    [what, programId, via || "app"]);
}

async function selectProgramSeason(page, what, programId, seasonId, via) {
  return page.evaluate(
    ([w, p, s, v]) => window.hsFixture.selectProgramSeason(w, p, s, v),
    [what, programId, seasonId, via || "app"]);
}

// The MUTATION-shape selection (archive / reopen / delete). `kind` is the
// target's kind as ApiService classifies it, and `via` is the driver described
// on the in-page helper — "app" by default, "api" for an out-of-band actor
// page that drives no UI from this selection.
async function selectForMutation(page, what, kind, programId, seasonId, via) {
  return page.evaluate(
    ([w, k, p, s, v]) => window.hsFixture.selectForMutation(w, k, p, s, v),
    [what, kind, programId, seasonId || null, via || "app"]);
}

// Node-side reload-boundary selections (see `selectPersisted`): use these for
// fixture DATA SETUP whose very next act is a full page load.
async function selectPersistedProgram(page, what, programId) {
  return page.evaluate(
    ([w, p]) => window.hsFixture.selectPersistedProgram(w, p), [what, programId]);
}

async function selectPersistedProgramSeason(page, what, programId, seasonId) {
  return page.evaluate(
    ([w, p, s]) => window.hsFixture.selectPersistedProgramSeason(w, p, s),
    [what, programId, seasonId]);
}

module.exports = {
  FIXTURE_SOURCE,
  installContextFixture,
  selectProgram,
  selectProgramSeason,
  selectPersistedProgram,
  selectPersistedProgramSeason,
  selectForMutation,
  MUTATION_SEASON_OWNED,
  MUTATION_PROGRAM_AXIS,
};
