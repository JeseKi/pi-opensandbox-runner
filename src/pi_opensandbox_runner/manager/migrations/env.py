from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, Engine, engine_from_config, pool

from pi_opensandbox_runner.manager import models as _models  # noqa: F401
from pi_opensandbox_runner.manager.database import Base

config = context.config
target_metadata = Base.metadata


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


provided = config.attributes.get("connection")
if isinstance(provided, Engine):
    with provided.connect() as connection:
        run_migrations(connection)
elif context.is_offline_mode():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        run_migrations(connection)
