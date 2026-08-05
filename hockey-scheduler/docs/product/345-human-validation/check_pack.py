#!/usr/bin/env python3
"""Executable regression coverage for the #345 human-validation facilitator pack.

Plain `python3`, standard library only, no third-party dependencies.

    python3 check_pack.py                  # run every check; exit 0 when clean
    python3 check_pack.py --list-breaks    # list the injectable defects
    python3 check_pack.py --break NAME     # inject one defect; the check it
                                           # targets MUST then fail (exit 1)

Why the `--break` flags exist
-----------------------------
A check that cannot fail is worse than no check: it reports green while
testing nothing. Every check below therefore has at least one named mutation
that injects the exact defect it is supposed to catch. CI runs the checker
clean AND runs every mutation, requiring each mutation to exit non-zero — so
if a defect drifts out of the text and a mutation stops biting, CI says which
one, instead of the check quietly passing forever.

Mutations are applied IN MEMORY only. This script never writes to any file.

Scope: this file verifies the pack's *instrumentation*. It performs no
session, produces no evidence, and asserts nothing about whether the two
Human-only #345 acceptance criteria have been met. They have not.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

PACK_DIR = Path(__file__).resolve().parent
PRODUCT_DIR = PACK_DIR.parent

# ---------------------------------------------------------------------------
# The source pin (safeguard S1).
#
# A protocol quotation is not like a code citation. If the protocol moves and
# this pack does not, the pack keeps instructing a human to perform a step the
# protocol has since corrected, and nothing sees it. That is exactly how the
# pre-#394 K5/S5 inversion survived: the quoted expectation was the opposite of
# shipped behaviour, and no check compared the copy against its source.
#
# So the pin is not decorative. PINNED_COMMIT is the human-readable provenance
# ("read at origin/main = ..."), and the blob SHA-1s below are the enforcement:
# they are the git object ids of the two protocol files at that commit. If
# either protocol file changes by one byte, check_protocol_pin fails and every
# quotation in this pack is re-verified against the new text before the pin is
# advanced.
# ---------------------------------------------------------------------------
PINNED_COMMIT = "36195faadb5c97936022d8f3706af51181a6b64d"

MODERATED_PROTOCOL = "moderated-operator-validation-protocol.md"
KSR_PROTOCOL = "manual-keyboard-screenreader-validation-protocol.md"

PROTOCOL_BLOBS = {
    MODERATED_PROTOCOL: "c99935885b3de9141cad9b575a43a3d4fd62e0b3",
    KSR_PROTOCOL: "738e6c096e5d95d671b211e3f3df21bf975d17cc",
}

CAPTURE_SHEETS = {
    "09-capture-sheet-league-admin.md": ("League Admin", "LA-01"),
    "10-capture-sheet-arena-manager.md": ("Arena Manager", "AM-01"),
    "11-capture-sheet-coach.md": ("Coach", "C-01"),
}

TASK_PROMPT_SHEETS = (
    "06-task-prompts-league-admin.md",
    "07-task-prompts-arena-manager.md",
    "08-task-prompts-coach.md",
)

ENVIRONMENT_SHEETS = (
    "01-environment-league-admin.md",
    "02-environment-arena-manager.md",
    "03-environment-coach.md",
)

EASE_CANON = "12-ease-rating-question.md"

# Every markdown file in the pack, in reading order.
PACK_FILES = (
    ENVIRONMENT_SHEETS
    + ("04-keyboard-script.md", "05-screen-reader-script.md")
    + TASK_PROMPT_SHEETS
    + tuple(CAPTURE_SHEETS)
    + (EASE_CANON, "README.md")
)

# ---------------------------------------------------------------------------
# Break plumbing
# ---------------------------------------------------------------------------

_ACTIVE_BREAK: str | None = None

BREAKS: dict[str, str] = {}


def register_break(name: str, description: str) -> str:
    BREAKS[name] = description
    return name


def broken(name: str) -> bool:
    if name not in BREAKS:  # pragma: no cover - programmer error
        raise KeyError(f"unregistered break: {name}")
    return _ACTIVE_BREAK == name


# ---------------------------------------------------------------------------
# File access (break mutations are applied here, in memory)
# ---------------------------------------------------------------------------

B_PII_IDENTITY_FIELD = register_break(
    "pii-identity-field",
    "put the `Participant identity` row back on the League Admin capture sheet",
)
B_PII_NAME_FIELD = register_break(
    "pii-name-field",
    "add a participant full-name field to the Arena Manager capture sheet",
)
B_PII_CONTACT_FIELD = register_break(
    "pii-contact-field",
    "add a contact-details field to the Coach capture sheet",
)
B_PII_EMPLOYER_FIELD = register_break(
    "pii-employer-field",
    "add an employer field to the League Admin capture sheet",
)
B_PII_CODE_MISSING = register_break(
    "pii-code-missing",
    "drop the anonymous participant-code row from the Arena Manager sheet",
)
B_PII_RULE_MISSING = register_break(
    "pii-rule-missing",
    "delete the no-names / mapping-outside-the-repo / redaction rules from the Coach sheet",
)
B_RECORDINGS_PERMITTED = register_break(
    "recordings-permitted",
    "let the pack permit audio/video recording with no retention rule in force",
)
B_SUPERSESSION_NOTE_MISSING = register_break(
    "supersession-note-missing",
    "remove the note that the earlier 'explicit blank for participant identity' requirement is superseded",
)


def flex(marker: str) -> re.Pattern:
    """Match a required sentence across markdown line wrapping.

    The pack wraps at ~80 columns, so a required sentence is rarely on one
    line. Both the presence checks and the break mutations go through this,
    so a mutation can never silently become a no-op because the text rewrapped.
    """
    return re.compile(r"\s+".join(re.escape(word) for word in marker.split()))


def _inject_row(text: str, anchor: str, row: str) -> str:
    """Insert a table row immediately after `anchor` (used by break flags)."""
    if anchor not in text:  # pragma: no cover - anchor drifted
        raise AssertionError(
            f"break anchor not found, the mutation would be a no-op: {anchor!r}"
        )
    return text.replace(anchor, anchor + "\n" + row, 1)


_IDENTITY_ROW = "| Participant identity (see the privacy rule below) | |"


def read_pack(name: str) -> str:
    text = (PACK_DIR / name).read_text(encoding="utf-8")

    if name == "09-capture-sheet-league-admin.md":
        if broken(B_PII_IDENTITY_FIELD):
            text = _inject_row(text, "| Date | |", _IDENTITY_ROW)
        if broken(B_PII_EMPLOYER_FIELD):
            text = _inject_row(text, "| Date | |", "| Employer | |")
    if name == "10-capture-sheet-arena-manager.md":
        if broken(B_PII_NAME_FIELD):
            text = _inject_row(text, "| Date | |", "| Participant's full name | |")
        if broken(B_PII_CODE_MISSING):
            text = re.sub(r"(?m)^\|\s*Participant code[^\n]*\n", "", text)
    if name == "11-capture-sheet-coach.md":
        if broken(B_PII_CONTACT_FIELD):
            text = _inject_row(text, "| Date | |", "| Contact details | |")
        if broken(B_PII_RULE_MISSING):
            for marker in (
                RULE_NO_IDENTIFIERS,
                RULE_MAPPING_OUTSIDE_REPO,
                RULE_REDACTION_CHECK,
            ):
                text = flex(marker).sub("", text)

    if name == "06-task-prompts-league-admin.md" and broken(B_PRIMARY_OLD_RULE):
        text = text.replace(
            "- Record their exact words",
            "- Let them proceed and score the match against the control they "
            "actually named.\n- Record their exact words",
            1,
        )
    if name == "09-capture-sheet-league-admin.md" and broken(B_PRIMARY_RULE_MISSING):
        text = flex(RULE_NEVER_YES).sub("", text)

    if name == "07-task-prompts-arena-manager.md" and broken(B_EASE_LOCAL_COPY):
        text += (
            "\n\n```text\nOn a scale of one to five, how easy did you find that?\n```\n"
        )
    if name == "08-task-prompts-coach.md" and broken(B_EASE_POINTER_MISSING):
        text = text.replace(f"({EASE_CANON})", "(README.md)")
    if name == "02-environment-arena-manager.md" and broken(B_GATE_ROW_MISSING):
        text = re.sub(r"(?m)^\|[^\n]*" + re.escape(GATE_MARKER) + r"[^\n]*\n", "", text)
    if name == "README.md" and broken(B_README_NONBLOCKING):
        text += "\n\n**None of these blocks a session.**\n"

    if broken(B_RECORDINGS_PERMITTED):
        text = flex(RULE_NO_RECORDINGS).sub(
            "Recording is at the moderator's discretion.", text
        )
    if broken(B_SUPERSESSION_NOTE_MISSING):
        text = flex(RULE_SUPERSEDED_IDENTITY).sub("", text)
    if broken(B_PIN_MISSING) and name == "09-capture-sheet-league-admin.md":
        text = text.replace(PINNED_COMMIT, "origin/main")
        for blob in PROTOCOL_BLOBS.values():
            text = text.replace(blob, "")

    return text


def read_protocol(filename: str) -> bytes:
    data = (PRODUCT_DIR / filename).read_bytes()
    if broken(B_PROTOCOL_DRIFT) and filename == KSR_PROTOCOL:
        # Stand in for "the protocol was corrected upstream and the pack was
        # not re-verified against it" — the pre-#394 K5/S5 failure mode.
        data = data.replace(b"re-filters", b"re-renders", 1)
    return data


# ---------------------------------------------------------------------------
# Required rule text (blocker 1 + safeguard S2)
#
# These are the exact sentences the capture sheets must carry. They are
# checked by presence, so deleting or softening one is a check failure rather
# than a silent policy change.
# ---------------------------------------------------------------------------

RULE_NO_IDENTIFIERS = (
    "Never write a name, contact detail, employer, job title, or account "
    "identifier on this sheet."
)
RULE_MAPPING_OUTSIDE_REPO = (
    "The consent-to-code mapping lives outside this repository, with access "
    "restricted to the moderator."
)
RULE_REDACTION_CHECK = (
    "Redaction check before attachment: re-read every quote and strike "
    "anything that identifies the participant or anyone else."
)
RULE_NO_RECORDINGS = (
    "These sessions run without audio or video recording."
)
RULE_SUPERSEDED_IDENTITY = (
    "Supersedes the earlier requirement for an explicit blank for participant "
    "identity"
)

# ---------------------------------------------------------------------------
# Blocker 1 — the issue-ready export
#
# The regression below is not prose. It builds an issue-ready export from a
# synthetic source record that deliberately contains a fake full name, email,
# phone number, employer, account identifier, and an unrelated personal
# disclosure inside a quote, and proves that none of it survives into the
# artifact the README tells a facilitator to attach to the PUBLIC issue.
#
# Every value below is invented for this test. No real person is described.
# ---------------------------------------------------------------------------

B_EXPORT_LEAK_NAME = register_break(
    "export-leak-name",
    "let the issue-ready export carry the participant's name through",
)
B_EXPORT_REDACTION_OFF = register_break(
    "export-redaction-off",
    "stop redacting unrelated personal disclosures out of quotes",
)
B_EXPORT_KEY_ALLOWLIST_OFF = register_break(
    "export-key-allowlist-off",
    "export the whole source record instead of the allowed fields",
)

REDACTION_MARKER = (
    "[unrelated personal disclosure — not transcribed, per moderated protocol §1.6]"
)

# A synthetic facilitator scratch record. NOT evidence, NOT a real person.
SYNTHETIC_SOURCE_RECORD = {
    "participant_code": "LA-01",
    "role_under_test": "League Admin",
    "experience_with_app": "New",
    "role_familiarity": "~6 years running a house league; no exposure to this app",
    "consent_status": "obtained, verbal, covering anonymized notes and direct quotes",
    "consent_time": "2026-08-05T09:58-04:00",
    "recording_status": "none — no audio or video captured",
    # --- everything below this line must never reach the issue ---------------
    "full_name": "Marguerite Okonkwo-Delacroix",
    "email": "m.okonkwo@northgate-ice.example",
    "phone": "+1-555-0142",
    "employer": "Northgate Ice Partners",
    "account_identifier": "okonkwo_m@northgate-ice.example",
    "quotes": [
        {
            "task": "Task 1",
            "raw": (
                "Look, I have to be out by four — my son's custody hearing is "
                "Thursday and my ex still works at Northgate Ice Partners, so "
                "it is all a bit much this week. Anyway. Which of these is the "
                "season thing?"
            ),
            "unrelated": [
                "my son's custody hearing is Thursday and my ex still works at "
                "Northgate Ice Partners, so it is all a bit much this week. "
            ],
            "context": "before any click, on the Home/Tasks hub",
        },
        {
            "task": "Task 3",
            "raw": "I genuinely cannot tell which of these two is the main one.",
            "unrelated": [],
            "context": "after opening the workflow, before clicking anything",
        },
    ],
}

# The only fields permitted in the artifact attached to the public issue:
# the anonymous code, the role/experience needed to interpret the result,
# consent status and time, and redacted task evidence.
ALLOWED_EXPORT_KEYS = (
    "participant_code",
    "role_under_test",
    "experience_with_app",
    "role_familiarity",
    "consent_status",
    "consent_time",
    "recording_status",
    "task_evidence",
)

# Values that must not appear anywhere in the export, in any form.
FORBIDDEN_EXPORT_VALUES = (
    "Marguerite",
    "Okonkwo",
    "Delacroix",
    "m.okonkwo@northgate-ice.example",
    "+1-555-0142",
    "Northgate Ice Partners",
    "okonkwo_m@northgate-ice.example",
    "custody hearing",
    "my ex",
)


def redact_quote(quote: dict) -> str:
    text = quote["raw"]
    if not broken(B_EXPORT_REDACTION_OFF):
        for span in quote["unrelated"]:
            if span not in text:  # pragma: no cover - fixture drifted
                raise AssertionError(f"redaction span not present in quote: {span!r}")
            text = text.replace(span, REDACTION_MARKER + " ")
    return " ".join(text.split())


def build_issue_export(record: dict) -> dict:
    """Build the artifact that gets attached to the public #345 issue."""
    if broken(B_EXPORT_KEY_ALLOWLIST_OFF):
        return dict(record)

    export = {
        "participant_code": record["participant_code"],
        "role_under_test": record["role_under_test"],
        "experience_with_app": record["experience_with_app"],
        "role_familiarity": record["role_familiarity"],
        "consent_status": record["consent_status"],
        "consent_time": record["consent_time"],
        "recording_status": record["recording_status"],
        "task_evidence": [
            {
                "task": q["task"],
                "quote": redact_quote(q),
                "context": q["context"],
            }
            for q in record["quotes"]
        ],
    }
    if broken(B_EXPORT_LEAK_NAME):
        export["participant_name"] = record["full_name"]
    return export


