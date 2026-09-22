import os

import psycopg
import pytest

pytestmark = pytest.mark.integration


def _dsn() -> str:
    user = os.getenv("POSTGRES_USER", "triage")
    password = os.getenv("POSTGRES_PASSWORD", "triage")
    db = os.getenv("POSTGRES_DB", "triage")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{password}@localhost:{port}/{db}"


def test_pgvector_extension_is_installed() -> None:
    with psycopg.connect(_dsn()) as conn:
        row = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    assert row is not None, "pgvector extension missing: was infra/postgres/init.sql applied?"


def test_vector_similarity_query_works() -> None:
    with psycopg.connect(_dsn()) as conn:
        row = conn.execute("SELECT '[1,0,0]'::vector <=> '[0,1,0]'::vector").fetchone()
    assert row is not None
    assert row[0] == pytest.approx(1.0)  # cosine distance of orthogonal vectors
