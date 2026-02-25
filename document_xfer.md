# OFAM Asset Transfer Automation (CMDB + Spreadsheet → Oracle Fusion) via CDX Batch

## Purpose

Automate Fixed Assets (FA) transfers in Oracle Fusion when:

1. **CMDB indicates an asset transfer** (system-driven, event/polling), and/or the transfer is **initiated by users in CMDB**, and
2. **A user initiates a transfer sync** using a **minimal spreadsheet** uploaded to a bucket (user-driven).

Design intent:

* Enable **more frequent automation** only when CMDB enrichment + validations + exception handling are in place.
* Standardize all Oracle FA actions through **ERP Integration REST Service** (**OperationName + ParameterList**, POST, parse response).

---

## What changed (retrofit updates from latest alignment)

### 1) Data completeness is the main risk

* Working assumption: **most attributes/DFFs copy from source → target** in transfer, but this must be proven via a **full-field transfer test** and codified controls (see **Testing & Controls**).

### 2) Asset number reuse in the target book is not supported

* Expectation: **cross-book transfer creates a new asset number in the target book**.
* Therefore, the design must **store and report traceability** across books (source ↔ target linkage), not rely on “same asset number”.

### 3) Tag behavior must be treated as two separate fields

* Oracle **header “Tag Number”** has **uniqueness behavior** (reuse can error).
* “Asset Tag Number” as a **DFF (CMDB tag)** is a different field with different governance expectations.
* The automation must implement deterministic **underscore incrementing** at least for the **Oracle header Tag Number**.

### 4) Reporting must classify transfers vs AP additions

* Even if mechanics look like “add + retire” behind the scenes, reporting must clearly distinguish:

  * **True additions** (from AP)
  * **Cross-book transfers**
  * **In-book transfers**
* The job must write this classification into outputs/audit for downstream roll-forwards and controls.

---

## Scope

### In scope

* **CDX Batch job** (“assetxferpipeline”) that:

  * Pulls transfer candidates from **CMDB API** (polling) **or** reads a user file from a **bucket**.
  * Enriches each request using **CMDB + Oracle FA inquiry APIs**.
  * Executes transfers using **Oracle ERP Integrations REST**.
  * Writes **audit + outputs + exceptions**, including **traceability** and **transfer classification**.

### Out of scope (for now)

* Automatic choice of **API vs FBDI** (design allows it later; implementation here is **API-only**).
* Full “web UI workbench” (we still design interfaces for exceptions + audit, but UI can come later).

---

## Primary user problem to solve

* Transfers are currently manual and often done at month-end.
* OFAM goal: automate enrichment + validation so transfers can run more frequently **without expanding manual workload**.

---

## Key design principles

1. **Single orchestration engine:** CDX Batch job owns orchestration (CMDB → Fusion).
2. **Fusion API pattern standardization:** Every transaction uses ERP Integrations REST:

   * `OperationName = processTransaction-<HANDLE>`
   * `ParameterList = "{KEY:VALUE,...}"`
3. **Always pre-read source asset state** from Fusion, then **overlay the intended changes**.

   * Ensures “all other parameters from source asset” are preserved unless explicitly changed.
4. **Minimal user input:** users provide only the minimal business key; system derives everything else.
5. **Deterministic tag handling:** treat Oracle header Tag vs CMDB tag DFF as separate; implement underscore incrementing for header Tag uniqueness.
6. **Evidence-first:** job must produce artifacts needed for audit, support, and reporting classification.

---

## Asset identity and minimal input

### Canonical identifier (recommended)

**(Book Type Code, Asset Number)**

Rationale:

* Fusion inquiry supports multiple identifiers, but **Book Type Code is mandatory** for asset inquiry.
* Tag/serial can be ambiguous and may not be reliably unique.

### Minimal spreadsheet columns (API-only design)

| Column           | Required | Notes                                            |
| ---------------- | -------: | ------------------------------------------------ |
| `book_type_code` |      Yes | Source book for resolving the asset.             |
| `asset_number`   |      Yes | Used to find `asset_id` via inquiry.             |
| `effective_date` | Optional | Default = job run date; **YYYY-MM-DD** required. |
| `request_id`     | Optional | Recommended for idempotency + traceability.      |

Interpretation (file mode):

* “**Sync this asset to CMDB’s current assignment now**.”
* If CMDB indicates no delta vs Fusion, job returns **NOOP**.

---

## Oracle Fusion integration standards (must-follow)

### Endpoint style

