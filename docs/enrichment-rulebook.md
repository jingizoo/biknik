# Mass Additions Enrichment Rulebook

## Pilot scope
Book: CORP_BOOK
Region: US
Queue/status: NEW, ERROR
Sample size: 50-100

## Lookup keys, in order
1. Asset tag
2. Serial number
3. PO number
4. Invoice number
5. Project/reference field

## Fields to enrich
- Location ID
- Depreciation expense CCID
- Employee ID
- Category ID, only where allowed
- Queue/status, only after validation

## Validation rules
- Book must match pilot scope.
- Category must be valid for book.
- Location must resolve to a valid Oracle location ID.
- Expense CCID must be valid and active.
- Employee must be active where required.
- Do not move to POST unless all required validations pass.

## Exception reasons
- CMDB not found
- Multiple CMDB matches
- Missing location
- Missing CCID
- Invalid category/book
- Oracle validation failed