def render_export(export: dict) -> str:
    lines = []
    for key, value in export.items():
        if key == "task_evidence":
            for item in value:
                lines.append(f'{item["task"]}: "{item["quote"]}" — {item["context"]}')
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


def table_cells(text: str) -> list[tuple[int, str]]:
    """Every cell of every markdown table row that is NOT inside a blockquote.

    Blockquoted rows are verbatim protocol text — §1.6's own "do not capture
    full name, contact details, employer" wording lives in one, and must stay
    exactly as the protocol wrote it.
    """
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(">") or not stripped.startswith("|"):
            continue
        for cell in stripped.strip("|").split("|"):
            cell = cell.strip()
            if cell:
                out.append((lineno, cell))
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

CHECKS: list = []


def check(name: str):
    def wrap(fn):
        fn.check_name = name
        CHECKS.append(fn)
        return fn

    return wrap


FORBIDDEN_FIELD_PATTERNS = (
    ("participant identity", r"participant\s+identit(?:y|ies)"),
    ("participant name", r"participant(?:'s|s')?\s+(?:full\s+|real\s+|legal\s+)?name"),
    ("full name", r"\bfull\s+name\b"),
    ("real / legal / given name", r"\b(?:real|legal|given|first|last)\s+name\b|\bsurname\b"),
    ("contact details", r"\bcontact\s+(?:details?|info(?:rmation)?)\b"),
    ("email address", r"\be-?mail\b"),
    ("phone number", r"\bphone\b|\btelephone\b|\bmobile\s+number\b"),
    ("employer", r"\bemployer\b|\bcompany\s+name\b|\bworkplace\b"),
    ("account identifier", r"\baccount\s+(?:identifier|id|username|login)\b"),
)


