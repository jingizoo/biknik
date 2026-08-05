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
BREAK_TARGETS: dict[str, str] = {}


def register_break(name: str, description: str, target: str) -> str:
    """Register an injectable defect, and the check that must catch it.

    `target` is not bookkeeping. Verifying only that a mutation makes
    *something* fail is too weak: it passes when the mutation lands on a
    surface the check does not inspect, and the failure comes from an
    unrelated check. That is exactly how the permissive recording instruction
    survived in the three environment runbooks while `recordings-permitted`
    reported green — the mutation only ever touched the capture sheets, so it
    proved the suite ran, not that the rule was enforced where the defect was.
    """
    BREAKS[name] = description
    BREAK_TARGETS[name] = target
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
    "pii-fields",
)
B_PII_NAME_FIELD = register_break(
    "pii-name-field",
    "add a participant full-name field to the Arena Manager capture sheet",
    "pii-fields",
)
B_PII_CONTACT_FIELD = register_break(
    "pii-contact-field",
    "add a contact-details field to the Coach capture sheet",
    "pii-fields",
)
B_PII_EMPLOYER_FIELD = register_break(
    "pii-employer-field",
    "add an employer field to the League Admin capture sheet",
    "pii-fields",
)
B_PII_CODE_MISSING = register_break(
    "pii-code-missing",
    "drop the anonymous participant-code row from the Arena Manager sheet",
    "pii-fields",
)
B_PII_RULE_MISSING = register_break(
    "pii-rule-missing",
    "delete the no-names / mapping-outside-the-repo / redaction rules from the Coach sheet",
    "pii-rules",
)
B_RECORDINGS_PERMITTED = register_break(
    "recordings-permitted",
    "let the pack permit audio/video recording with no retention rule in force",
    "pii-rules",
)
B_SUPERSESSION_NOTE_MISSING = register_break(
    "supersession-note-missing",
    "remove the note that the earlier 'explicit blank for participant identity' requirement is superseded",
    "pii-supersession-note",
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
    return mut(
        _ACTIVE_BREAK or "(none)",
        text,
        text.replace(anchor, anchor + "\n" + row, 1),
        f"insert row after {anchor!r}",
    )


_IDENTITY_ROW = "| Participant identity (see the privacy rule below) | |"


def mut(break_name: str, before, after, what: str = ""):
    """Apply a mutation, and refuse to let it silently do nothing.

    A mutation whose anchor text has moved becomes a no-op: the injected defect
    is never actually injected, and the run stays green (or goes red for some
    unrelated reason) while the mutation reports success. That is the same
    failure as a check that stops checking, and it has already happened twice
    on this branch — once where a mutation's fixture tripped a second guard,
    and once where half of `readme-denies-ci` stopped matching after the
    sentence it targeted was rewrapped.

    verify_breaks catches the AssertionError and reports the break as
    TOOTHLESS, naming it.
    """
    if before == after:
        raise AssertionError(
            f"--break {break_name} had no effect{f' ({what})' if what else ''} — "
            f"its anchor text has moved, so this mutation injects nothing and "
            f"tests nothing. Fix the anchor; do not delete the mutation."
        )
    return after


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
            text = mut(
                B_PII_CODE_MISSING,
                text,
                re.sub(r"(?m)^\|\s*Participant code[^\n]*\n", "", text),
            )
    if name == "11-capture-sheet-coach.md":
        if broken(B_PII_CONTACT_FIELD):
            text = _inject_row(text, "| Date | |", "| Contact details | |")
        if broken(B_PII_RULE_MISSING):
            for marker in (
                RULE_NO_IDENTIFIERS,
                RULE_MAPPING_OUTSIDE_REPO,
                RULE_REDACTION_CHECK,
            ):
                text = mut(B_PII_RULE_MISSING, text, flex(marker).sub("", text), marker[:40])

    if name == "06-task-prompts-league-admin.md" and broken(B_PRIMARY_OLD_RULE):
        text = mut(
            B_PRIMARY_OLD_RULE,
            text,
            text.replace(
                "- Record their exact words",
                "- Let them proceed and score the match against the control they "
                "actually named.\n- Record their exact words",
                1,
            ),
        )
    if name == "09-capture-sheet-league-admin.md" and broken(B_PRIMARY_RULE_MISSING):
        text = mut(B_PRIMARY_RULE_MISSING, text, flex(RULE_NEVER_YES).sub("", text))

    if name == "07-task-prompts-arena-manager.md" and broken(B_EASE_LOCAL_COPY):
        text += (
            "\n\n```text\nOn a scale of one to five, how easy did you find that?\n```\n"
        )
    if name == "08-task-prompts-coach.md" and broken(B_EASE_POINTER_MISSING):
        text = mut(
            B_EASE_POINTER_MISSING, text, text.replace(f"({EASE_CANON})", "(README.md)")
        )
    if name == "02-environment-arena-manager.md" and broken(B_GATE_ROW_MISSING):
        text = mut(
            B_GATE_ROW_MISSING,
            text,
            re.sub(r"(?m)^\|[^\n]*" + re.escape(GATE_MARKER) + r"[^\n]*\n", "", text),
        )
    if name == "README.md" and broken(B_README_NONBLOCKING):
        text += "\n\n**None of these blocks a session.**\n"
    if name == "README.md" and broken(B_README_DENIES_CI):
        text = mut(
            B_README_DENIES_CI,
            text,
            flex(README_CI_ADMISSION).sub("It adds nothing outside this pack", text),
            "drop the CI admission",
        )
        text = mut(
            B_README_DENIES_CI,
            text,
            flex(
                "This pack adds **no application code, no product behaviour, and no "
                "change to any application test**"
            ).sub(OLD_README_SCOPE_DENIAL.rstrip("."), text),
            "restore the CI denial",
        )
    if name == "README.md" and broken(B_BLOCKQUOTE_NOT_PROTOCOL):
        text += (
            "\n\n> Ask the participant to confirm the room is quiet before you\n"
            "> begin, and note anything unusual about the setup.\n"
        )
    if name == "01-environment-league-admin.md" and broken(
        B_RECORDING_RUNBOOK_PERMISSIVE
    ):
        text = mut(
            B_RECORDING_RUNBOOK_PERMISSIVE,
            text,
            text.replace(
                "3. These sessions run without audio or video recording.",
                OLD_PERMISSIVE_RECORDING_STEP,
                1,
            ),
        )
    if name == "02-environment-arena-manager.md" and broken(
        B_RECORDING_PROHIBITION_MISSING
    ):
        text = mut(
            B_RECORDING_PROHIBITION_MISSING,
            text,
            flex(RULE_NO_RECORDINGS).sub("Reset the browser profile.", text),
        )

    # Scoped to the capture sheets, which is the surface this mutation's own
    # check (pii-rules) inspects. It used to run against every file, so on the
    # files with no such sentence it was a silent no-op — invisible, because
    # the sheets it did hit kept the check red.
    if name in CAPTURE_SHEETS and broken(B_RECORDINGS_PERMITTED):
        text = mut(
            B_RECORDINGS_PERMITTED,
            text,
            flex(RULE_NO_RECORDINGS).sub(
                "Recording is at the moderator's discretion.", text
            ),
            f"drop the no-recording rule from {name}",
        )
    if broken(B_SUPERSESSION_NOTE_MISSING) and flex(
        RULE_SUPERSEDED_IDENTITY
    ).search(text):
        text = mut(
            B_SUPERSESSION_NOTE_MISSING,
            text,
            flex(RULE_SUPERSEDED_IDENTITY).sub("", text),
        )
    if broken(B_PIN_MISSING) and name == "09-capture-sheet-league-admin.md":
        text = mut(B_PIN_MISSING, text, text.replace(PINNED_COMMIT, "origin/main"))
        for blob in PROTOCOL_BLOBS.values():
            text = mut(B_PIN_MISSING, text, text.replace(blob, ""), f"blob {blob[:8]}")

    return text


def read_protocol(filename: str) -> bytes:
    data = (PRODUCT_DIR / filename).read_bytes()
    if broken(B_PROTOCOL_DRIFT) and filename == KSR_PROTOCOL:
        # Stand in for "the protocol was corrected upstream and the pack was
        # not re-verified against it" — the pre-#394 K5/S5 failure mode.
        data = mut(
            B_PROTOCOL_DRIFT,
            data,
            data.replace(b"re-filters", b"re-renders", 1),
            "reword K5's expected outcome in the protocol",
        )
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
    "pii-export",
)
B_EXPORT_REDACTION_OFF = register_break(
    "export-redaction-off",
    "stop redacting unrelated personal disclosures out of quotes",
    "pii-export",
)
B_EXPORT_KEY_ALLOWLIST_OFF = register_break(
    "export-key-allowlist-off",
    "export the whole source record instead of the allowed fields",
    "pii-export",
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
    "primary-action-rubric",
)
B_RUBRIC_TRANSCRIPTION_YES = register_break(
    "rubric-transcription-yes",
    "let the §6 transcription turn a diagnostic match into a primary-action Yes",
    "primary-action-rubric",
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
    "primary-action-text",
)
B_PRIMARY_RULE_MISSING = register_break(
    "primary-action-rule-missing",
    "delete the never-Yes rule from the League Admin capture sheet",
    "primary-action-text",
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
    "ease-single-source",
)
B_EASE_POINTER_MISSING = register_break(
    "ease-pointer-missing",
    "drop the Coach prompt sheet's reference to the canonical ease wording",
    "ease-single-source",
)
B_EASE_READINESS_BLIND = register_break(
    "ease-readiness-blind",
    "make the readiness check ignore an unruled (blank) ease wording",
    "ease-readiness",
)
B_EASE_DIVERGENCE_BLIND = register_break(
    "ease-divergence-blind",
    "make the readiness check ignore role sheets resolving to different wording",
    "ease-readiness",
)
B_GATE_ROW_MISSING = register_break(
    "gate-row-missing",
    "remove the hard pre-flight gate row from the Arena Manager environment sheet",
    "ease-preflight-gate",
)
B_README_NONBLOCKING = register_break(
    "readme-nonblocking-claim",
    "put the README's 'none of these blocks a session' claim back",
    "ease-preflight-gate",
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
    "ease-readiness",
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
# Pack-authored text vs. quoted protocol text.
#
# This pack reserves `>` for verbatim protocol text and writes everything of
# its own outside a blockquote. That convention is what lets a check tell
# "the protocol says recording is optional" (which is true, and must stay
# quoted exactly) apart from "this pack tells you to record" (which
# contradicts an owner ruling).
#
# The convention is therefore load-bearing, so it is asserted rather than
# assumed: every blockquote in the pack must appear verbatim in one of the two
# pinned protocols. Without this, a pack-authored instruction could be smuggled
# behind a `>` and every check below would skip it by design.
# ---------------------------------------------------------------------------

B_BLOCKQUOTE_NOT_PROTOCOL = register_break(
    "blockquote-not-protocol",
    "smuggle a pack-authored instruction into a `>` blockquote in the README",
    "blockquote-is-protocol-text",
)


def normalize(text: str) -> str:
    return " ".join(text.split())


def blockquote_blocks(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    cur: list[str] = []
    start: int | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(">"):
            if start is None:
                start = lineno
            cur.append(re.sub(r"^\s*>\s?", "", line))
        else:
            if cur and start is not None:
                out.append((start, "\n".join(cur)))
            cur, start = [], None
    if cur and start is not None:
        out.append((start, "\n".join(cur)))
    return out


def pack_authored_lines(text: str) -> list[tuple[int, str]]:
    """Every line the pack wrote itself — blockquotes excluded."""
    return [
        (lineno, line)
        for lineno, line in enumerate(text.splitlines(), start=1)
        if not line.lstrip().startswith(">")
    ]


@check("blockquote-is-protocol-text")
def check_blockquote_is_protocol_text() -> list[str]:
    failures = []
    protocols = {
        name: normalize(read_protocol(name).decode("utf-8"))
        for name in PROTOCOL_BLOBS
    }
    for filename in PACK_FILES:
        if not (PACK_DIR / filename).exists():
            continue
        for lineno, block in blockquote_blocks(read_pack(filename)):
            norm = normalize(block)
            if not norm:
                continue
            if not any(norm in body for body in protocols.values()):
                failures.append(
                    f"{filename}:{lineno}: blockquote is not verbatim protocol "
                    f"text. `>` is this pack's marker for quoted protocol, and "
                    f"every check that separates pack instructions from quoted "
                    f"protocol relies on it: {norm[:110]!r}"
                )
    return failures


# ---------------------------------------------------------------------------
# The owner's no-recording ruling, enforced across EVERY pack-authored line.
#
# The three environment runbooks used to end with a pack-authored step reading
# "Recording, if any, is stored outside this repository — moderated protocol
# §1.6 covers this and nothing in this pack overrides it." That is backwards:
# the pack DOES override it, by owner ruling. A facilitator following the
# runbook — on the between-participants path, the one operational page open
# during a session — was told recording was permitted, while the capture sheet
# in their other hand said it was prohibited.
#
# The original `recordings-permitted` mutation did not catch this, because it
# only ever removed the prohibition from the CAPTURE SHEETS. It made the suite
# go red, so it looked like proof the rule was enforced; it was proof only that
# the rule was enforced on the one surface the mutation touched. Hence the
# three directions below, and hence BREAK_TARGETS above.
# ---------------------------------------------------------------------------

B_RECORDING_RUNBOOK_PERMISSIVE = register_break(
    "recording-runbook-permissive",
    "restore the exact old 'Recording, if any' instruction in the League Admin runbook",
    "recording-consistency",
)
B_RECORDING_PROHIBITION_MISSING = register_break(
    "recording-prohibition-missing",
    "delete the no-recording instruction from the Arena Manager runbook",
    "recording-consistency",
)

OLD_PERMISSIVE_RECORDING_STEP = (
    "3. Recording, if any, is stored outside this repository — moderated protocol\n"
    "   §1.6 covers this and nothing in this pack overrides it."
)

# Phrasings that tell a reader recording may happen. Matched against
# pack-authored text only: the protocol genuinely does permit recording, and
# its own wording must stay quoted exactly, so blockquotes are exempt.
PERMISSIVE_RECORDING_INSTRUCTIONS = (
    ('"recording, if any"', r"recording,?\s+if\s+any"),
    ('"if recording ..."', r"\bif\s+recording\b"),
    (
        "recording called optional/permitted in the pack's own voice",
        r"\brecording\s+is\s+(?:optional|permitted|allowed|fine|acceptable|up\s+to)",
    ),
    # Deliberately NOT a pattern: "recording at the moderator's choice".
    # The capture sheets and README use that phrase correctly, to state what
    # §1.6 permits immediately before explaining why this pack overrides it.
    # Banning it would force the pack to stop saying what it is overriding,
    # which is worse than the defect. The patterns here are instruction-shaped
    # instead — they match the pack telling someone recording may happen, not
    # the pack describing the protocol.
    (
        "an instruction that a recording may be made",
        r"\byou\s+may\s+record\b|\brecord\s+(?:the\s+session|audio|video)\b"
        r"|\brecording\s+is\s+your\s+call\b",
    ),
    ('"nothing in this pack overrides it"', r"nothing\s+in\s+this\s+pack\s+overrides\s+it"),
    (
        "an instruction on where a recording is stored (implies one exists)",
        r"recording[^.]{0,60}?\bis\s+stored\s+outside\b",
    ),
)

RECORDING_MENTION = re.compile(r"\brecordings?\b|\baudio\b|\bvideo\b", re.IGNORECASE)

# Files that carry operational instructions for running a session. Each must
# state the rule outright — silence is how the runbooks got it wrong.
RECORDING_RULE_FILES = ENVIRONMENT_SHEETS + tuple(CAPTURE_SHEETS) + ("README.md",)


@check("recording-consistency")
def check_recording_consistency() -> list[str]:
    """Every pack-authored instruction must agree that recording is OFF.

    Three directions, because catching only the first would pass a file that
    simply says nothing:
      1. the prohibition is present in every operational file;
      2. any file that mentions recording at all states the rule;
      3. no pack-authored line carries permissive recording language.
    """
    failures = []

    # (1) Required presence.
    for filename in RECORDING_RULE_FILES:
        if not flex(RULE_NO_RECORDINGS).search(read_pack(filename)):
            failures.append(
                f"{filename}: does not state the no-recording rule (expected the "
                f"exact sentence {RULE_NO_RECORDINGS!r}). This file gives "
                f"operational instructions for running a session; leaving the "
                f"rule out is how the runbooks came to permit what the capture "
                f"sheets forbid"
            )

    # (2) Co-location: mention it, state the rule.
    for filename in PACK_FILES:
        if not (PACK_DIR / filename).exists():
            continue
        text = read_pack(filename)
        mentions = [
            lineno
            for lineno, line in pack_authored_lines(text)
            if RECORDING_MENTION.search(line)
        ]
        if mentions and not flex(RULE_NO_RECORDINGS).search(text):
            failures.append(
                f"{filename}: mentions recording in its own voice (line "
                f"{mentions[0]}) but never states the no-recording rule"
            )

    # (3) No permissive pack-authored instruction, anywhere.
    for filename in PACK_FILES:
        if not (PACK_DIR / filename).exists():
            continue
        lines = pack_authored_lines(read_pack(filename))
        authored = normalize("\n".join(line for _, line in lines))
        for label, pattern in PERMISSIVE_RECORDING_INSTRUCTIONS:
            match = re.search(pattern, authored, re.IGNORECASE)
            if not match:
                continue
            # Attribute to the line the match actually starts on. Matching on
            # the first word alone pointed at the first line that merely
            # contained that word, which named innocent lines.
            head = normalize(match.group(0)).split()[:4]
            head_rx = re.compile(r"\s+".join(re.escape(w) for w in head), re.IGNORECASE)
            where = next(
                (
                    lineno
                    for lineno, line in lines
                    if head_rx.search(normalize(line))
                ),
                "(wrapped across lines)",
            )
            failures.append(
                f"{filename}:{where}: pack-authored text still permits "
                f"recording — {label}: {match.group(0)!r}. The protocol does "
                f"permit it and its own wording stays quoted; every instruction "
                f"the pack writes itself must say recording is off"
            )
    return failures


# ---------------------------------------------------------------------------
# The pack's public scope claim vs. what the diff actually ships, and the
# CI job's description of the guarantee it enforces.
#
# THIS CHECK DELIBERATELY READS TWO FILES OUTSIDE THE PACK DIRECTORY.
# That is not an oversight, and it must not be "tidied up" into a
# pack-only check. The reason: this checker is registered as a CI job, and
# the job's own comments and log output state the contract the job
# enforces. If the registration drifts away from the contract — or the
# README goes on claiming the pack touches no CI while the job exists —
# then the surfaces a reviewer and a maintainer actually read are lying
# about the thing this file does. Nothing inside the pack directory can
# see that. So the checker guards its own registration.
#
# Concretely, this exists because the workflow kept teaching the WEAKER
# guarantee ("every mutation must make at least one check fail") after the
# harness had moved to the stronger one ("its OWN named check"). That
# weaker rule is exactly what let `recordings-permitted` bite the capture
# sheets while the live defect sat in the runbooks. A maintainer reading
# the workflow could reasonably have deleted BREAK_TARGETS believing the
# documented guarantee still held.
# ---------------------------------------------------------------------------

REPO_ROOT = PACK_DIR.parents[3]
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "hockey-scheduler-ci.yml"
HUMAN_VALIDATION_JOB = "human-validation-pack:"

B_README_DENIES_CI = register_break(
    "readme-denies-ci",
    "restore the README sentence claiming the pack adds no CI configuration",
    "scope-and-ci-contract",
)
B_WORKFLOW_WEAK_CONTRACT = register_break(
    "workflow-weak-contract",
    "restore the workflow's superseded 'at least one check fail' guarantee",
    "scope-and-ci-contract",
)

OLD_README_SCOPE_DENIAL = (
    "This pack adds no application code, tests, CI configuration, or product\n"
    "behaviour, and it modifies neither protocol."
)

# The README must say, in its own words, that this change reaches CI.
README_CI_ADMISSION = (
    "It adds a checker for this pack and a dedicated CI job that runs it"
)

# Ways a document can deny touching CI.
CI_DENIAL_PATTERNS = (
    ("\"adds no ... CI ...\"", r"adds?\s+no\b[^.]{0,120}\bCI\b"),
    ("\"no CI configuration/change/job\"", r"\bno\s+CI\s+(?:configuration|change|job)"),
    ("\"does not touch/change CI\"", r"does\s+not\s+(?:touch|change|modify|add)\s+[^.]{0,40}\bCI\b"),
)

# The superseded, weaker falsification guarantee.
WEAK_CONTRACT_PATTERNS = (
    ("\"at least one check\"", r"at\s+least\s+one\s+check"),
    ("\"make(s) a check fail\"", r"makes?\s+a\s+check\s+fail"),
    ("\"some/any check fails\"", r"\b(?:some|any)\s+check\s+fails?\b"),
)

STRONG_CONTRACT_PHRASE = "own named check"


def read_ci_workflow() -> str:
    text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
    if broken(B_WORKFLOW_WEAK_CONTRACT):
        # The anchor is the comment EXACTLY as it wraps in the file. An earlier
        # version anchored on the unwrapped sentence, which never matched — so
        # this half of the mutation injected nothing while the other half kept
        # the check red. Found only once every edit was forced through mut().
        text = mut(
            B_WORKFLOW_WEAK_CONTRACT,
            text,
            text.replace(
                "      #   (b) EVERY --break mutation must still make the "
                "mutation's own named\n      #       check fail.",
                "      #   (b) EVERY --break mutation must still make at least "
                "one check fail.",
                1,
            ),
            "weaken the YAML comment",
        )
        text = mut(
            B_WORKFLOW_WEAK_CONTRACT,
            text,
            text.replace(
                "every injected defect must still fail its own named check",
                "every injected defect must still make a check fail",
            ),
            "weaken the emitted log line",
        )
    return text


@check("scope-and-ci-contract")
def check_scope_and_ci_contract() -> list[str]:
    """README's scope claim and the CI job's stated guarantee must both be true.

    Reads `README.md` and `.github/workflows/hockey-scheduler-ci.yml` — see the
    block comment above for why reaching outside the pack directory is the point
    of this check rather than a bug in it.
    """
    failures = []
    readme = read_pack("README.md")
    workflow = read_ci_workflow()
    job_exists = HUMAN_VALIDATION_JOB in workflow

    # --- Direction 1: the README must not deny a CI change that exists -------
    if job_exists:
        for label, pattern in CI_DENIAL_PATTERNS:
            match = re.search(pattern, normalize(readme), re.IGNORECASE)
            if match:
                failures.append(
                    f"README.md: denies touching CI ({label}: {match.group(0)!r}) "
                    f"while the `{HUMAN_VALIDATION_JOB[:-1]}` job exists in "
                    f"{CI_WORKFLOW_PATH.name}. README is the scope surface a "
                    f"reviewer sizes this change from; it must not hide a real "
                    f"CI change"
                )
        if not flex(README_CI_ADMISSION).search(readme):
            failures.append(
                f"README.md: never states that this change adds a checker and a "
                f"CI job (expected {README_CI_ADMISSION!r}). Not denying CI is "
                f"not the same as disclosing it — a reader must be able to tell "
                f"from README alone that this touches CI"
            )
    else:
        if flex(README_CI_ADMISSION).search(readme):
            failures.append(
                f"README.md: claims a dedicated CI job runs the checker, but no "
                f"`{HUMAN_VALIDATION_JOB[:-1]}` job exists in "
                f"{CI_WORKFLOW_PATH.name} — the claim is now false in the other "
                f"direction"
            )

    # --- Direction 2: the workflow must not teach the superseded contract ----
    #
    # Scanned twice. Line by line catches the emitted `echo` text. Then each
    # run of consecutive comment lines is unwrapped and scanned as one string,
    # because a YAML comment wraps at the margin and the phrase this is looking
    # for straddles the wrap — "... must still make at least one\n#  check
    # fail." is invisible to a per-line scan, and that is not a hypothetical:
    # the mutation for this very check had a stale anchor for exactly that
    # reason. Blocks are joined individually, never the whole file, so two
    # unrelated comments cannot concatenate into a false match.
    def _report(lineno: int, label: str, snippet: str) -> None:
        failures.append(
            f"{CI_WORKFLOW_PATH.name}:{lineno}: describes the superseded "
            f"any-check guarantee ({label}) — {snippet[:90]!r}. That is the "
            f"rule that let a mutation bite the wrong surface while the real "
            f"defect went unseen; a maintainer reading it could remove "
            f"BREAK_TARGETS believing the guarantee held"
        )

    lines = workflow.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for label, pattern in WEAK_CONTRACT_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                _report(lineno, label, line.strip())

    block: list[str] = []
    block_start = 0
    for lineno, line in enumerate(lines + [""], start=1):
        if line.lstrip().startswith("#"):
            if not block:
                block_start = lineno
            block.append(re.sub(r"^\s*#\s?", "", line))
            continue
        if block:
            joined = normalize(" ".join(block))
            for label, pattern in WEAK_CONTRACT_PATTERNS:
                match = re.search(pattern, joined, re.IGNORECASE)
                # Only report if the per-line scan did not already catch it.
                if match and not any(
                    re.search(pattern, l, re.IGNORECASE) for l in block
                ):
                    _report(
                        block_start,
                        f"{label}, wrapped across comment lines",
                        match.group(0),
                    )
            block = []

    # --- Direction 3: the workflow must state the real contract, in BOTH the
    # comments a maintainer reads in the file and the text emitted into the
    # Actions log, which is the only version most people ever see.
    if job_exists:
        comment_lines = [
            l for l in workflow.splitlines() if l.lstrip().startswith("#")
        ]
        emitted_lines = [l for l in workflow.splitlines() if "echo " in l]
        if not any(STRONG_CONTRACT_PHRASE in l for l in comment_lines):
            failures.append(
                f"{CI_WORKFLOW_PATH.name}: no comment states the actual "
                f"guarantee ({STRONG_CONTRACT_PHRASE!r})"
            )
        if not any(STRONG_CONTRACT_PHRASE in l for l in emitted_lines):
            failures.append(
                f"{CI_WORKFLOW_PATH.name}: the step emits no line stating the "
                f"actual guarantee ({STRONG_CONTRACT_PHRASE!r}) — the Actions log "
                f"is the version a maintainer reads when the job fails"
            )
    return failures


# ---------------------------------------------------------------------------
# Guard the guard: every mutation edit must go through mut().
#
# mut() raises when an edit changes nothing, which is how a mutation whose
# anchor text has drifted gets reported instead of silently injecting nothing.
# But mut() only protects the edits that actually call it. A raw
# `text.replace(...)` inside a mutation body has no guard at all: if its anchor
# moves, the edit becomes a no-op, and the only symptom is a mutation that goes
# on "passing" for the wrong reason.
#
# That is not hypothetical. `readme-denies-ci` performs TWO edits; one anchor
# moved after a sentence was rewrapped, and because the OTHER edit still fired,
# the mutation kept failing its check and nothing complained. Comparing the
# whole before/after cannot see that — each edit has to be guarded on its own.
#
# So this check reads this file's own source, finds every mutation body (an
# `if broken(...)` block, not an `if not broken(...)` block, which is
# production code the mutation disables), and fails if any of them performs a
# string replacement outside mut(). Part 1 of the fix converts today's raw
# edits; this check is the part that stops the next one being added.
# ---------------------------------------------------------------------------

import ast  # noqa: E402  (kept beside the check that needs it)

B_RAW_REPLACE_IN_MUTATION = register_break(
    "raw-replace-in-mutation",
    "reintroduce an unguarded .replace() inside a mutation body",
    "mutation-guard",
)

# Calls that are themselves guarded, so a mutation body may use them directly.
GUARDED_EDIT_CALLS = {"mut", "_inject_row"}
# Helpers that exist only to perform mutation edits; their bodies are held to
# the same rule as a mutation body, so the guard cannot be bypassed by moving
# a raw replacement one function call away.
MUTATION_HELPERS = {"_inject_row"}
REPLACEMENT_METHODS = {"replace", "sub", "subn"}

OWN_SOURCE = Path(__file__).resolve()


def read_own_source() -> str:
    text = OWN_SOURCE.read_text(encoding="utf-8")
    if broken(B_RAW_REPLACE_IN_MUTATION):
        text = mut(
            B_RAW_REPLACE_IN_MUTATION,
            text,
            text.replace(
                '        text = mut(\n'
                '            B_EASE_POINTER_MISSING, text, '
                'text.replace(f"({EASE_CANON})", "(README.md)")\n'
                '        )',
                '        text = text.replace(f"({EASE_CANON})", "(README.md)")',
                1,
            ),
            "un-guard the ease-pointer mutation",
        )
    return text


def _calls_broken_unnegated(test: ast.expr) -> bool:
    """True for `if broken(X)` / `if cond and broken(X)`; False for `not broken(X)`."""

    def is_broken_call(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "broken"
        )

    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return False
    if is_broken_call(test):
        return True
    if isinstance(test, ast.BoolOp):
        return any(is_broken_call(v) for v in test.values)
    return False


def _replacement_calls(node: ast.AST) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in REPLACEMENT_METHODS
    ]


def _unguarded_edits(body: list[ast.stmt], where: str) -> list[str]:
    """Statements in `body` that replace text without going through mut()."""
    problems = []
    for stmt in body:
        for node in ast.walk(stmt):
            # Returns count: `return text.replace(...)` edits text just as much
            # as an assignment does, and skipping them left _inject_row's own
            # replacement unpoliced by the first version of this check.
            if not isinstance(node, (ast.Assign, ast.AugAssign, ast.Return)):
                continue
            value = node.value
            if value is None or not _replacement_calls(value):
                continue
            outermost_is_guarded = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in GUARDED_EDIT_CALLS
            )
            if not outermost_is_guarded:
                problems.append(
                    f"check_pack.py:{node.lineno}: {where} performs a string "
                    f"replacement outside mut(). An edit with no guard becomes a "
                    f"silent no-op the moment its anchor text moves, and the "
                    f"mutation goes on reporting success"
                )
    return problems


