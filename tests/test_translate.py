import sqlite3
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from enchilada.main import app
from enchilada.translate import translate_r4, translate_r5


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """In-memory SQLite DB with the schema and known-good test rows."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE vocabulary (
            vocabulary_id        TEXT NOT NULL PRIMARY KEY,
            vocabulary_name      TEXT NOT NULL,
            vocabulary_reference TEXT
        );
        CREATE TABLE concept (
            concept_id    INTEGER NOT NULL,
            concept_code  TEXT    NOT NULL,
            vocabulary_id TEXT    NOT NULL,
            standard_concept TEXT
        );
        CREATE TABLE concept_relationship (
            concept_id_1    INTEGER NOT NULL,
            concept_id_2    INTEGER NOT NULL,
            relationship_id TEXT    NOT NULL
        );
        CREATE INDEX idx_concept ON concept (concept_code, vocabulary_id);
        CREATE INDEX idx_cr ON concept_relationship (concept_id_1, relationship_id);

        -- standard concepts (step 1 path)
        INSERT INTO concept VALUES (316866, '38341003',  'SNOMED',  'S');
        INSERT INTO concept VALUES (437663, '386661006', 'SNOMED',  'S');

        -- non-standard concept + Maps to relationship (step 2 path)
        INSERT INTO concept VALUES (999001, 'NONSTANDARD_CODE', 'SNOMED',  NULL);
        INSERT INTO concept_relationship VALUES (999001, 316866, 'Maps to');

        -- ICD-10-CM: no standard concepts in OMOP, always needs Maps to
        INSERT INTO concept VALUES (201820, 'E11.9', 'ICD10CM', NULL);
        INSERT INTO concept VALUES (888001, 'E11',   'ICD10CM', NULL);
        INSERT INTO concept_relationship VALUES (201820, 316866, 'Maps to');
        INSERT INTO concept_relationship VALUES (888001, 316866, 'Maps to');
    """)
    yield c
    c.close()


@pytest.fixture
def client(conn):
    """TestClient with lifespan replaced so no config.yaml or CSV needed."""
    @asynccontextmanager
    async def test_lifespan(a):
        a.state.conn = conn
        yield

    app.router.lifespan_context = test_lifespan
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Unit tests — translate() directly
# ---------------------------------------------------------------------------

SNOMED  = "http://snomed.info/sct"
ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"
OMOP    = "http://ohdsi.org/omop"


def test_standard_concept_hypertension(conn):
    result = translate_r4(conn, SNOMED, "38341003", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    concept = result["parameter"][1]["part"][1]["valueCoding"]
    assert concept["code"] == "316866"
    assert concept["system"] == OMOP


def test_standard_concept_fever(conn):
    result = translate_r4(conn, SNOMED, "386661006", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    assert result["parameter"][1]["part"][1]["valueCoding"]["code"] == "437663"


def test_maps_to_relationship(conn):
    result = translate_r4(conn, SNOMED, "NONSTANDARD_CODE", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    assert result["parameter"][1]["part"][1]["valueCoding"]["code"] == "316866"


def test_no_match(conn):
    result = translate_r4(conn, SNOMED, "UNKNOWN_CODE", OMOP)
    assert result["parameter"][0]["valueBoolean"] is False
    assert "UNKNOWN_CODE" in result["parameter"][1]["valueString"]


def test_unknown_system(conn):
    result = translate_r4(conn, "http://example.com/unknown", "12345", OMOP)
    assert result["parameter"][0]["valueBoolean"] is False
    assert "Unknown vocabulary" in result["parameter"][1]["valueString"]


# ---------------------------------------------------------------------------
# HTTP tests — FastAPI endpoint
# ---------------------------------------------------------------------------

def _parameters_body(system: str, code: str, targetsystem: str) -> dict:
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "system",       "valueUri":  system},
            {"name": "code",         "valueCode": code},
            {"name": "targetsystem", "valueUri":  targetsystem},
        ],
    }


def test_http_post_match(client):
    resp = client.post("/r4/ConceptMap/$translate", json=_parameters_body(SNOMED, "38341003", OMOP))
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is True


def test_http_post_no_match(client):
    resp = client.post("/r4/ConceptMap/$translate", json=_parameters_body(SNOMED, "UNKNOWN", OMOP))
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is False


def test_http_get_match(client):
    resp = client.get("/r4/ConceptMap/$translate", params={"system": SNOMED, "code": "38341003", "targetsystem": OMOP})
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is True


def test_http_missing_param(client):
    resp = client.post("/r4/ConceptMap/$translate", json={"resourceType": "Parameters", "parameter": []})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Bare-code tests: system URI passed via 'url' (FML mapUri) instead of 'system'
