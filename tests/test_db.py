"""Tests for enchilada/db.py schema, loaders, and init_db."""

import csv
import sqlite3
import tempfile
from pathlib import Path

import pytest

from enchilada.db import (
    _load_concept_extra,
    _load_concept_relationship_extra,
    _load_vocabulary_extra,
    _setup_schema,
    init_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _setup_schema(conn)
    conn.commit()
    return conn


def _write_tsv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestSchema:
    def test_vocabulary_table_exists(self):
        conn = _make_conn()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "vocabulary" in tables

    def test_concept_table_exists(self):
        conn = _make_conn()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "concept" in tables

    def test_concept_relationship_table_exists(self):
        conn = _make_conn()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "concept_relationship" in tables

    def test_vocabulary_columns(self):
        conn = _make_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vocabulary)")}
        assert cols >= {"vocabulary_id", "vocabulary_name", "vocabulary_reference"}


# ---------------------------------------------------------------------------
# _load_vocabulary_extra tests
# ---------------------------------------------------------------------------

class TestLoadVocabularyExtra:
    def test_WHEN_vocabulary_extra_loaded_SHOULD_populate_vocabulary_table(self, tmp_path):
        conn = _make_conn()
        csv_path = tmp_path / "vocab.tsv"
        _write_tsv(csv_path, [
            {"vocabulary_id": "AdministrativeGender",
             "vocabulary_name": "FHIR R4 Administrative Gender",
             "vocabulary_reference": "http://hl7.org/fhir/administrative-gender"},
        ])
        _load_vocabulary_extra(conn, str(csv_path))
        row = conn.execute(
            "SELECT * FROM vocabulary WHERE vocabulary_id = 'AdministrativeGender'"
        ).fetchone()
        assert row is not None
        assert row["vocabulary_name"] == "FHIR R4 Administrative Gender"
        assert row["vocabulary_reference"] == "http://hl7.org/fhir/administrative-gender"

    def test_WHEN_vocabulary_loaded_twice_SHOULD_replace_not_duplicate(self, tmp_path):
        conn = _make_conn()
        csv_path = tmp_path / "vocab.tsv"
        _write_tsv(csv_path, [
            {"vocabulary_id": "TestVocab", "vocabulary_name": "Old Name", "vocabulary_reference": ""},
        ])
        _load_vocabulary_extra(conn, str(csv_path))
        _write_tsv(csv_path, [
            {"vocabulary_id": "TestVocab", "vocabulary_name": "New Name", "vocabulary_reference": ""},
        ])
        _load_vocabulary_extra(conn, str(csv_path))
        rows = conn.execute("SELECT * FROM vocabulary WHERE vocabulary_id = 'TestVocab'").fetchall()
        assert len(rows) == 1
        assert rows[0]["vocabulary_name"] == "New Name"

    def test_WHEN_multiple_vocabularies_SHOULD_load_all(self, tmp_path):
        conn = _make_conn()
        csv_path = tmp_path / "vocab.tsv"
        _write_tsv(csv_path, [
            {"vocabulary_id": "VocabA", "vocabulary_name": "A", "vocabulary_reference": ""},
            {"vocabulary_id": "VocabB", "vocabulary_name": "B", "vocabulary_reference": ""},
        ])
        _load_vocabulary_extra(conn, str(csv_path))
        count = conn.execute("SELECT count(*) FROM vocabulary").fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# _load_concept_extra tests
# ---------------------------------------------------------------------------

