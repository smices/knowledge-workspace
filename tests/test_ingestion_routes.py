import asyncio
from hashlib import sha256
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db, main
from app import global_ids
from app.auth import Principal


def test_upload_does_not_commit_metadata_when_minio_write_fails(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([
            db.Tenant(id="tenant", name="tenant"),
            db.Principal(id="owner", tenant_id="tenant"),
            db.Role(id="6001", tenant_id="tenant", name="reader"),
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


def test_upload_authority_failure_precedes_blob_write_and_metadata(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([db.Tenant(id="tenant", name="tenant"),
                         db.Principal(id="owner", tenant_id="tenant")])
        session.commit()
    monkeypatch.setattr(main, "SessionLocal", Session)

    class BrokenAuthority:
        def next(self, _count):
            raise RuntimeError("unavailable")

    blob_writes = []
    monkeypatch.setattr(global_ids, "_client", BrokenAuthority())
    monkeypatch.setattr(main, "put_file", lambda *args: blob_writes.append(args))
    file = UploadFile(filename="document.txt", file=BytesIO(b"content"), headers={"content-type": "text/plain"})
    with pytest.raises(global_ids.GlobalIDUnavailable):
        asyncio.run(main.upload_document(file, None, None, Principal("owner", "tenant", frozenset({"reader"}))))
    assert blob_writes == []
    with Session() as session:
        assert session.scalar(select(db.Document)) is None
        assert session.scalar(select(db.DocumentVersion)) is None
        assert session.scalar(select(db.EventOutbox)) is None
    engine.dispose()


def test_duplicate_upload_reuses_existing_ids_without_calling_authority(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([db.Tenant(id="tenant", name="tenant"),
                         db.Principal(id="owner", tenant_id="tenant"),
                         db.Role(id="6001", tenant_id="tenant", name="reader")])
        session.flush()
        session.add_all([
            db.Document(id="9001", tenant_id="tenant", title="document.txt", created_by="owner",
                        status="ready", object_key="source", content_type="text/plain"),
            db.DocumentVersion(id="9002", document_id="9001", version=1, source_object_key="source",
                               source_filename="document.txt", content_type="text/plain",
                               source_hash=sha256(b"content").hexdigest(), created_by="owner", status="ready"),
            db.DocumentGrant(document_id="9001", role_id="6001"),
        ])
        session.commit()
    monkeypatch.setattr(main, "SessionLocal", Session)

    class BrokenAuthority:
        def next(self, _count):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(global_ids, "_client", BrokenAuthority())
    monkeypatch.setattr(main, "file_exists", lambda _key: True)
    monkeypatch.setattr(main, "put_file", lambda *_args: pytest.fail("duplicate must not upload"))
    file = UploadFile(filename="document.txt", file=BytesIO(b"content"), headers={"content-type": "text/plain"})
    result = asyncio.run(main.upload_document(file, None, None, Principal("owner", "tenant", frozenset({"reader"}))))
    assert result == {"document_id": "9001", "status": "reused"}
    engine.dispose()