@check("pii-fields")
def check_pii_fields() -> list[str]:
    """No identifying field may exist on any of the three attachable sheets.

    The capture sheets are what the README tells a facilitator to transcribe
    and attach to the PUBLIC issue. A field is an invitation: a table row
    labelled `Participant identity` will get a real name written into it.
    """
    failures = []
    for filename, (role, code) in CAPTURE_SHEETS.items():
        text = read_pack(filename)

        for lineno, cell in table_cells(text):
            for label, pattern in FORBIDDEN_FIELD_PATTERNS:
                if re.search(pattern, cell, re.IGNORECASE):
                    failures.append(
                        f"{filename}:{lineno}: forbidden {label} field on an "
                        f"attachable capture sheet: {cell!r}"
                    )

        code_rows = [
            cell
            for _, cell in table_cells(text)
            if re.search(r"participant code", cell, re.IGNORECASE)
        ]
        if not code_rows:
            failures.append(
                f"{filename}: no `Participant code` field — the anonymous code "
                f"is what replaces the identity field, so its absence means the "
                f"sheet records nothing to join a session to its consent record"
            )
        if code not in text:
            failures.append(
                f"{filename}: the {role} participant code format {code!r} is not "
                f"shown anywhere, so the facilitator has no example to follow"
            )
    return failures


@check("pii-rules")
def check_pii_rules() -> list[str]:
    """Each attachable sheet must carry the four operative privacy rules."""
    failures = []
    required = (
        ("no-identifiers prohibition", RULE_NO_IDENTIFIERS),
        ("consent-to-code mapping kept outside the repository", RULE_MAPPING_OUTSIDE_REPO),
        ("redaction check before attachment", RULE_REDACTION_CHECK),
        ("no audio/video recording", RULE_NO_RECORDINGS),
    )
    for filename in CAPTURE_SHEETS:
        text = read_pack(filename)
        for label, marker in required:
            if not flex(marker).search(text):
                failures.append(
                    f"{filename}: missing the {label} rule (expected the exact "
                    f"sentence {marker!r})"
                )
    return failures


@check("pii-supersession-note")
def check_pii_supersession_note() -> list[str]:
    """Safeguard S2.

    The owner's earlier brief asked for an explicit blank for participant
    identity. The participant-code rule supersedes it. Say so, so a reader who
    saw the first requirement can tell it changed deliberately rather than
    being quietly dropped.
    """
    failures = []
    carriers = [f for f in CAPTURE_SHEETS] + ["README.md"]
    for filename in carriers:
        if not flex(RULE_SUPERSEDED_IDENTITY).search(read_pack(filename)):
            failures.append(
                f"{filename}: no note that the earlier 'explicit blank for "
                f"participant identity' requirement is superseded by the "
                f"participant-code rule"
            )
    return failures


