from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from app import models  # noqa: F401 -- registra os models em Base.metadata
from app.config import settings
from app.db import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Sobrescreve o valor do alembic.ini -- uma fonte só de configuração
# (CNPJ_DATABASE_URL), sem duplicar a URL em dois lugares.
config.set_main_option("sqlalchemy.url", settings.database_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `correios_cep` (criada pelo edne-correios-loader / bootstrap em
# app/routers/enderecos.py) não é gerenciada pelo Alembic -- é dona externa
# do próprio esquema, reconstruído do zero a cada `import-ceps`.
target_metadata = Base.metadata

TABLES_NOT_MANAGED_BY_ALEMBIC = {"correios_cep"}


def include_object(object, name, type_, reflected, compare_to):
    # Sem isso, autogenerate acha `correios_cep` (existe no banco, não está
    # em Base.metadata) e sugere um DROP TABLE -- ela é de outra ferramenta.
    if type_ == "table" and name in TABLES_NOT_MANAGED_BY_ALEMBIC:
        return False
    return True

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
