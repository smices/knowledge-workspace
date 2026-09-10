"""Opt-in check that the Alembic head contains the declarative schema."""

import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.db import Base


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires isolated TEST_DATABASE_URL")
def test_alembic_head_matches_declared_tables_and_columns():
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.get_backend_name() == "postgresql"
    schema = "schema_parity_" + uuid4().hex
    control = create_engine(url)
    with control.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(scoped)
    try:
        environment = dict(
            os.environ,
            DATABASE_URL=scoped.render_as_string(hide_password=False),
            AUTH_MODE="jwt",
            OPENAI_API_KEY="test-placeholder",
            JWT_SECRET="test-secret-at-least-32-characters",
        )
        code = (
            "from pydantic_settings.sources import DotEnvSettingsSource; "
            "DotEnvSettingsSource.__call__ = lambda self: {}; "
            "from alembic.config import Config; from alembic import command; "
            "command.upgrade(Config('alembic.ini'), 'head')"
        )
        subprocess.run(
            [sys.executable, "-c", code],
            env=environment,
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
        )

        database = inspect(engine)
        actual_tables = set(database.get_table_names()) - {"alembic_version"}
        declared_tables = set(Base.metadata.tables)
        assert actual_tables == declared_tables
        for table_name, table in Base.metadata.tables.items():
            actual_columns = {column["name"] for column in database.get_columns(table_name)}
            assert actual_columns == {column.name for column in table.columns}
    finally:
        engine.dispose()
        with control.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        control.dispose()