@check("pii-export")
def check_pii_export() -> list[str]:
    """Blocker 1's executable regression.

    A synthetic source record carrying a fake full name, email, phone,
    employer, account identifier and an unrelated personal disclosure is run
    through the issue-ready export, and the export is proven to contain ONLY
    the anonymous code, the role/experience needed to interpret the result,
    consent status and time, and redacted task evidence.
    """
    failures = []
    export = build_issue_export(SYNTHETIC_SOURCE_RECORD)
    rendered = render_export(export)

    extra = set(export) - set(ALLOWED_EXPORT_KEYS)
    if extra:
        failures.append(
            "issue-ready export carries fields outside the allowlist: "
            + ", ".join(sorted(extra))
        )
    missing = set(ALLOWED_EXPORT_KEYS) - set(export)
    if missing:
        failures.append(
            "issue-ready export is missing required fields: " + ", ".join(sorted(missing))
        )

    for value in FORBIDDEN_EXPORT_VALUES:
        if value.lower() in rendered.lower():
            failures.append(
                f"issue-ready export leaks {value!r} — this artifact is attached "
                f"to the public #345 issue"
            )

    # The export must still say enough to interpret the result.
    for required in (
        SYNTHETIC_SOURCE_RECORD["participant_code"],
        SYNTHETIC_SOURCE_RECORD["role_under_test"],
        SYNTHETIC_SOURCE_RECORD["experience_with_app"],
        SYNTHETIC_SOURCE_RECORD["consent_time"],
    ):
        if required not in rendered:
            failures.append(f"issue-ready export dropped required context {required!r}")

    if REDACTION_MARKER not in rendered:
        failures.append(
            "issue-ready export contains no redaction marker — the unrelated "
            "personal disclosure in the Task 1 quote was not struck"
        )

    # The on-task half of the redacted quote must survive: redaction that
    # deletes the finding is not redaction, it is data loss.
    if "Which of these is the season thing?" not in rendered:
        failures.append(
            "redaction removed the on-task part of the Task 1 quote; only the "
            "unrelated disclosure should be struck"
        )
    return failures


# ---------------------------------------------------------------------------
# Blocker 2 — League Admin task 3 must not record a passing match for the
# wrong control.
#
# Moderated protocol §2's "Record" subsection defines exactly one yes/no here:
#
#   "Whether the participant's stated expectation of the primary action
#    matched its actual effect (yes/no) ..."
#
# The measurement is about THE PRIMARY ACTION. If a participant misidentifies
# which control is primary and then correctly predicts what that OTHER control
# does, they have demonstrated the failure #345's "one primary action per
# screen" requirement exists to prevent — and scoring that as a match writes a
# `yes` into the field that transcribes as the canonical primary-action
# measurement. The product failure under test becomes valid-looking positive
# evidence.
#
# So: the mistaken control and its observed effect are recorded as DIAGNOSTIC
# detail, and the canonical result is never `Yes` unless the expectation was
# about the actual primary action.
# ---------------------------------------------------------------------------

B_RUBRIC_SCORE_NAMED = register_break(
    "rubric-score-named-control",
    "score the §2 match against whichever control the participant named (the original defect)",
)
B_RUBRIC_TRANSCRIPTION_YES = register_break(
    "rubric-transcription-yes",
    "let the §6 transcription turn a diagnostic match into a primary-action Yes",
)

NOT_EVALUATED = "Not evaluated"
CANONICAL_MEASURE = (
    "Whether the participant's stated expectation of the primary action "
    "matched its actual effect"
)


def score_primary_action(obs: dict) -> dict:
    """The §2 step-3 result, plus the diagnostic detail that explains it."""
    matched_named = obs["prediction_matched_named_control"]

    if obs["named_control_is_primary"]:
        canonical = "Yes" if matched_named else "No"
    elif broken(B_RUBRIC_SCORE_NAMED):
        canonical = "Yes" if matched_named else "No"
    else:
        # They never stated an expectation about the primary action, so the
        # canonical measurement has no value to report. It is emphatically not
        # a match.
        canonical = NOT_EVALUATED

    return {
        "canonical_result": canonical,
        "diagnostic": {
            "control_they_named": obs["named_control"],
            "was_it_the_primary_action": "yes" if obs["named_control_is_primary"] else "no",
            "screen_primary_action": obs["screen_primary_action"],
            "their_words": obs["stated_expectation"],
            "what_that_control_actually_did": obs["actual_effect_of_named_control"],
            "did_their_prediction_match_that_control": "yes" if matched_named else "no",
        },
        "is_a_finding_about_the_screen": not obs["named_control_is_primary"],
    }


def transcribe_to_section6(scored: dict) -> str:
    """Render the lines that go into the protocol's own §6 evidence template."""
    result = scored["canonical_result"]
    if broken(B_RUBRIC_TRANSCRIPTION_YES):
        if scored["diagnostic"]["did_their_prediction_match_that_control"] == "yes":
            result = "Yes"

    lines = [f"{CANONICAL_MEASURE}: {result}"]
    diag = scored["diagnostic"]
    lines.append(
        f"Diagnostic — control the participant described: "
        f"{diag['control_they_named']} (screen's primary action: "
        f"{diag['screen_primary_action']}; was it the primary action: "
        f"{diag['was_it_the_primary_action']})"
    )
    lines.append(f"Diagnostic — their words: \"{diag['their_words']}\"")
    lines.append(
        f"Diagnostic — what that control actually did: "
        f"{diag['what_that_control_actually_did']}"
    )
    if scored["is_a_finding_about_the_screen"]:
        lines.append(
            "Follow-up finding — the participant could not identify the screen's "
            "primary action; #345 requires one primary action per screen."
        )
    return "\n".join(lines)


# Three fixtures. (a) is the blocker: it must never produce a passing match.
RUBRIC_FIXTURES = (
    (
        "a) secondary control, correct prediction about it",
        {
            "named_control": "Import data",
            "named_control_is_primary": False,
            "screen_primary_action": "Add Season",
            "stated_expectation": "That one loads a spreadsheet of teams, I think.",
            "actual_effect_of_named_control": "opened the Imports and onboarding workflow",
            "prediction_matched_named_control": True,
        },
        NOT_EVALUATED,
        False,
    ),
    (
        "b) primary control, wrong prediction",
        {
            "named_control": "Add Season",
            "named_control_is_primary": True,
            "screen_primary_action": "Add Season",
            "stated_expectation": "It'll take me to a list of the seasons already set up.",
            "actual_effect_of_named_control": "opened the New season drawer",
            "prediction_matched_named_control": False,
        },
        "No",
        False,
    ),
    (
        "c) primary control, correct prediction",
        {
            "named_control": "Add Season",
            "named_control_is_primary": True,
            "screen_primary_action": "Add Season",
            "stated_expectation": "It should open a form to create a new season.",
            "actual_effect_of_named_control": "opened the New season drawer",
            "prediction_matched_named_control": True,
        },
        "Yes",
        True,
    ),
)