All FA actions are invoked by POSTing to ERP Integrations REST using the standard payload pattern.

### Required headers

Use baseline REST headers:

* `Content-Type: application/vnd.oracle.adf.resourceitem+json`
* `REST-header-version: 4`
* `ACCEPT: application/json`
* `Authorization: Basic ...`

### Payload correctness rules

* **Parameter names must be UPPERCASE** (wrong ones are ignored).
* Collection parameters (JTF_*_TABLE) require comma-separated lists.
* Dates must be **YYYY-MM-DD**.

### Privileges

Integration user needs:

* Baseline: `FUN_FSCM_REST_SERVICE_ACCESS_INTEGRATION_PRIV`
* Plus handle-specific privilege(s).

---

## Transfer types supported by this design

### A) In-book transfer (typical CMDB-driven move)

Use **`transferAsset`** when asset stays in the same FA book:

* Transfer to new location/expense/custodian.
* Requires rosetta distribution inputs (distribution IDs, units, CCIDs).

### B) Cross-book transfer (asset moves between books)

**Option B1 (Preferred):** Use Fusion’s **cross-book transfer handle** (as tested).

* Still invoked via ERP Integrations REST pattern.

**Critical constraint:** target book **will create a new asset number**; “reuse same asset number” is not supported.

* Design must store: **source asset identifiers + returned target asset identifiers** as the system traceability record.

**Option B2 (Fallback only if cross-book handle not available):** orchestrate:

1. Read source state
2. Create destination asset
3. Retire/adjust source asset
4. Store cross-reference + audit

---

## Tag and DFF handling (retrofit)

### Field definitions (must be treated differently)

1. **Oracle Header Tag Number** (UI header “Tag Number”)

   * Has **uniqueness behavior**; reuse can cause transfer failure.
2. **CMDB Asset Tag (DFF)** (“Asset Tag Number” / CMDB tag)

   * Business expectation: this is the CMDB identity tag; governance differs from Oracle header tag.

### Required automation behavior (minimum)

* Implement **underscore increment** logic for **Oracle Header Tag Number** to avoid uniqueness errors:

  * Candidate = source header tag
  * If collision in target context, try: `_1`, `_2`, `_3`… until unique
  * Persist the final chosen header tag in audit outputs

### Collision risk: humans vs automation

* If users manually skip suffixes or diverge from convention, collisions can reoccur.
* Mitigation: make suffix selection deterministic and machine-driven (no “guessing”).

> Design decision still required (governance): whether to also modify the **source** tag fields during transfer-out to prevent future CMDB updates to the old record. This must be aligned with the authoritative runbook/deck and revised for the new native transfer behavior.

---

## “Copy all other parameters from source asset” rule

### Definition

For any transfer request, the job must:

1. **Query Fusion for the source asset’s full state**, including book and distributions, before changing anything.
2. Build the transaction payload by:

   * Copying all required fields/collections from source
   * Overlaying only the delta driven by CMDB (or explicit request overrides, if introduced later)

### Why

Transfer APIs require distribution-level inputs; safest approach is to derive distribution rosetta tables from Fusion inquiry and modify only intended attributes.

### Source-of-truth per field

| Field group                                  | System of record | How obtained                                                |
| -------------------------------------------- | ---------------- | ----------------------------------------------------------- |
| Asset identity (`asset_id`, `asset_number`)  | Fusion           | `getAssetInformation` using `book_type_code + asset_number` |
| Current distributions (IDs, units, CCIDs)    | Fusion           | `getAssetBookInformation` / inquiry                         |
| Physical assignment (location/custodian/org) | CMDB             | CMDB “current state” API                                    |
| Accounting mappings (segment mapping → CCID) | Mapping layer    | OFAM-owned mapping/validation                               |
| Transfer effective date                      | Request/CMDB     | file value, CMDB effective date, or default job date        |

---

## Data completeness controls (retrofit)

### Required validation: pre/post compare (per transfer)

For transfers (especially cross-book):

* Capture a **pre-state snapshot** (source)
* Capture a **post-state snapshot** (target)
* Run a **field-by-field compare** for:

  * DFFs (including CMDB tag DFF)
  * Serial, model/manufacturer fields where relevant
  * Key accounting/assignment attributes
* Persist comparison result:

  * `COMPARE_STATUS = MATCH | MISMATCH | PARTIAL`
  * `MISMATCH_FIELDS = [...]`

### Operational control reporting

Produce a daily/weekly exception report for:

