"""
Alembic migration environment — AI Wallet Guard v6.

Run migrations:
    alembic upgrade head

Create a new migration:
    alembic revision --autogenerate -m "describe your change"
"""
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# Import our engine so Alembic uses the same connection string.
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import engine  # noqa: E402

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = None  # we manage schema via raw SQL migrations


def run_migrations_offline():
    context.configure(
        url=str(engine.url), target_metadata=target_metadata,
        literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