@check("primary-action-rubric")
def check_primary_action_rubric() -> list[str]:
    """Blocker 2's executable regression, including the §6 transcription."""
    failures = []
    for label, obs, expected, should_pass in RUBRIC_FIXTURES:
        scored = score_primary_action(obs)
        actual = scored["canonical_result"]
        if actual != expected:
            failures.append(
                f"fixture {label}: canonical §2 result is {actual!r}, expected "
                f"{expected!r}"
            )
        if actual == "Yes" and not should_pass:
            failures.append(
                f"fixture {label}: recorded a PASSING primary-action match for a "
                f"control that was not the primary action — this is the defect "
                f"under test being written up as positive evidence"
            )

        # The transcription into §6 must preserve the outcome, not launder it.
        section6 = transcribe_to_section6(scored)
        transcribed = None
        for line in section6.splitlines():
            if line.startswith(CANONICAL_MEASURE):
                transcribed = line.split(": ", 1)[1]
        if transcribed != expected:
            failures.append(
                f"fixture {label}: §6 transcription says {transcribed!r} but the "
                f"scored result was {expected!r} — the transcription changed the "
                f"answer"
            )
        if not obs["named_control_is_primary"]:
            if obs["named_control"] not in section6:
                failures.append(
                    f"fixture {label}: §6 transcription dropped the diagnostic "
                    f"detail naming the control the participant described"
                )
            if obs["actual_effect_of_named_control"] not in section6:
                failures.append(
                    f"fixture {label}: §6 transcription dropped the observed effect "
                    f"of the control the participant described"
                )
    return failures


B_PRIMARY_OLD_RULE = register_break(
    "primary-action-old-rule",
    "put the 'score the match against the control they actually named' instruction back",
)
B_PRIMARY_RULE_MISSING = register_break(
    "primary-action-rule-missing",
    "delete the never-Yes rule from the League Admin capture sheet",
)

RULE_PRIMARY_ONLY = (
    "Only an expectation about the actual primary action may be compared for "
    "the §2 yes/no result."
)
RULE_NEVER_YES = (
    "If the control they described was not the screen's primary action, the "
    "canonical §2 result is Fail/No or Not evaluated — never Yes."
)

FORBIDDEN_SCORING_PHRASES = (
    "score the match against the control they actually named",
    "judged against the control they named",
    "score the match against the control they named",
)

PRIMARY_ACTION_FILES = (
    "06-task-prompts-league-admin.md",
    "09-capture-sheet-league-admin.md",
)


@check("primary-action-text")
def check_primary_action_text() -> list[str]:
    """The League Admin prompt sheet and capture sheet must say the same thing
    the rubric does — the sheet is what a facilitator actually reads."""
    failures = []
    for filename in PRIMARY_ACTION_FILES:
        text = read_pack(filename)
        for phrase in FORBIDDEN_SCORING_PHRASES:
            if flex(phrase).search(text):
                failures.append(
                    f"{filename}: still instructs the facilitator to {phrase!r} — "
                    f"a correct prediction about a secondary control would be "
                    f"transcribed as the canonical primary-action match"
                )
        for marker in (RULE_PRIMARY_ONLY, RULE_NEVER_YES):
            if not flex(marker).search(text):
                failures.append(
                    f"{filename}: missing the rule {marker!r}"
                )
    return failures


# ---------------------------------------------------------------------------
# Blocker 3 — the ease-rating wording is a hard pre-flight gate.
#
# The README used to say none of the four owner questions blocks a session,
# while item 4 said the ease-rating wording must be ratified or replaced before
# the first session. A facilitator could follow the first and start, and the
# second then said that session should not have started.
#
# The ease question is asked NINE times across the three sessions, and its
# consistency is the thing that makes the three sets comparable. Ruling on it
# after session one invalidates the set. The owner has ratified no wording, and
# defaulting one on their behalf would be the same class of defect as blocker 2
# — the pack quietly deciding something the protocol left to a person. So the
# ruling is a gate, not a default, and it has exactly one home.
# ---------------------------------------------------------------------------

B_EASE_LOCAL_COPY = register_break(
    "ease-local-copy",
    "give the Arena Manager prompt sheet its own copy of the ease wording again",
)
B_EASE_POINTER_MISSING = register_break(
    "ease-pointer-missing",
    "drop the Coach prompt sheet's reference to the canonical ease wording",
)
B_EASE_READINESS_BLIND = register_break(
    "ease-readiness-blind",
    "make the readiness check ignore an unruled (blank) ease wording",
)
B_EASE_DIVERGENCE_BLIND = register_break(
    "ease-divergence-blind",
    "make the readiness check ignore role sheets resolving to different wording",
)
B_GATE_ROW_MISSING = register_break(
    "gate-row-missing",
    "remove the hard pre-flight gate row from the Arena Manager environment sheet",
)
B_README_NONBLOCKING = register_break(
    "readme-nonblocking-claim",
    "put the README's 'none of these blocks a session' claim back",
)

UNRULED_SENTINEL = "NOT YET RULED"
RULING_START = "<!-- ease-ruling:start -->"
RULING_END = "<!-- ease-ruling:end -->"
EASE_HEADING = "## Immediately after each task — the ease rating"
EASE_HEADING_SHEET = "## Ease rating — protocol §6's scale, verbatim"

GATE_MARKER = "HARD GATE — the ease-rating wording is ruled and recorded."
GATE_BLOCKS = "No session starts until this line is PASS."

B_EASE_NOSOURCE_BLIND = register_break(
    "ease-nosource-blind",
    "make the readiness check ignore a role sheet that names no ease wording at all",
)

FENCE_RE = re.compile(r"```text\n(.*?)\n```", re.DOTALL)


class _NoSource:
    """A role sheet that neither names the canonical file nor carries text.

    Distinct from None, which means "points at the canonical ruling, and the
    ruling is blank". Collapsing the two would make each guard's fixture trip
    the other guard, so blinding one guard would change nothing and its break
    would silently stop biting.
    """

    def __repr__(self) -> str:  # pragma: no cover - display only
        return "<names no ease wording>"


NO_SOURCE = _NoSource()


def extract_ruling(text: str) -> str | None:
    """The owner-ratified wording, or None while it is unruled."""
    if RULING_START not in text or RULING_END not in text:
        return None
    block = text.split(RULING_START, 1)[1].split(RULING_END, 1)[0]
    if UNRULED_SENTINEL in block:
        return None
    quoted = [b.strip() for b in FENCE_RE.findall(block)]
    quoted = [q for q in quoted if q]
    if not quoted:
        return None
    return "\n---\n".join(quoted)


def ease_section(text: str, heading: str = EASE_HEADING) -> str:
    if heading not in text:
        return ""
    return text.split(heading, 1)[1].split("\n---\n", 1)[0]