* Missing header Tag Number where expected
* Missing/blank CMDB tag DFF where required for CMDB sync
* Mismatches between header tag and DFF tag where policy requires alignment
* Transfer records without stored traceability

---

## High-level architecture

```mermaid
flowchart LR
  %% ===== Styles =====
  classDef user fill:#f6f8fa,stroke:#6b7280,stroke-width:1px;
  classDef store fill:#fff7ed,stroke:#9a3412,stroke-width:1px;
  classDef svc fill:#eff6ff,stroke:#1d4ed8,stroke-width:1px;
  classDef proc fill:#ecfdf5,stroke:#047857,stroke-width:1px;
  classDef erp fill:#fdf2f8,stroke:#9d174d,stroke-width:1px;

  %% ===== Users =====
  subgraph U[Users]
    X[Excel Macro\nUploads CSV]:::user
    UI[CDX Batch UI\nRun + Parameters]:::user
    CMDBUI[CMDB UI\nUser-initiated transfer]:::user
  end

  %% ===== Storage =====
  subgraph S[Bucket Storage]
    IN[/requests/*.csv/]:::store
    OUT[/results/*/]:::store
    AUD[/audit/*/]:::store
    EXC[/exceptions/*/]:::store
  end

  %% ===== CMDB =====
  subgraph C[CMDB]
    EVT[Transfer Events API]:::svc
    ASSET[Asset Details API]:::svc
    ACK[Update Status API\n(optional)]:::svc
  end

  %% ===== CDX Batch =====
  subgraph P[CDX Batch - assetxferpipeline]
    JOB[Orchestrator]:::proc
    ENR[Enrichment\nCMDB + Fusion Inquiry]:::proc
    MAP[Mapping + Validation]:::proc
    TAG[Tag Resolver\n(underscore increment)]:::proc
    TRX[Transfer Executor\n(ERP Integrations REST)]:::proc
    CMP[Post-Transfer Compare\n(field-by-field)]:::proc
    STORE[Audit + Output Writer]:::proc
  end

  %% ===== Fusion =====
  subgraph F[Oracle Fusion]
    ERP[ERP Integrations REST]:::erp
    INQ[Inquiry Handles\ngetAssetInformation/getAssetBookInformation]:::erp
    HND[Transaction Handles\ntransferAsset / cross-book transfer]:::erp
  end

  %% ===== Flows =====
  X -->|upload| IN
  UI -->|run job\nmode=file| JOB
  CMDBUI -->|creates/updates| EVT

  JOB -->|poll| EVT
  JOB -->|lookup| ASSET

  JOB --> ENR
  ENR --> INQ --> ERP

  ENR --> MAP --> TAG --> TRX --> HND --> ERP

  TRX --> CMP --> STORE
  STORE --> OUT
  STORE --> AUD
  STORE --> EXC
  STORE --> ACK
```

---

## End-to-end processing flows

### Flow 1 — CMDB-initiated transfers (polling)

1. Scheduler triggers CDX Batch (e.g., hourly).
2. Job calls **CMDB Transfer Events API** with `since_timestamp` watermark.
3. For each event:

   1. Resolve asset identity (book + asset number mapping).
   2. Fusion inquiry:

      * `getAssetInformation` (resolve `asset_id`)
      * `getAssetBookInformation` (distributions/book state)
   3. CMDB current assignment lookup.
   4. Mapping + validation (derive CCIDs, validate ledger/book compatibility).
   5. Determine transfer type:

      * Same book → `transferAsset`
      * Different book → cross-book transfer handle
   6. **Tag resolver**:

      * Ensure Oracle header Tag uniqueness (underscore logic).
   7. Execute transfer (ERP Integrations REST).
   8. **Post-transfer compare** (data completeness).
   9. Persist outputs + audit; optionally ACK CMDB.

### Flow 2 — User-initiated (spreadsheet → bucket → CDX Batch)

1. User fills minimal CSV (`book_type_code`, `asset_number`, optional `effective_date`, `request_id`).
2. Macro uploads to bucket `/requests/`.
3. User runs Batch job with `mode=file`, `requests_uri=...`, `execute=false` first.
4. Job processes each row using the same enrichment/transfer steps as Flow 1.

---

## Processing sequence diagram (per asset)

