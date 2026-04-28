# OFAM Mass Additions MVP

Purpose:
Dry-run and then pilot automation for Oracle Fusion Fixed Assets Mass Additions enrichment.

Initial flow:
1. Read pilot Mass Addition IDs.
2. Call getMassAddition.
3. Look up CMDB enrichment data.
4. Apply enrichment rules.
5. Produce proposed updateMassAddition payload.
6. Write audit and exception outputs.
7. Only run live updates when RUN_MODE=live and UAT is approved.