def resolve_ease_wording() -> tuple[str | None, dict[str, str | None]]:
    """What each role sheet actually resolves the ease wording to.

    A sheet that prints its own copy resolves to that copy — which is how three
    sessions end up asking three different questions. A sheet that names the
    canonical file resolves to the canonical ruling. A sheet that does neither
    resolves to nothing.
    """
    try:
        canonical = extract_ruling(read_pack(EASE_CANON))
    except FileNotFoundError:
        canonical = None

    per_role: dict[str, str | None] = {}
    for sheet in TASK_PROMPT_SHEETS:
        section = ease_section(read_pack(sheet))
        local = [b.strip() for b in FENCE_RE.findall(section) if b.strip()]
        if local:
            per_role[sheet] = "\n---\n".join(local)
        elif re.search(r"\(" + re.escape(EASE_CANON) + r"\)", section):
            per_role[sheet] = canonical
        else:
            per_role[sheet] = NO_SOURCE
    return canonical, per_role


def ease_readiness(
    canonical: str | None, per_role: dict[str, str | None]
) -> tuple[bool, list[str]]:
    """The session-readiness check for the ease wording.

    Fails when the ruling is blank, or when any role sheet resolves to
    different wording. Passes only when all three use the same recorded text.
    """
    reasons: list[str] = []

    if canonical is None and not broken(B_EASE_READINESS_BLIND):
        reasons.append(
            "the ease-rating wording has not been ruled — "
            f"{EASE_CANON} carries no owner-ratified text"
        )

    if not broken(B_EASE_NOSOURCE_BLIND):
        for sheet, resolved in sorted(per_role.items()):
            if resolved is NO_SOURCE:
                reasons.append(
                    f"{sheet} names no ease wording at all — it neither "
                    f"references {EASE_CANON} nor carries ruled text"
                )

    resolved_values = {
        v for v in per_role.values() if isinstance(v, str)
    }
    if len(resolved_values) > 1 and not broken(B_EASE_DIVERGENCE_BLIND):
        reasons.append(
            "the role sheets resolve to different ease wording, so the three "
            "sessions would not be comparable: "
            + "; ".join(
                f"{s} -> {v!r}"
                for s, v in sorted(per_role.items())
                if isinstance(v, str)
            )
        )

    return (not reasons), reasons


# Each fixture isolates ONE guard, so blinding that guard changes this
# fixture's verdict and nothing else. A fixture that trips two guards would
# stay red when one is blinded, and that guard's break would report green
# while testing nothing.
EASE_READINESS_FIXTURES = (
    (
        "ruling blank, all three sheets point at the canonical file",
        None,
        {s: None for s in TASK_PROMPT_SHEETS},
        False,
    ),
    (
        "one role sheet resolves to different wording",
        "How easy was that, one to five?",
        {
            TASK_PROMPT_SHEETS[0]: "How easy was that, one to five?",
            TASK_PROMPT_SHEETS[1]: "How easy was that, one to five?",
            TASK_PROMPT_SHEETS[2]: "That seemed easy enough, right?",
        },
        False,
    ),
    (
        "a role sheet names no ease wording at all",
        "How easy was that, one to five?",
        {
            TASK_PROMPT_SHEETS[0]: "How easy was that, one to five?",
            TASK_PROMPT_SHEETS[1]: NO_SOURCE,
            TASK_PROMPT_SHEETS[2]: "How easy was that, one to five?",
        },
        False,
    ),
    (
        "ruled, and all three resolve to the same recorded text",
        "How easy was that, one to five?",
        {s: "How easy was that, one to five?" for s in TASK_PROMPT_SHEETS},
        True,
    ),
)


@check("ease-readiness")
def check_ease_readiness() -> list[str]:
    """Blocker 3's executable regression: the readiness check itself."""
    failures = []
    for label, canonical, per_role, expected_ready in EASE_READINESS_FIXTURES:
        ready, reasons = ease_readiness(canonical, per_role)
        if ready != expected_ready:
            failures.append(
                f"readiness fixture {label!r}: got ready={ready}, expected "
                f"ready={expected_ready} (reasons: {reasons or 'none'})"
            )
        if not expected_ready and not reasons:
            failures.append(
                f"readiness fixture {label!r}: refused the session without saying "
                f"why — a gate with no reason cannot be cleared"
            )
    return failures


@check("ease-single-source")
def check_ease_single_source() -> list[str]:
    """The ruled wording has exactly one home, and all three sheets use it."""
    failures = []

    if not (PACK_DIR / EASE_CANON).exists():
        return [
            f"{EASE_CANON} does not exist — there is no single canonical location "
            f"for the ease-rating wording, so the three role sheets can drift"
        ]

    canon_text = read_pack(EASE_CANON)
    if RULING_START not in canon_text or RULING_END not in canon_text:
        failures.append(
            f"{EASE_CANON}: no machine-readable ruling block "
            f"({RULING_START} ... {RULING_END}), so nothing can tell whether the "
            f"owner has ruled"
        )

    for sheet in TASK_PROMPT_SHEETS:
        section = ease_section(read_pack(sheet))
        if not section:
            failures.append(f"{sheet}: no ease-rating section found")
            continue
        local = [b.strip() for b in FENCE_RE.findall(section) if b.strip()]
        if local:
            failures.append(
                f"{sheet}: prints its own copy of the ease wording. Three copies "
                f"drift into three different questions, and the ease question is "
                f"asked nine times — its consistency is what makes the three "
                f"sessions comparable. Reference {EASE_CANON} instead."
            )
        if not re.search(r"\(" + re.escape(EASE_CANON) + r"\)", section):
            failures.append(
                f"{sheet}: its ease-rating section does not reference "
                f"{EASE_CANON}, so it resolves to no ruled wording"
            )

    # The capture sheets tell the moderator where the wording lives too. If they
    # keep pointing at a role prompt sheet, a facilitator reading only the
    # capture sheet is sent to a file that no longer carries any wording.
    for sheet in CAPTURE_SHEETS:
        section = ease_section(read_pack(sheet), EASE_HEADING_SHEET)
        if not section:
            failures.append(f"{sheet}: no ease-rating section found")
            continue
        if not re.search(r"\(" + re.escape(EASE_CANON) + r"\)", section):
            failures.append(
                f"{sheet}: its ease-rating section does not point at "
                f"{EASE_CANON} — it sends the moderator somewhere that no "
                f"longer carries the wording"
            )
        for prompt_sheet in TASK_PROMPT_SHEETS:
            if re.search(r"\(" + re.escape(prompt_sheet) + r"\)", section):
                failures.append(
                    f"{sheet}: its ease-rating section still names "
                    f"{prompt_sheet} as the source of the wording"
                )
    return failures