```mermaid
sequenceDiagram
  autonumber
  participant R as Run Trigger (Scheduler/User)
  participant J as CDX Batch Job
  participant M as CMDB
  participant F as Fusion ERP Integrations REST

  R->>J: Start job (mode=cmdb_poll|file)\nexecute=true|false
  alt cmdb_poll
    J->>M: GET transfer-events?since=watermark
    M-->>J: events[]
  else file
    J->>J: Read CSV from requests_uri
  end

  loop each request
    J->>M: GET asset details (current assignment)
    M-->>J: target assignment fields

    J->>F: POST getAssetInformation\n(book_type_code + asset_number)
    F-->>J: asset_id + source summary

    J->>F: POST getAssetBookInformation\n(asset_id + book_type_code)
    F-->>J: source distributions + required attributes

    J->>J: Build payload\n(copy source; overlay CMDB delta)

    J->>J: Resolve header Tag uniqueness\n(underscore increment if needed)

    alt same book
      J->>F: POST transferAsset
    else cross-book
      J->>F: POST cross-book transfer handle
    end
    F-->>J: X_RETURN_STATUS + IDs + messages

    J->>F: POST getAssetInformation\n(target identifiers) for post-state
    F-->>J: target state (incl DFFs)

    J->>J: Field-by-field compare\nrecord mismatches

    J->>J: Persist audit + results\nclassify as TRANSFER (in-book/cross-book)
  end

  J-->>R: Job summary + output URIs
```

---

## CDX Batch job functional specification

### Job name / module

* Module path: `pte/accounting/external-services/acctgateway/assetxferpipeline`
* Entrypoint wrapper: `bin/<job_wrapper>` → `src/main/python/batch_entrypoint.py`

### Run modes

| Mode        | Description                      | Primary input                       |
| ----------- | -------------------------------- | ----------------------------------- |
| `cmdb_poll` | Poll CMDB events since watermark | `cmdb_since_ts` or stored watermark |
| `file`      | Read user file from bucket       | `requests_uri`                      |

### Required CDX Batch input variables (proposed)

| Variable        | Required | Example                    | Notes             |
| --------------- | -------: | -------------------------- | ----------------- |
| `mode`          |      Yes | `cmdb_poll` / `file`       | Input source      |
| `requests_uri`  |     Cond | `gs://.../requests/x.csv`  | File mode         |
| `execute`       |      Yes | `false` / `true`           | Dry-run supported |
| `out_dir_uri`   |      Yes | `gs://.../results/run123/` | Outputs           |
| `cmdb_since_ts` |     Cond | `2026-02-01T00:00:00Z`     | Polling mode      |
| `env`           |      Yes | `dev` / `test` / `prod`    | Endpoints/secrets |

### Outputs (per run)

* `summary.json` (counts: total, success, fail, noop, mismatch)
* `results.csv` (per request):

  * `request_id`
  * `source_book_type_code`, `source_asset_number`, `source_asset_id`
  * `target_book_type_code` (if cross-book)
  * `target_asset_number` (new number), `target_asset_id`
  * `transfer_type` = `IN_BOOK_TRANSFER | CROSS_BOOK_TRANSFER`
  * `compare_status` + `mismatch_fields`
  * `x_event_id`, `x_transaction_header_id`
  * `status`, `message`
* `audit/` folder (request/response payloads; masked secrets)
* `exceptions/` folder (payload + reason + retry guidance)

---

## Technical design: internal components

### 1) CMDB client

Responsibilities:

* Fetch transfer events incrementally.
* Fetch current state for asset assignment.
* Optional: update status back to CMDB.

### 2) Fusion client (ERP Integrations REST)

Responsibilities:

* Build and post payloads (OperationName + ParameterList).
* Enforce headers and payload rules (uppercase params, date format).

Core operations:

* `getAssetInformation`
* `getAssetBookInformation`
* `transferAsset`
* cross-book transfer handle (preferred)

### 3) Transfer builder (copy source + overlay delta)

Inputs:

* Fusion source state (book + distributions)
* CMDB desired target assignment
* Mapping outputs (target CCIDs, etc.)

Output:

* Fully formed ParameterList (including distribution rosetta tables).

### 4) Mapping + validation layer

Responsibilities:

* Convert CMDB fields → Fusion identifiers.
* Validate effective date, required fields, ledger/book compatibility.
* Fail fast with actionable error messaging.

### 5) Tag resolver (retrofit)

Responsibilities:

* Identify the relevant **Oracle header Tag Number** used by the transfer payload.
* Ensure uniqueness using underscore increment logic.
* Persist final tag decision in audit.

### 6) Post-transfer comparator (retrofit)

Responsibilities:

* Fetch target asset state after transfer.
* Compare source vs target for:

  * DFFs
  * key descriptive fields (serial/model/manufacturer where applicable)
  * critical assignment fields