@check("mutation-guard")
def check_mutation_guard() -> list[str]:
    """No mutation may edit text without mut() proving the edit landed."""
    tree = ast.parse(read_own_source(), filename=str(OWN_SOURCE))
    failures = []

    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _calls_broken_unnegated(node.test):
            failures.extend(
                _unguarded_edits(node.body, f"mutation body at line {node.lineno}")
            )
        if isinstance(node, ast.FunctionDef) and node.name in MUTATION_HELPERS:
            failures.extend(
                _unguarded_edits(node.body, f"mutation helper {node.name}()")
            )

    # Anti-vacuity. If the mutation-body detector ever stops matching — someone
    # renames `broken()`, or wraps the guards differently — this check would
    # inspect nothing and report clean, which is the exact failure it exists to
    # prevent, one level up. So: every mut() call in the file must be one this
    # walk actually reached. A detector that goes blind drops that count to
    # zero while the calls are still plainly there.
    all_mut_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "mut"
    ]
    reached: set[int] = set()
    for node in ast.walk(tree):
        in_scope = (
            isinstance(node, ast.If) and _calls_broken_unnegated(node.test)
        ) or (isinstance(node, ast.FunctionDef) and node.name in MUTATION_HELPERS)
        if not in_scope:
            continue
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "mut"
                ):
                    reached.add(inner.lineno)

    unreached = [c.lineno for c in all_mut_calls if c.lineno not in reached]
    if unreached:
        failures.append(
            f"{len(unreached)} of {len(all_mut_calls)} mut() call(s) sit outside "
            f"any mutation body this check can see (lines "
            f"{', '.join(str(l) for l in sorted(unreached))}). Either a guarded "
            f"edit has escaped its `if broken(...)` block, or the detector has "
            f"stopped recognising mutation bodies and is now policing nothing"
        )
    return failures