@check("ease-preflight-gate")
def check_ease_preflight_gate() -> list[str]:
    """Every environment sheet must block the session on the unruled wording,
    and the README must not say otherwise."""
    failures = []
    for sheet in ENVIRONMENT_SHEETS:
        text = read_pack(sheet)
        if GATE_MARKER not in text:
            failures.append(
                f"{sheet}: pre-flight checklist has no hard gate on the "
                f"ease-rating ruling"
            )
        if GATE_BLOCKS not in text:
            failures.append(
                f"{sheet}: the ease-rating gate does not say it blocks the "
                f"session, so a facilitator can note it and carry on"
            )
        if not re.search(r"\(" + re.escape(EASE_CANON) + r"\)", text):
            failures.append(
                f"{sheet}: the ease-rating gate does not point at {EASE_CANON}"
            )

    readme = read_pack("README.md")
    contradictions = (
        "None of these blocks a session.",
        "none of these blocks a session",
    )
    for phrase in contradictions:
        if flex(phrase).search(readme):
            failures.append(
                f"README.md: still claims {phrase!r} while the ease-rating ruling "
                f"is a hard pre-flight gate — a facilitator can follow one "
                f"sentence and start a session the other says should not have "
                f"started"
            )
    return failures


# ---------------------------------------------------------------------------
# Safeguard S1 — the source pin
# ---------------------------------------------------------------------------

B_PROTOCOL_DRIFT = register_break(
    "protocol-drift",
    "change one word of the keyboard/screen-reader protocol, as an upstream correction would",
)
B_PIN_MISSING = register_break(
    "pin-missing",
    "strip the source pin from the League Admin capture sheet",
)