# ---------------------------------------------------------------------------

def _bare_code_body(url: str, code: str, targetsystem: str) -> dict:
    """Parameters body mimicking FML translate(bare_code, 'systemUri', 'code').
    FML puts the mapUri into the 'url' parameter; no 'system' field is present."""
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "url",          "valueUri":  url},
            {"name": "code",         "valueCode": code},
            {"name": "targetsystem", "valueUri":  targetsystem},
        ],
    }


def test_WHEN_bare_code_with_known_url_as_system_SHOULD_translate(client):
    """url parameter used as system fallback when system is absent."""
    resp = client.post(
        "/r4/ConceptMap/$translate",
        json=_bare_code_body("http://snomed.info/sct", "38341003", OMOP),
    )
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is True
    assert resp.json()["parameter"][1]["part"][1]["valueCoding"]["code"] == "316866"


def test_WHEN_bare_code_without_system_or_url_SHOULD_return_400(client):
    """No system and no url: enchilada cannot determine vocabulary."""
    resp = client.post("/r4/ConceptMap/$translate", json={
        "resourceType": "Parameters",
        "parameter": [{"name": "code", "valueCode": "male"}],
    })
    assert resp.status_code == 400


def test_WHEN_bare_code_with_unknown_url_SHOULD_return_no_match(client):
    """url is present but not a known FHIR system URI: returns result=false."""
    resp = client.post(
        "/r4/ConceptMap/$translate",
        json=_bare_code_body("http://example.com/unknown-system", "whatever", OMOP),
    )
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is False


def test_metadata_capability_statement(client):
    resp = client.get("/r4/metadata")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resourceType"] == "CapabilityStatement"
    assert body["fhirVersion"] == "4.0.1"


def test_metadata_terminology_capabilities(client):
    resp = client.get("/r4/metadata", params={"mode": "terminology"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["resourceType"] == "TerminologyCapabilities"
    uris = {cs["uri"] for cs in body["codeSystem"]}
    assert "http://snomed.info/sct" in uris
    assert "http://loinc.org" in uris


# ---------------------------------------------------------------------------
# R4 spec: coding/valueCoding alternative to flat system+code
# ---------------------------------------------------------------------------

def _r4_coding_body(system: str, code: str, targetsystem: str) -> dict:
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "coding",       "valueCoding": {"system": system, "code": code}},
            {"name": "targetsystem", "valueUri":    targetsystem},
        ],
    }


def test_WHEN_r4_coding_valueCoding_match_SHOULD_translate(client):
    resp = client.post("/r4/ConceptMap/$translate", json=_r4_coding_body(SNOMED, "38341003", OMOP))
    assert resp.status_code == 200
    data = resp.json()
    assert data["parameter"][0]["valueBoolean"] is True
    match_parts = {p["name"]: p for p in data["parameter"][1]["part"]}
    assert match_parts["equivalence"]["valueCode"] == "equivalent"
    assert match_parts["concept"]["valueCoding"]["code"] == "316866"


def test_WHEN_r4_coding_valueCoding_no_match_SHOULD_return_false(client):
    resp = client.post("/r4/ConceptMap/$translate", json=_r4_coding_body(SNOMED, "UNKNOWN", OMOP))
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is False


# ---------------------------------------------------------------------------
# R4 response format: match part must use "equivalence" not "relationship"
# ---------------------------------------------------------------------------

def test_WHEN_r4_match_SHOULD_use_equivalence_in_response(client):
    resp = client.post("/r4/ConceptMap/$translate", json=_parameters_body(SNOMED, "38341003", OMOP))
    assert resp.status_code == 200
    parts = {p["name"]: p for p in resp.json()["parameter"][1]["part"]}
    assert "equivalence" in parts
    assert "relationship" not in parts


# ---------------------------------------------------------------------------
# R5 route tests — correct R5 parameter names and response format
# ---------------------------------------------------------------------------

def test_r5_metadata_capability_statement(client):
    resp = client.get("/r5/metadata")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resourceType"] == "CapabilityStatement"
    assert body["fhirVersion"] == "5.0.0"


def test_r5_metadata_terminology_capabilities(client):
    resp = client.get("/r5/metadata", params={"mode": "terminology"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["resourceType"] == "TerminologyCapabilities"
    uris = {cs["uri"] for cs in body["codeSystem"]}
    assert "http://snomed.info/sct" in uris
    assert "http://loinc.org" in uris


def _r5_flat_body(system: str, sourceCode: str, targetSystem: str) -> dict:
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "system",       "valueUri":  system},
            {"name": "sourceCode",   "valueCode": sourceCode},
            {"name": "targetSystem", "valueUri":  targetSystem},
        ],
    }