* Persist compare results for controls.

### 7) Audit + exception writer

Responsibilities:

* Persist evidence trail for every request.
* Ensure failed items are routed with retry guidance, not buried in logs.

---

## Error handling and idempotency

### Idempotency strategy

Prefer:

* CMDB `event_id` as idempotency key.
  Fallback:
* Hash `(book_type_code, asset_number, effective_date, target_assignment_signature)`.

### Failure categories

1. **Data issues:** missing CMDB fields, mapping failures → reject.
2. **Fusion validation failures:** invalid CCID, closed period, tag uniqueness conflicts.
3. **Transport/security:** auth failure, timeout, transient errors.

### Retry rules

* Transient transport: retry with backoff.
* Business validation: do not auto-retry; route to exceptions.

### Known constraint to consider

* Transfer example guidance notes a limitation: **no support for asset books with alternate ledger currencies** (must be validated for impacted books).

---

## Operating the solution with CDX Batch

### User run (spreadsheet path)

1. Create `xfer_requests.csv` with minimal columns.
2. Upload via Excel macro to `/requests/`.
3. Run job in CDX Batch UI:

   * `mode=file`
   * `requests_uri=<bucket path>`
   * `execute=false` (dry-run)
4. Review outputs in `out_dir_uri`.
5. Re-run with `execute=true`.

### Operations run (CMDB polling path)

1. Schedule CDX Batch:

   * `mode=cmdb_poll`
   * watermark-driven `cmdb_since_ts`
   * `execute=true`
2. Monitor:

   * run logs
   * `summary.json`
   * `exceptions/`

---

## CDX Batch deployment runbook (developer-facing, as implemented)

1. Ensure repo layout:

   * `src/main/python/` contains entrypoint(s)
   * `src/main/batch/<env>/` contains job YAML
2. Build python packaging:

   * `gradlew :...:assetxferpipeline:pyshadow`
3. Generate Dockerfile artifacts:

   * `gradlew dockerfile`
4. Build image via generated script:

   * `./build/Dockerfile.jdk.sh <tag>`
5. Register Docker image in CDX Batch UI (Docker images → Add).
6. Publish job YAML:

   * `batchCheck`, `batchDiff`, `batchSync`
7. Run in CDX Batch UI with required input variables.

---

## Testing & acceptance criteria (retrofit)

### Mandatory tests before productionization

1. **Full-field DFF test (data completeness)**

   * Create one test asset with **all DFFs populated** + representative key fields.
   * Transfer (API).
   * Field-by-field compare source vs target; document mismatches.

2. **Tag uniqueness tests (UI + API)**

   * Create asset with header Tag populated + DFF populated.
   * Transfer; confirm:

     * collision produces error if not modified
     * `_1/_2/...` resolves
   * Verify API implements same logic.

3. **Intercompany transfer scenario**

   * Execute scenario-based test using provided intercompany document; confirm success and reporting traceability.

4. **Realistic asset compare**

   * Transfer an asset with “more DFFs and other stuff”.
   * Confirm compare report is clean or mismatches are explainable and controlled.

### Acceptance criteria (what Simon/Jonathan typically need)

* Transfers can run from CMDB events and from minimal spreadsheet input.
* Evidence trail exists: request → enrichment → payload → response → traceability → compare.
* Clear exceptions and retry guidance.
* Reporting classification fields are produced to distinguish transfers vs AP additions.

---

## Appendix A — Fusion payload template (standardized)

```json
{
  "OperationName": "processTransaction-<HANDLE>",
  "ParameterList": "{P_BOOK_TYPE_CODE:<BOOK>,P_ASSET_ID:<ID>,...}"
}
```

---

## Appendix B — Why this satisfies “Simon and Jonathan” (updated)

* **Operational clarity**

  * Minimal input, dry-run, deterministic tag handling, exception outputs, and evidence trail.
* **Technical completeness**

  * Standardized ERP Integrations REST invocation and strict payload rules (uppercase params, comma-separated collections, standard date formats).
* **Control readiness**

  * Explicit data completeness compare + reporting classification to support roll-forwards and reconciliations.

---

## Open decisions to finalize (explicit)

1. **Authoritative tag governance**

   * Confirm which field is authoritative for CMDB identity and whether source-side tag mutation is required post-transfer.
2. **Reporting integration**

   * Confirm where transfer classification fields land (FDA dashboard, OAC/OIC later, etc.).
3. **Controls placement**

   * Confirm where daily/weekly data quality checks will be surfaced and owned.