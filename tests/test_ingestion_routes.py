import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db, main
from app.auth import Principal


def test_upload_does_not_commit_metadata_when_minio_write_fails(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([
            db.Tenant(id="tenant", name="tenant"),
            db.Principal(id="owner", tenant_id="tenant"),
            db.Role(id="tenant:reader", tenant_id="tenant", name="reader"),
        ])
        session.commit()
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "put_file", lambda *args: (_ for _ in ()).throw(RuntimeError("storage down")))
    monkeypatch.setattr(main, "delete_file", lambda *args: None)
    file = UploadFile(filename="document.txt", file=BytesIO(b"content"), headers={"content-type": "text/plain"})
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.upload_document(file, None, None, Principal("owner", "tenant", frozenset({"reader"}))))
    assert error.value.status_code == 503
    with Session() as session:
        assert session.scalar(select(db.Document)) is None
    engine.dispose()