def blob_sha1(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


@check("protocol-pin")
def check_protocol_pin() -> list[str]:
    """Safeguard S1: the pack is pinned to the exact protocol bytes it quotes.

    If either protocol file changes, this fails and every quotation in the
    pack has to be re-verified before the pin advances. Without it, a
    corrected protocol and an uncorrected pack look identical from the outside
    — which is precisely the pre-#394 K5/S5 failure.
    """
    failures = []
    for filename, expected in PROTOCOL_BLOBS.items():
        actual = blob_sha1(read_protocol(filename))
        if actual != expected:
            failures.append(
                f"{filename}: content has changed since the pinned commit "
                f"{PINNED_COMMIT[:8]} (blob {actual} != pinned {expected}). "
                f"Re-verify every quotation in this pack against the new text, "
                f"then advance PROTOCOL_BLOBS and the pin lines in the pack."
            )
    return failures


@check("pin-present")
def check_pin_present() -> list[str]:
    """Every pack file that cites a protocol names the path AND the SHA.

    Naming the path alone is not a pin — the path keeps resolving after the
    document is corrected. Naming the SHA alone is not usable — a reader
    cannot tell which document it belongs to.
    """
    failures = []
    for filename in PACK_FILES:
        if not (PACK_DIR / filename).exists():
            failures.append(f"{filename}: expected pack file is missing")
            continue
        text = read_pack(filename)
        cites = [p for p in PROTOCOL_BLOBS if p in text]
        if not cites:
            continue
        if "**Source pin.**" not in text:
            failures.append(
                f"{filename}: cites a protocol but carries no `**Source pin.**` "
                f"block naming the canonical document paths and the pinned commit"
            )
        if PINNED_COMMIT not in text:
            failures.append(
                f"{filename}: cites {', '.join(sorted(cites))} but carries no "
                f"pin to {PINNED_COMMIT[:8]} — a reader cannot tell which "
                f"revision of the protocol this sheet was written against"
            )
        for protocol in cites:
            blob = PROTOCOL_BLOBS[protocol]
            if blob not in text:
                failures.append(
                    f"{filename}: cites {protocol} but does not record its blob "
                    f"SHA-1 {blob[:8]}, so protocol drift cannot be detected "
                    f"from this sheet"
                )
    return failures


# ---------------------------------------------------------------------------
# The 17 K steps and the 14 S steps, byte-exact against the pinned protocol.
#
# protocol-pin proves the SOURCE has not moved. This proves the COPY still
# matches it. Both are needed: a pack whose quotations drift from an unchanged
# protocol fails just as badly as a pack pinned to a protocol that changed, and
# the pre-#394 K5/S5 inversion was one word's difference in an expected outcome.
# ---------------------------------------------------------------------------

B_K_STEP_DRIFT = register_break(
    "k-step-drift",
    "reword one K step's expected outcome in the keyboard script",
)
B_S_STEP_DRIFT = register_break(
    "s-step-drift",
    "reword one S step's expected announcement in the screen-reader script",
)

PROCEDURES = {
    "K": {
        "count": 17,
        "protocol": KSR_PROTOCOL,
        "section": "§3",
        "pack_file": "04-keyboard-script.md",
        "expected_label": "Expected outcome",
        "break": None,  # filled below
    },
    "S": {
        "count": 14,
        "protocol": KSR_PROTOCOL,
        "section": "§4",
        "pack_file": "05-screen-reader-script.md",
        "expected_label": "Expected announcement",
        "break": None,
    },
}
PROCEDURES["K"]["break"] = B_K_STEP_DRIFT
PROCEDURES["S"]["break"] = B_S_STEP_DRIFT


def protocol_steps(prefix: str) -> dict[str, tuple[str, str]]:
    """{'K1': (step, expected)} straight out of the protocol's own table."""
    text = read_protocol(PROCEDURES[prefix]["protocol"]).decode("utf-8")
    steps: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        if not re.match(r"^\|\s*" + prefix + r"\d+\s*\|", line):
            continue
        parts = line.split("|")
        if len(parts) != 5:
            raise AssertionError(
                f"protocol row does not have exactly 3 cells, so the parser "
                f"cannot be trusted: {line[:80]!r}"
            )
        steps[parts[1].strip()] = (parts[2].strip(), parts[3].strip())
    return steps


def pack_steps(prefix: str) -> dict[str, tuple[str | None, str | None]]:
    meta = PROCEDURES[prefix]
    text = read_pack(meta["pack_file"])
    if broken(meta["break"]):
        # One word, in one expected outcome — the size of the K5/S5 inversion.
        target = "> The dialog opens and focus moves into it." if prefix == "K" else (
            "> The toast/status channel (`#toast-root[aria-live=\"polite\"]`) "
            "announces the success message once."
        )
        if target not in text:
            raise AssertionError(f"drift mutation anchor not found: {target!r}")
        text = text.replace(target, target.replace("once", "at least once"), 1)
        text = text.replace(target, target.replace("into it", "near it"), 1)

    out: dict[str, tuple[str | None, str | None]] = {}
    sections = re.split(r"(?m)^### (" + prefix + r"\d+)$", text)
    # sections = [preamble, id, body, id, body, ...]
    for i in range(1, len(sections), 2):
        step_id = sections[i]
        body = sections[i + 1]

        def quoted_after(label: str) -> str | None:
            marker = re.search(
                r"\*\*" + re.escape(label) + r" \(protocol [^)]*\):\*\*\n\n> (.*)",
                body,
            )
            return marker.group(1).strip() if marker else None

        out[step_id] = (
            quoted_after("Step"),
            quoted_after(meta["expected_label"]),
        )
    return out


@check("ksr-steps-verbatim")
def check_ksr_steps_verbatim() -> list[str]:
    """All 17 K steps and all 14 S steps, byte-exact against the protocol."""
    failures = []
    for prefix, meta in PROCEDURES.items():
        source = protocol_steps(prefix)
        copy = pack_steps(prefix)

        expected_ids = [f"{prefix}{n}" for n in range(1, meta["count"] + 1)]
        if sorted(source) != sorted(expected_ids):
            failures.append(
                f"protocol {meta['section']} has {sorted(source)}, expected "
                f"{meta['count']} steps {expected_ids[0]}–{expected_ids[-1]}"
            )
        missing = [i for i in expected_ids if i not in copy]
        if missing:
            failures.append(
                f"{meta['pack_file']}: missing step section(s) {', '.join(missing)}"
            )

        for step_id in expected_ids:
            if step_id not in source or step_id not in copy:
                continue
            src_step, src_expected = source[step_id]
            got_step, got_expected = copy[step_id]
            if got_step is None:
                failures.append(f"{meta['pack_file']}: {step_id} quotes no step text")
            elif got_step != src_step:
                failures.append(
                    f"{meta['pack_file']}: {step_id} step text is not verbatim.\n"
                    f"           protocol: {src_step}\n"
                    f"           pack:     {got_step}"
                )
            if got_expected is None:
                failures.append(
                    f"{meta['pack_file']}: {step_id} quotes no expected outcome"
                )
            elif got_expected != src_expected:
                failures.append(
                    f"{meta['pack_file']}: {step_id} expected outcome is not "
                    f"verbatim.\n"
                    f"           protocol: {src_expected}\n"
                    f"           pack:     {got_expected}"
                )
    return failures


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def session_readiness() -> int:
    """What a facilitator runs before inviting a participant.

    This is NOT the same question as `check_pack.py` with no arguments. That
    asks whether the pack is built correctly. This asks whether a session may
    start today, and it exits non-zero while the ease-rating wording is
    unruled — which is the hard gate, working as intended, not a defect.
    """
    canonical, per_role = resolve_ease_wording()
    ready, reasons = ease_readiness(canonical, per_role)
    if ready:
        print("READY — the ease-rating wording is ruled and identical everywhere:")
        print(f"  {canonical!r}")
        return 0
    print("NOT READY — a session must not start. Reasons:")
    for reason in reasons:
        print(f"  - {reason}")
    print(
        f"\nFill the ruling block in {EASE_CANON} with the owner's exact wording, "
        f"then re-run this."
    )
    return 1


def collect(selected: str | None = None) -> list[str]:
    failures: list[str] = []
    for fn in CHECKS:
        if selected and fn.check_name != selected:
            continue
        failures.extend(f"{fn.check_name}: {line}" for line in fn())
    return failures


def verify_breaks(verbose: bool = False) -> int:
    """Prove every check can still fail.

    Runs the suite clean (must be clean), then re-runs it once per registered
    defect and requires each defect to produce at least one failure. A defect
    that no longer bites means the text it targeted has drifted away and the
    check is now guarding nothing — the exact way two checkers in this
    repository went quietly green while testing nothing.
    """
    global _ACTIVE_BREAK

    _ACTIVE_BREAK = None
    clean = collect()
    if clean:
        print("FAIL: the checker is not clean before break verification:")
        for line in clean:
            print(f"  {line}")
        return 1
    print(f"clean run: {len(CHECKS)} check(s) passed, 0 failures")

    toothless = []
    for name in sorted(BREAKS):
        _ACTIVE_BREAK = name
        try:
            found = collect()
        except AssertionError as exc:
            # A mutation whose anchor vanished is itself a drift report.
            print(f"[TOOTHLESS] {name}: mutation could not be applied: {exc}")
            toothless.append(name)
            continue
        finally:
            _ACTIVE_BREAK = None
        if found:
            first = found[0]
            print(f"[  bites ] {name}: {len(found)} failure(s); first: {first}")
            if verbose:
                for line in found[1:]:
                    print(f"            {line}")
        else:
            print(f"[TOOTHLESS] {name}: injected the defect and NOTHING failed")
            toothless.append(name)

    print()
    if toothless:
        print(
            "FAIL: these mutations no longer make any check fail, so the checks "
            "they belong to are guarding nothing:"
        )
        for name in toothless:
            print(f"  {name} — {BREAKS[name]}")
        print(
            "Either the defect drifted out of the text (fix the check) or the "
            "mutation's anchor moved (fix the mutation). Do not delete the "
            "mutation to make this pass."
        )
        return 1
    print(f"all {len(BREAKS)} mutation(s) still make at least one check fail")
    return 0


def run(selected: str | None = None) -> int:
    failures: list[str] = []
    ran = 0
    for fn in CHECKS:
        if selected and fn.check_name != selected:
            continue
        ran += 1
        found = fn()
        status = "FAIL" if found else "ok"
        print(f"[{status:>4}] {fn.check_name}")
        for line in found:
            print(f"         {line}")
        failures.extend(found)

    print()
    if failures:
        print(f"{len(failures)} failure(s) across {ran} check(s).")
        return 1
    print(f"{ran} check(s) passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    global _ACTIVE_BREAK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--break", dest="break_name", help="inject a named defect")
    parser.add_argument("--list-breaks", action="store_true")
    parser.add_argument("--only", help="run a single check by name")
    parser.add_argument(
        "--verify-breaks",
        action="store_true",
        help="run clean, then run every mutation and require each one to fail",
    )
    parser.add_argument(
        "--session-readiness",
        action="store_true",
        help="pre-flight gate: may a session start today? (non-zero while unruled)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.list_breaks:
        for name in sorted(BREAKS):
            print(f"{name}\t{BREAKS[name]}")
        return 0

    if args.verify_breaks:
        return verify_breaks(args.verbose)

    if args.session_readiness:
        return session_readiness()

    if args.break_name:
        if args.break_name not in BREAKS:
            print(f"unknown break: {args.break_name}", file=sys.stderr)
            return 2
        _ACTIVE_BREAK = args.break_name
        print(f"!! defect injected: {args.break_name} — {BREAKS[args.break_name]}")
        print("!! at least one check below MUST fail\n")

    return run(args.only)


if __name__ == "__main__":
    raise SystemExit(main())