def _r5_coding_body(system: str, code: str, targetSystem: str) -> dict:
    return {
        "resourceType": "Parameters",
        "parameter": [
            {"name": "sourceCoding", "valueCoding": {"system": system, "code": code}},
            {"name": "targetSystem", "valueUri":    targetSystem},
        ],
    }


def test_WHEN_r5_sourceCode_targetSystem_match_SHOULD_translate(client):
    resp = client.post("/r5/ConceptMap/$translate", json=_r5_flat_body(SNOMED, "38341003", OMOP))
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is True
    assert resp.json()["parameter"][1]["part"][1]["valueCoding"]["code"] == "316866"


def test_WHEN_r5_sourceCode_targetSystem_no_match_SHOULD_return_false(client):
    resp = client.post("/r5/ConceptMap/$translate", json=_r5_flat_body(SNOMED, "UNKNOWN", OMOP))
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is False


def test_WHEN_r5_missing_params_SHOULD_return_400(client):
    resp = client.post("/r5/ConceptMap/$translate", json={"resourceType": "Parameters", "parameter": []})
    assert resp.status_code == 400


def test_WHEN_r5_sourceCode_without_system_SHOULD_return_400(client):
    resp = client.post("/r5/ConceptMap/$translate", json={
        "resourceType": "Parameters",
        "parameter": [{"name": "sourceCode", "valueCode": "38341003"}],
    })
    assert resp.status_code == 400


def test_WHEN_r5_sourceCoding_targetSystem_match_SHOULD_translate(client):
    resp = client.post("/r5/ConceptMap/$translate", json=_r5_coding_body(SNOMED, "38341003", OMOP))
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is True
    assert resp.json()["parameter"][1]["part"][1]["valueCoding"]["code"] == "316866"


def test_WHEN_r5_sourceCoding_targetSystem_no_match_SHOULD_return_false(client):
    resp = client.post("/r5/ConceptMap/$translate", json=_r5_coding_body(SNOMED, "UNKNOWN", OMOP))
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is False


def test_WHEN_r5_match_SHOULD_use_relationship_not_equivalence_in_response(client):
    resp = client.post("/r5/ConceptMap/$translate", json=_r5_flat_body(SNOMED, "38341003", OMOP))
    assert resp.status_code == 200
    parts = {p["name"]: p for p in resp.json()["parameter"][1]["part"]}
    assert "relationship" in parts
    assert "equivalence" not in parts


def test_WHEN_r5_get_match_SHOULD_use_sourceCode_targetSystem_params(client):
    resp = client.get(
        "/r5/ConceptMap/$translate",
        params={"system": SNOMED, "sourceCode": "38341003", "targetSystem": OMOP},
    )
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is True


def test_WHEN_r5_bare_code_with_known_url_as_system_SHOULD_translate(client):
    resp = client.post(
        "/r5/ConceptMap/$translate",
        json={
            "resourceType": "Parameters",
            "parameter": [
                {"name": "url",          "valueUri":  "http://snomed.info/sct"},
                {"name": "sourceCode",   "valueCode": "38341003"},
                {"name": "targetSystem", "valueUri":  OMOP},
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["parameter"][0]["valueBoolean"] is True
    assert resp.json()["parameter"][1]["part"][1]["valueCoding"]["code"] == "316866"


# ---------------------------------------------------------------------------
# ICD-10-CM tests
# ---------------------------------------------------------------------------

def test_WHEN_icd10cm_E11_9_SHOULD_translate_via_maps_to(conn):
    """ICD-10-CM codes are never standard in OMOP; E11.9 resolves via Maps-to → 316866."""
    result = translate_r4(conn, ICD10CM, "E11.9", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    assert result["parameter"][1]["part"][1]["valueCoding"]["code"] == "316866"


def test_WHEN_icd10cm_E11_SHOULD_translate_via_maps_to(conn):
    result = translate_r4(conn, ICD10CM, "E11", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    assert result["parameter"][1]["part"][1]["valueCoding"]["code"] == "316866"


def test_WHEN_icd10cm_unknown_code_SHOULD_return_false(conn):
    result = translate_r4(conn, ICD10CM, "Z99.999", OMOP)
    assert result["parameter"][0]["valueBoolean"] is False


def test_WHEN_icd10cm_r4_http_post_SHOULD_translate(client):
    resp = client.post(
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
    assert parts["concept"]["valueCoding"]["code"] == "316866"


def test_WHEN_icd10cm_r5_sourceCoding_SHOULD_translate(client):
    resp = client.post(
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
    assert parts["concept"]["valueCoding"]["code"] == "316866"
