# Mass Additions Enrichment Rulebook

## Pilot scope
Book: CORP_BOOK
Region: US
Queue/status: NEW
Sample size: 50-200/day

## Mass update cycle this code owns
1. At 5 AM, list Mass Additions where `POSTING_STATUS = NEW` for the pilot book.
2. For each row, call `getMassAddition` to retrieve the full record.
3. Lookup CMDB using first-hit precedence: tag -> serial -> PO -> invoice.
4. Validate and classify:
   - CMDB not found
   - Inactive CCID
   - Missing location
   - Missing CCID
5. Build `updateMassAddition` payload using uppercase Oracle parameters and POST queue/status.
6. In dry-run, write proposed payloads + exceptions + audit only.
7. In live mode, call `updateMassAddition`; parse `X_RETURN_STATUS` and `X_MSG_DATA`.

## What this automation does not own
- AP invoice creation and ship-to defaults.
- Cross-book transfer orchestration (addition/retirement patterns remain outside this cycle).
- Oracle capitalization itself (`Post Mass Additions` scheduled process).

## Trust gradient
- Start with `RUN_MODE=dry-run` for several weeks.
- Review proposed updates against Leanne/Robert decisions.
- Enable `RUN_MODE=live` for one book only after UAT confidence is high.
- Keep exception queue as the human workbench.
