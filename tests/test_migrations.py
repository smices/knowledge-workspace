"""Opt-in migration race test; all DDL is isolated in a unique temporary schema."""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="requires isolated TEST_DATABASE_URL")
def test_parallel_migrators_serialize_on_postgres():
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.get_backend_name() == "postgresql"
    schema = "migration_test_" + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    processes = []
    try:
        scoped = url.update_query_dict({"options": f"-csearch_path={schema}"})
        environment = dict(os.environ, DATABASE_URL=scoped.render_as_string(hide_password=False),
                           OPENAI_API_KEY="test-placeholder", JWT_SECRET="test-secret-at-least-32-characters",
                           AUTH_MODE="jwt")
        code = (
            "from pydantic_settings.sources import DotEnvSettingsSource; "
            "DotEnvSettingsSource.__call__ = lambda self: {}; "
            "from alembic.config import Config; from alembic import command; "
            "command.upgrade(Config('alembic.ini'), 'head')"
        )
        processes = [subprocess.Popen([sys.executable, "-c", code], env=environment,
                                      cwd=Path(__file__).resolve().parents[1],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
        assert [process.wait(timeout=150) for process in processes] == [0, 0]
        with engine.connect() as connection:
            assert connection.scalar(text(f'SELECT count(*) FROM "{schema}".alembic_version')) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()