# ---------------------------------------------------------------------------
# Safeguard S1 — the source pin
# ---------------------------------------------------------------------------

B_PROTOCOL_DRIFT = register_break(
    "protocol-drift",
    "change one word of the keyboard/screen-reader protocol, as an upstream correction would",
    "protocol-pin",
)
B_PIN_MISSING = register_break(
    "pin-missing",
    "strip the source pin from the League Admin capture sheet",
    "pin-present",
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
    "ksr-steps-verbatim",
)
B_S_STEP_DRIFT = register_break(
    "s-step-drift",
    "reword one S step's expected announcement in the screen-reader script",
    "ksr-steps-verbatim",
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

# One drift edit per procedure: the exact quoted line, and the single word to
# change in it. Kept as data so each mutation has exactly one anchor to verify.
DRIFT_TARGETS = {
    "K": ("> The dialog opens and focus moves into it.", "into it", "near it"),
    "S": (
        '> The toast/status channel (`#toast-root[aria-live="polite"]`) '
        "announces the success message once.",
        "once",
        "at least once",
    ),
}


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
        #
        # This used to run BOTH substitutions unconditionally against a single
        # target, so exactly one of them fired per prefix and the other was a
        # guaranteed no-op. Harmless while the live edit worked, and completely
        # invisible if the live one ever stopped matching. One edit per prefix
        # now, each guarded.
        target, old_word, new_word = DRIFT_TARGETS[prefix]
        text = mut(
            meta["break"],
            text,
            text.replace(target, target.replace(old_word, new_word), 1),
            f"reword {prefix}-step expected outcome ({old_word!r} -> {new_word!r})",
        )

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


def collect_by_check(selected: str | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for fn in CHECKS:
        if selected and fn.check_name != selected:
            continue
        found = fn()
        if found:
            out[fn.check_name] = found
    return out


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
    misdirected = []
    for name in sorted(BREAKS):
        target = BREAK_TARGETS[name]
        _ACTIVE_BREAK = name
        try:
            by_check = collect_by_check()
        except AssertionError as exc:
            # A mutation whose anchor vanished is itself a drift report.
            print(f"[TOOTHLESS] {name}: mutation could not be applied: {exc}")
            toothless.append(name)
            continue
        finally:
            _ACTIVE_BREAK = None

        if not by_check:
            print(f"[TOOTHLESS] {name}: injected the defect and NOTHING failed")
            toothless.append(name)
        elif target not in by_check:
            # The suite went red, but not where it was supposed to. Counting
            # this as "bites" is what let a mutation on the wrong surface look
            # like proof that the rule was enforced.
            print(
                f"[MISDIRECT] {name}: {target} PASSED; only "
                f"{', '.join(sorted(by_check))} failed"
            )
            misdirected.append((name, target, sorted(by_check)))
        else:
            hits = by_check[target]
            print(f"[  bites ] {name} -> {target}: {hits[0]}")
            if verbose:
                for line in hits[1:]:
                    print(f"            {line}")

    print()
    if toothless:
        print(
            "FAIL: these mutations no longer make any check fail, so the checks "
            "they belong to are guarding nothing:"
        )
        for name in toothless:
            print(f"  {name} — {BREAKS[name]}")
    if misdirected:
        print(
            "FAIL: these mutations made the suite red, but their OWN check "
            "passed. The defect landed on a surface that check does not "
            "inspect, so the failure came from somewhere else and proves "
            "nothing about the rule:"
        )
        for name, target, actual in misdirected:
            print(f"  {name}: expected {target} to fail; instead {actual} failed")
    if toothless or misdirected:
        print(
            "Either the defect drifted out of the text (fix the check), the "
            "mutation's anchor moved (fix the mutation), or the check does not "
            "cover the surface the mutation targets (widen the check). Do not "
            "delete the mutation to make this pass."
        )
        return 1
    print(
        f"all {len(BREAKS)} mutation(s) still make their OWN named check fail"
    )
    return 0


def run(selected: str | None = None) -> int:
    known = [fn.check_name for fn in CHECKS]
    if selected and selected not in known:
        # Running zero checks and exiting 0 is a vacuous pass — the same shape
        # as a mutation that stops biting. Refuse instead.
        print(f"unknown check: {selected}", file=sys.stderr)
        print(f"known checks: {', '.join(known)}", file=sys.stderr)
        return 2

    failures: list[str] = []
    ran = 0
    for fn in CHECKS:
        if selected and fn.check_name != selected:
            continue
        ran += 1
        try:
            found = fn()
        except AssertionError as exc:
            # A mutation that no longer applies is a real finding, not a crash.
            print(f"[FAIL] {fn.check_name}")
            print(f"         {exc}")
            failures.append(f"{fn.check_name}: {exc}")
            continue
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