class TestLoadConceptExtra:
    def test_WHEN_local_concept_loaded_SHOULD_store_null_standard_concept(self, tmp_path):
        conn = _make_conn()
        csv_path = tmp_path / "concepts.tsv"
        _write_tsv(csv_path, [
            {"concept_id": "2000000001", "concept_code": "male",
             "vocabulary_id": "AdministrativeGender", "standard_concept": ""},
        ])
        _load_concept_extra(conn, str(csv_path))
        row = conn.execute(
            "SELECT * FROM concept WHERE concept_code = 'male' AND vocabulary_id = 'AdministrativeGender'"
        ).fetchone()
        assert row is not None
        assert int(row["concept_id"]) == 2000000001
        assert row["standard_concept"] is None

    def test_WHEN_local_concept_id_SHOULD_be_gt_2billion(self, tmp_path):
        conn = _make_conn()
        csv_path = tmp_path / "concepts.tsv"
        _write_tsv(csv_path, [
            {"concept_id": "2500000000", "concept_code": "AMB",
             "vocabulary_id": "v3-ActCode", "standard_concept": ""},
        ])
        _load_concept_extra(conn, str(csv_path))
        row = conn.execute("SELECT concept_id FROM concept WHERE concept_code = 'AMB'").fetchone()
        assert int(row["concept_id"]) > 2_000_000_000

    def test_WHEN_concept_extra_loaded_twice_SHOULD_not_duplicate(self, tmp_path):
        conn = _make_conn()
        csv_path = tmp_path / "concepts.tsv"
        _write_tsv(csv_path, [
            {"concept_id": "2000000001", "concept_code": "male",
             "vocabulary_id": "AdministrativeGender", "standard_concept": ""},
        ])
        _load_concept_extra(conn, str(csv_path))
        _load_concept_extra(conn, str(csv_path))
        rows = conn.execute(
            "SELECT * FROM concept WHERE concept_code = 'male' AND vocabulary_id = 'AdministrativeGender'"
        ).fetchall()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# _load_concept_relationship_extra tests
# ---------------------------------------------------------------------------

class TestLoadConceptRelationshipExtra:
    def test_WHEN_maps_to_loaded_SHOULD_be_queryable(self, tmp_path):
        conn = _make_conn()
        csv_path = tmp_path / "cr.tsv"
        _write_tsv(csv_path, [
            {"concept_id_1": "2000000001", "concept_id_2": "8507", "relationship_id": "Maps to"},
        ])
        _load_concept_relationship_extra(conn, str(csv_path))
        row = conn.execute(
            "SELECT concept_id_2 FROM concept_relationship "
            "WHERE concept_id_1 = 2000000001 AND relationship_id = 'Maps to'"
        ).fetchone()
        assert row is not None
        assert int(row["concept_id_2"]) == 8507

    def test_WHEN_relationship_loaded_twice_SHOULD_not_duplicate(self, tmp_path):
        conn = _make_conn()
        csv_path = tmp_path / "cr.tsv"
        _write_tsv(csv_path, [
            {"concept_id_1": "2000000001", "concept_id_2": "8507", "relationship_id": "Maps to"},
        ])
        _load_concept_relationship_extra(conn, str(csv_path))
        _load_concept_relationship_extra(conn, str(csv_path))
        rows = conn.execute(
            "SELECT * FROM concept_relationship WHERE concept_id_1 = 2000000001"
        ).fetchall()
        assert len(rows) == 1

    def test_WHEN_concept_extra_and_relationship_extra_combined_SHOULD_resolve_via_maps_to(
        self, tmp_path
    ):
        """Full integration: local concept + Maps to relationship resolves to standard concept."""
        from enchilada.translate import translate_r4
        conn = _make_conn()

        # Standard concept (the target)
        conn.execute("INSERT INTO concept VALUES (8507, 'M', 'Gender', 'S')")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_concept ON concept (concept_code, vocabulary_id)")
        conn.commit()

        # Load local source concept and relationship
        concept_csv = tmp_path / "concepts.tsv"
        _write_tsv(concept_csv, [
            {"concept_id": "2409062701", "concept_code": "male",
             "vocabulary_id": "AdministrativeGender", "standard_concept": ""},
        ])
        _load_concept_extra(conn, str(concept_csv))

        cr_csv = tmp_path / "cr.tsv"
        _write_tsv(cr_csv, [
            {"concept_id_1": "2409062701", "concept_id_2": "8507", "relationship_id": "Maps to"},
        ])
        _load_concept_relationship_extra(conn, str(cr_csv))

        result = translate_r4(conn, "http://hl7.org/fhir/administrative-gender", "male",
                              "https://athena.ohdsi.org")
        assert result["parameter"][0]["valueBoolean"] is True
        assert result["parameter"][1]["part"][1]["valueCoding"]["code"] == "8507"
