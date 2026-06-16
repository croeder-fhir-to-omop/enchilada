"""Integration tests against the real enchilada.db built from CONCEPT.csv.

These tests require enchilada.db to be present and populated (including
concept_relationship). They are skipped automatically if the DB is absent.

Run with:
    pytest tests/test_integration.py -v

Or together with unit tests:
    pytest -v
"""
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from enchilada.main import app
from enchilada.translate import translate_r4, translate_r5

# ---------------------------------------------------------------------------
# Paths and skip condition
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent.parent / "enchilada.db"

SNOMED  = "http://snomed.info/sct"
ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"
OMOP    = "https://athena.ohdsi.org"

skip_if_no_db = pytest.mark.skipif(
    not DB_PATH.exists(),
    reason=f"enchilada.db not found at {DB_PATH} — run the server once to build it",
)

skip_if_no_cr = pytest.mark.skipif(
    not DB_PATH.exists() or (
        DB_PATH.exists() and
        sqlite3.connect(str(DB_PATH)).execute(
            "SELECT COUNT(*) FROM concept_relationship"
        ).fetchone()[0] == 0
    ),
    reason="concept_relationship not loaded in enchilada.db — Maps-to path untestable",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_conn():
    """Open the real enchilada.db for direct translate() tests."""
    if not DB_PATH.exists():
        pytest.skip(f"enchilada.db not found at {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def real_client(real_conn):
    """TestClient wired to the real enchilada.db."""
    @asynccontextmanager
    async def real_lifespan(a):
        a.state.conn = real_conn
        yield

    app.router.lifespan_context = real_lifespan
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# SNOMED → OMOP  (step 1: direct standard concept lookup)
# ---------------------------------------------------------------------------

@skip_if_no_db
def test_WHEN_snomed_diabetes_SHOULD_return_real_omop_id(real_conn):
    """SNOMED 73211009 (Diabetes mellitus) → OMOP concept_id 201820."""
    result = translate_r4(real_conn, SNOMED, "73211009", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    assert result["parameter"][1]["part"][1]["valueCoding"]["code"] == "201820"


@skip_if_no_db
def test_WHEN_snomed_hypertension_SHOULD_return_real_omop_id(real_conn):
    """SNOMED 38341003 (Hypertensive disorder) → OMOP concept_id 316866."""
    result = translate_r4(real_conn, SNOMED, "38341003", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    assert result["parameter"][1]["part"][1]["valueCoding"]["code"] == "316866"


@skip_if_no_db
def test_WHEN_snomed_r5_diabetes_SHOULD_use_relationship_field(real_conn):
    """R5 response for SNOMED lookup uses 'relationship', not 'equivalence'."""
    result = translate_r5(real_conn, SNOMED, "73211009", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    parts = {p["name"]: p for p in result["parameter"][1]["part"]}
    assert "relationship" in parts
    assert "equivalence" not in parts
    assert parts["concept"]["valueCoding"]["code"] == "201820"


# ---------------------------------------------------------------------------
# ICD-10-CM → OMOP  (step 2: Maps-to relationship path)
#
# ICD-10-CM codes are never standard_concept='S' in OMOP; translation requires
# the CONCEPT_RELATIONSHIP table to provide the 'Maps to' path.
# ---------------------------------------------------------------------------

@skip_if_no_cr
def test_WHEN_icd10cm_E11_9_SHOULD_map_to_snomed_standard_concept(real_conn):
    """ICD-10-CM E11.9 (T2DM w/o complications) → OMOP 4193704 via Maps-to.

    4193704 = SNOMED 313436004 'Type II diabetes mellitus without complication'.
    """
    result = translate_r4(real_conn, ICD10CM, "E11.9", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    assert result["parameter"][1]["part"][1]["valueCoding"]["code"] == "4193704"


@skip_if_no_cr
def test_WHEN_icd10cm_unknown_code_SHOULD_return_false(real_conn):
    result = translate_r4(real_conn, ICD10CM, "Z99.999", OMOP)
    assert result["parameter"][0]["valueBoolean"] is False


@skip_if_no_cr
def test_WHEN_icd10cm_r5_E11_9_SHOULD_translate_with_relationship_field(real_conn):
    """R5 ICD-10-CM lookup uses 'relationship' response field."""
    result = translate_r5(real_conn, ICD10CM, "E11.9", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    parts = {p["name"]: p for p in result["parameter"][1]["part"]}
    assert "relationship" in parts
    assert "equivalence" not in parts
    assert parts["concept"]["valueCoding"]["code"] == "4193704"


# ---------------------------------------------------------------------------
# HTTP endpoint integration — real DB via TestClient
# ---------------------------------------------------------------------------

@skip_if_no_db
def test_WHEN_r4_http_post_snomed_SHOULD_translate(real_client):
    resp = real_client.post(
        "/r4/ConceptMap/$translate",
        json={
            "resourceType": "Parameters",
            "parameter": [
                {"name": "system",       "valueUri":  SNOMED},
                {"name": "code",         "valueCode": "73211009"},
                {"name": "targetsystem", "valueUri":  OMOP},
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["parameter"][0]["valueBoolean"] is True
    parts = {p["name"]: p for p in data["parameter"][1]["part"]}
    assert parts["equivalence"]["valueCode"] == "equivalent"
    assert parts["concept"]["valueCoding"]["code"] == "201820"


@skip_if_no_cr
def test_WHEN_r4_http_post_icd10cm_SHOULD_translate(real_client):
    resp = real_client.post(
        "/r4/ConceptMap/$translate",
        json={
            "resourceType": "Parameters",
            "parameter": [
                {"name": "system",       "valueUri":  ICD10CM},
                {"name": "code",         "valueCode": "E11.9"},
                {"name": "targetsystem", "valueUri":  OMOP},
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["parameter"][0]["valueBoolean"] is True
    parts = {p["name"]: p for p in data["parameter"][1]["part"]}
    assert parts["equivalence"]["valueCode"] == "equivalent"
    assert parts["concept"]["valueCoding"]["code"] == "4193704"


@skip_if_no_cr
def test_WHEN_r5_http_post_icd10cm_sourceCoding_SHOULD_translate(real_client):
    resp = real_client.post(
        "/r5/ConceptMap/$translate",
        json={
            "resourceType": "Parameters",
            "parameter": [
                {"name": "sourceCoding", "valueCoding": {"system": ICD10CM, "code": "E11.9"}},
                {"name": "targetSystem", "valueUri":    OMOP},
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["parameter"][0]["valueBoolean"] is True
    parts = {p["name"]: p for p in data["parameter"][1]["part"]}
    assert "relationship" in parts
    assert "equivalence" not in parts
    assert parts["concept"]["valueCoding"]["code"] == "4193704"
