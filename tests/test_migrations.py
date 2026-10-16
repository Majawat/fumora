from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from fumora.db import make_engine, upgrade_db
from fumora.models import Base


def test_migrations_match_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade_db(url)
    engine = make_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
