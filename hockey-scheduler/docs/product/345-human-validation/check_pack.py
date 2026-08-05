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
    + ("README.md",)
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
# Runner
# ---------------------------------------------------------------------------


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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.list_breaks:
        for name in sorted(BREAKS):
            print(f"{name}\t{BREAKS[name]}")
        return 0

    if args.verify_breaks:
        return verify_breaks(args.verbose)

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
