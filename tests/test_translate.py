import sqlite3
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from enchilada.main import app
from enchilada.translate import translate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """In-memory SQLite DB with the schema and known-good test rows."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript("""
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
        INSERT INTO concept VALUES (316866, '38341003',  'SNOMED', 'S');
        INSERT INTO concept VALUES (437663, '386661006', 'SNOMED', 'S');

        -- non-standard concept + Maps to relationship (step 2 path)
        INSERT INTO concept VALUES (999001, 'NONSTANDARD_CODE', 'SNOMED', NULL);
        INSERT INTO concept_relationship VALUES (999001, 316866, 'Maps to');
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

SNOMED = "http://snomed.info/sct"
OMOP   = "http://ohdsi.org/omop"


def test_standard_concept_hypertension(conn):
    result = translate(conn, SNOMED, "38341003", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    concept = result["parameter"][1]["part"][1]["valueCoding"]
    assert concept["code"] == "316866"
    assert concept["system"] == OMOP


def test_standard_concept_fever(conn):
    result = translate(conn, SNOMED, "386661006", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    assert result["parameter"][1]["part"][1]["valueCoding"]["code"] == "437663"


def test_maps_to_relationship(conn):
    result = translate(conn, SNOMED, "NONSTANDARD_CODE", OMOP)
    assert result["parameter"][0]["valueBoolean"] is True
    assert result["parameter"][1]["part"][1]["valueCoding"]["code"] == "316866"


def test_no_match(conn):
    result = translate(conn, SNOMED, "UNKNOWN_CODE", OMOP)
    assert result["parameter"][0]["valueBoolean"] is False
    assert "UNKNOWN_CODE" in result["parameter"][1]["valueString"]


def test_unknown_system(conn):
    result = translate(conn, "http://example.com/unknown", "12345", OMOP)
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
