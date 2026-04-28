from pathlib import Path

from ofam_mass_additions.cmdb import mock_lookup
from ofam_mass_additions.models import CmdbAsset
from ofam_mass_additions.runner.dry_run import run_dry_run


CSV_TEXT = """mass_addition_id,book_type_code,region,tag_number,serial_number,po_number,invoice_number,expected_action
MA-100,CORP_BOOK,US,TAG-100,SN-100,PO-100,INV-100,auto_update
MA-200,CORP_BOOK,US,TAG-404,SN-404,PO-404,INV-404,exception
MA-300,OTHER_BOOK,US,TAG-300,SN-300,PO-300,INV-300,skip
MA-400,CORP_BOOK,US,TAG-CCID,SN-CCID,PO-CCID,INV-CCID,exception
"""


def test_dry_run_generates_outputs(tmp_path: Path) -> None:
    input_csv = tmp_path / "pilot.csv"
    output_dir = tmp_path / "output"
    input_csv.write_text(CSV_TEXT, encoding="utf-8")

    mock_lookup.MOCK_CMDB["TAG-CCID"] = CmdbAsset(
        source_key="tag_number",
        source_value="TAG-CCID",
        location_id=777,
        expense_ccid=None,
        employee_id=42,
    )

    result = run_dry_run(input_csv=input_csv, output_dir=output_dir)

    assert result == {"auto_update": 1, "exception": 2, "total": 4}
    assert (output_dir / "proposed_updates.csv").exists()
    assert (output_dir / "exceptions.csv").exists()
    assert (output_dir / "audit.jsonl").exists()

    exception_text = (output_dir / "exceptions.csv").read_text(encoding="utf-8")
    assert "CMDB not found" in exception_text
    assert "Missing CCID" in exception_text


def test_oracle_style_failed_response_is_exception() -> None:
    from ofam_mass_additions.oracle.payloads import parse_oracle_status

    status, msg = parse_oracle_status('{"X_RETURN_STATUS":"E","X_MSG_DATA":"validation failed"}')
    assert status == "E"
    assert "validation failed" in msg
