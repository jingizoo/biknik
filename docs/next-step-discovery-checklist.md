# Mass Additions Discovery Checklist (for Robert/Leanne walkthrough)

## Process walkthrough (UI + API alignment)
- Demonstrate end-to-end Mass Additions flow in DEV2 (invoice -> mass additions -> create capitalization).
- Capture which fields are edited manually in UI before capitalization.
- Identify where scheduled processes are required versus direct screen actions.

## Data quality questions
- For each pilot exception, document whether the source issue is OTBI/AP, CMDB, or user override.
- Confirm policy for location authority when ship-to differs from delivery and CMDB.
- Confirm handling for invoice/receipt mismatches before posting to FA.

## API strategy checks
- Confirm that cross-book transfer remains addition + retirement for current REST support.
- Confirm whether any new REST support exists beyond current documented handles.
- Verify controls for enabling live `updateMassAddition` only after UAT sign-off.
