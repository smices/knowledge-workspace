from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import cache, db


def test_knowledge_revision_is_postgres_owned_and_passed_transaction_is_not_committed(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", Session)
    with Session() as session:
        session.add(db.Tenant(id="tenant", name="tenant"))
        session.commit()
    assert cache.knowledge_revision("tenant") == 0
    with Session() as session:
        assert cache.bump_knowledge_revision("tenant", session) == 1
        assert cache.knowledge_revision("tenant", session) == 1
        session.rollback()
    assert cache.knowledge_revision("tenant") == 0
    assert cache.bump_knowledge_revision("tenant") == 1
    assert cache.knowledge_revision("tenant") == 1
    engine.dispose()
