# enchilada

A local FHIR R4 terminology server implementing a subset of the standard FHIR terminology
API, backed by an on-disk copy of the OMOP vocabularies.

## Purpose

Matchbox calls an external FHIR terminology server to resolve source codes (SNOMED, ICD,
RxNorm, etc.) to OMOP concept IDs during `$transform` operations. This project provides a
local alternative for offline use, deterministic results, and no external network dependency.

## API surface

The FHIR R4 terminology API defines six operations across three resource types. This project
implements the subset relevant to concept translation:

| Resource | Interactions | Operations |
|---|---|---|
| `ConceptMap` | read, search-type | **$translate** ← primary target |
| `CodeSystem` | read, search-type | lookup, validate-code, subsumes |
| `ValueSet` | read, search-type | expand, validate-code |

Not implemented: `ConceptMap/$closure` (transitive closure table for search-time subsumption).
All implemented operations are standard FHIR R4 — no proprietary extensions.

Initial implementation focuses on `ConceptMap/$translate`. The others can be added
incrementally as needed.

### ConceptMap/$translate

```
POST /r4/ConceptMap/$translate
```

Parameters:
- `system` — source vocabulary URI (e.g. `http://snomed.info/sct`)
- `code` — source code (e.g. `38341003`)
- `targetsystem` — target vocabulary URI (e.g. `http://ohdsi.org/omop`)

**Match found** — HTTP 200:
```json
{
  "resourceType": "Parameters",
  "parameter": [
    { "name": "result", "valueBoolean": true },
    { "name": "match", "part": [
      { "name": "equivalence", "valueCode": "equivalent" },
      { "name": "concept", "valueCoding": {
        "system": "http://ohdsi.org/omop",
        "code": "316866"
      }}
    ]}
  ]
}
```

**No match found** — HTTP 200 (per FHIR spec; `$translate` is an operation returning a
result, not a resource lookup, so 404 does not apply):
```json
{
  "resourceType": "Parameters",
  "parameter": [
    { "name": "result", "valueBoolean": false },
    { "name": "message", "valueString": "No mapping found for SNOMED#38341003" }
  ]
}
```

The `message` parameter is defined in the FHIR spec as optional on both match and no-match;
here it is always populated on no-match to aid debugging.

## Data source

### CONCEPT.csv (available locally)

`/Users/croeder/git/CCDA/tislab-clad/CCDA_OMOP_Private/CONCEPT.csv`

- 7.4M rows, 965 MB
- Columns: `concept_id`, `concept_name`, `domain_id`, `vocabulary_id`, `concept_class_id`,
  `standard_concept`, `concept_code`, `valid_start_date`, `valid_end_date`, `invalid_reason`
- Standard concepts marked `standard_concept = 'S'`
- Confirmed: SNOMED 38341003 (hypertensive disorder) → concept_id 316866 present

### CONCEPT_RELATIONSHIP.csv (needs Athena download)

Not present locally. Required to translate non-standard source codes via the `Maps to`
relationship. Download from [Athena](https://athena.ohdsi.org) — select vocabularies
SNOMED, ICD10CM, ICD9CM, RxNorm, LOINC at minimum.

## Technology

- **Python + FastAPI** — minimal REST layer; one route handler per operation
- **SQLite** — point-lookup database loaded from CONCEPT.csv at startup; indexed on
  `(concept_code, vocabulary_id)`. SQLite is purpose-built for OLTP point lookups and
  a better fit than DuckDB (which is optimized for analytical scans, not single-row
  lookups over 7M rows). The CSV is loaded into a SQLite file once on first run;
  subsequent server starts reuse the existing file.
- **Reference**: `/Users/croeder/git/omop_on_duckdb/` — existing OMOP CSV loading patterns

## Configuration

`config.yaml`:
```yaml
server:
  host: 0.0.0.0
  port: 8081

data:
  concept_csv: /Users/croeder/git/CCDA/tislab-clad/CCDA_OMOP_Private/CONCEPT.csv
  concept_relationship_csv: null   # set path when available
  sqlite_db: ./enchilada.db        # created on first run, reused thereafter
```

## Translation logic

For `ConceptMap/$translate` given `(system, code, targetsystem)`:

1. Map FHIR vocabulary URI → OMOP `vocabulary_id`:

| FHIR URI | OMOP vocabulary_id |
|---|---|
| `http://snomed.info/sct` | `SNOMED` |
| `http://hl7.org/fhir/sid/icd-10-cm` | `ICD10CM` |
| `http://hl7.org/fhir/sid/icd-9-cm` | `ICD9CM` |
| `http://www.nlm.nih.gov/research/umls/rxnorm` | `RxNorm` |
| `http://loinc.org` | `LOINC` |

2. Query SQLite `CONCEPT` for `concept_code = code` AND `vocabulary_id = <mapped>` AND
   `standard_concept = 'S'` → return `concept_id` directly.

3. If not found as a standard concept, join through `CONCEPT_RELATIONSHIP` on
   `relationship_id = 'Maps to'` to find the standard concept (requires Athena download).

4. If still not found, return `result=false` with a descriptive `message`.

## Test cases

Two known-good codes confirmed against the local CONCEPT.csv:

| Input system | code | Expected concept_id |
|---|---|---|
| `http://snomed.info/sct` | `38341003` (hypertensive disorder) | `316866` |
| `http://snomed.info/sct` | `386661006` (fever) | `437663` |

## Matchbox integration

```yaml
matchbox:
  fhir:
    context:
      txServer: http://localhost:8081/r4
      translateMode: server
```

## Out of scope

- Full OMOP vocabulary coverage beyond what's in the Athena download
- Authentication
- Write operations
