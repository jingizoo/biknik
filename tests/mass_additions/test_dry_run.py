import json
from pathlib import Path

from ofam_mass_additions.models import MassAddition
from ofam_mass_additions.oracle.mock_client import MockOracleFaClient
from ofam_mass_additions.runner.cycle import MassAdditionCycleRunner
from ofam_mass_additions.runner.dry_run import run_dry_run


CSV_TEXT = """mass_addition_id,book_type_code,region,queue_name,posting_status,tag_number,serial_number,po_number,invoice_number,expected_action
MA-100,CORP_BOOK,US,NEW,NEW,TAG-100,SN-100,PO-100,INV-100,auto_update
MA-101,CORP_BOOK,US,NEW,NEW,TAG-404,SN-404,PO-404,INV-404,exception_cmdb_not_found
MA-102,CORP_BOOK,US,NEW,NEW,TAG-102,SN-102,PO-102,INV-102,exception_missing_ccid
MA-103,CORP_BOOK,US,NEW,NEW,TAG-103,SN-103,PO-103,INV-103,exception_inactive_ccid
MA-104,OTHER_BOOK,US,NEW,NEW,TAG-104,SN-104,PO-104,INV-104,skip_outside_book
"""


class ErrorOracleClient(MockOracleFaClient):
    def update_mass_addition(self, _payload: dict[str, str]) -> str:
        return '{"X_RETURN_STATUS":"E","X_MSG_DATA":"oracle validation failed"}'


def test_dry_run_generates_expected_outputs(tmp_path: Path) -> None:
    input_csv = tmp_path / "pilot.csv"
    output_dir = tmp_path / "output"
    input_csv.write_text(CSV_TEXT, encoding="utf-8")

    result = run_dry_run(input_csv=input_csv, output_dir=output_dir)

    assert result == {"total": 4, "auto_update": 1, "exception": 3}
    assert (output_dir / "proposed_updates.csv").exists()
    assert (output_dir / "exceptions.csv").exists()
    assert (output_dir / "audit.jsonl").exists()

    exception_text = (output_dir / "exceptions.csv").read_text(encoding="utf-8")
    assert "CMDB not found" in exception_text
    assert "Missing CCID" in exception_text
    assert "Inactive CCID" in exception_text


def test_live_mode_records_oracle_errors_as_exceptions(tmp_path: Path) -> None:
    rows = {
        "MA-100": MassAddition(
            mass_addition_id="MA-100",
            book_type_code="CORP_BOOK",
            region="US",
            posting_status="NEW",
            tag_number="TAG-100",
        )
    }
    runner = MassAdditionCycleRunner(
        oracle_client=ErrorOracleClient(rows=rows),
        run_mode="live",
        pilot_book="CORP_BOOK",
        pilot_region="US",
    )

    result = runner.run(output_dir=tmp_path)

    assert result == {"total": 1, "auto_update": 1, "exception": 1}
    exception_text = (tmp_path / "exceptions.csv").read_text(encoding="utf-8")
    assert "Oracle validation failed" in exception_text

    line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()[0]
    payload = json.loads(line)
    assert payload["status"] == "exception"


def test_oracle_style_failed_response_is_exception() -> None:
    from ofam_mass_additions.oracle.payloads import parse_oracle_status

    status, msg = parse_oracle_status('{"X_RETURN_STATUS":"E","X_MSG_DATA":"validation failed"}')
    assert status == "E"
    assert "validation failed" in msg


def test_debug_dump_logs_per_row(tmp_path: Path, caplog) -> None:
    """``--debug-dump`` prints each row's parsed fields and rule outcome
    so operators can see why rows were skipped without re-running."""
    import logging

    rows = {
        "MA-100": MassAddition(
            mass_addition_id="MA-100",
            book_type_code="CORP_BOOK",
            region="US",
            posting_status="NEW",
            tag_number="TAG-100",
        )
    }
    runner = MassAdditionCycleRunner(
        oracle_client=MockOracleFaClient(rows=rows),
        run_mode="dry-run",
        pilot_book="CORP_BOOK",
        pilot_region="US",
        debug_dump=True,
    )

    with caplog.at_level(logging.INFO, logger="ofam_mass_additions.runner.cycle"):
        runner.run(output_dir=tmp_path)

    dump_lines = [r for r in caplog.records if r.message.startswith("row=")]
    assert len(dump_lines) == 1
    rendered = dump_lines[0].getMessage()
    assert "row=MA-100" in rendered
    assert "decision=auto_update" in rendered
    assert "TAG-100" in rendered


def test_cmdb_overrides_replace_lookup_values(tmp_path: Path) -> None:
    """Per-asset overrides patch CmdbAsset fields so a row whose CMDB
    return is missing/wrong-typed fields can still produce an
    auto_update payload (used to ship demo cases)."""
    rows = {
        "MA-200": MassAddition(
            mass_addition_id="MA-200",
            book_type_code="CORP_BOOK",
            region="US",
            posting_status="NEW",
            tag_number="C42105000676",
            cost=14877.81,
            received_quantity=1,
        )
    }

    def empty_lookup(_):
        return None

    runner = MassAdditionCycleRunner(
        oracle_client=MockOracleFaClient(rows=rows),
        run_mode="dry-run",
        pilot_book="CORP_BOOK",
        pilot_region="US",
        cmdb_lookup=empty_lookup,
        cmdb_overrides={
            "C42105000676": {
                "location_id": 300000004974106,
                "expense_ccid": 682610,
                "employee_id": None,
            }
        },
    )

    result = runner.run(output_dir=tmp_path)
    assert result["auto_update"] == 1
    assert result["exception"] == 0


def test_cmdb_overrides_keyed_by_mass_addition_id_when_no_tag(tmp_path: Path) -> None:
    """Fall back to mass_addition_id when the row has no tag_number."""
    rows = {
        "300000023592395": MassAddition(
            mass_addition_id="300000023592395",
            book_type_code="CORP_BOOK",
            region="US",
            posting_status="NEW",
            tag_number=None,
            cost=27266.0,
            received_quantity=1,
        )
    }
    runner = MassAdditionCycleRunner(
        oracle_client=MockOracleFaClient(rows=rows),
        run_mode="dry-run",
        pilot_book="CORP_BOOK",
        pilot_region="US",
        cmdb_lookup=lambda _: None,
        cmdb_overrides={
            "300000023592395": {
                "location_id": 999, "expense_ccid": 1, "employee_id": None,
            }
        },
    )
    result = runner.run(output_dir=tmp_path)
    assert result["auto_update"] == 1


def test_debug_dump_off_by_default(tmp_path: Path, caplog) -> None:
    import logging

    rows = {
        "MA-100": MassAddition(
            mass_addition_id="MA-100",
            book_type_code="CORP_BOOK",
            region="US",
            posting_status="NEW",
            tag_number="TAG-100",
        )
    }
    runner = MassAdditionCycleRunner(
        oracle_client=MockOracleFaClient(rows=rows),
        run_mode="dry-run",
        pilot_book="CORP_BOOK",
        pilot_region="US",
    )

    with caplog.at_level(logging.INFO, logger="ofam_mass_additions.runner.cycle"):
        runner.run(output_dir=tmp_path)

    assert not [r for r in caplog.records if r.message.startswith("row=")]
