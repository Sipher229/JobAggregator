import pytest
from sqlalchemy.orm import sessionmaker

from core.database import Base, make_engine
from core import models  # noqa: F401
from core.seed import sync_communities


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def seeded_session(session):
    sync_communities(session)
    session.commit()
    return session
