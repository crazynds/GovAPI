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
# (APP_DATABASE_URL), sem duplicar a URL em dois lugares.
# `%` dobrado -- o ConfigParser usado por Alembic trata `%` como
# interpolação; sem isso, uma URL com caracteres especiais escapados
# (senha com # ou % gera %23/%25) quebra com "invalid interpolation syntax".
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# `correios_cep` era excluída daqui, quando o edne-correios-loader era dono do
# esquema dela. Hoje é um model nosso (models.PostalCode): a lib só popula uma
# tabela de scratch e nós fazemos o upsert, então o Alembic gerencia a tabela
# como qualquer outra -- e é isso que permite a FK de establishments.cep.

# `postal_codes_import` é a tabela de scratch daquele import: criada e
# destruída dentro de `import-ceps` (ver `app.ceps.SCRATCH_TABLE`), nunca deve
# aparecer num autogenerate.
#
# As três de staging (`*_staging`) estão aqui por um motivo diferente: elas SÃO
# models nossos, mas o ciclo de vida delas é do import, não do schema. São
# scratch UNLOGGED de ~63M linhas que só existe entre o COPY e o swap do build
# -- o `import-all` as dropa no fim e o pipeline as recria no início do próximo
# (`ensure_staging_tables`, em app/importer/pipeline.py). Sem excluí-las daqui,
# todo autogenerate as detectaria como "added table" e a próxima migration as
# devolveria ao schema, desfazendo isso silenciosamente.
TABLES_NOT_MANAGED_BY_ALEMBIC = {
    "postal_codes_import",
    "companies_staging",
    "establishments_staging",
    "simples_staging",
}

# As tabelas-SOMBRA do swap atômico do build (`establishments_new`,
# `partners_old`, ...). Também não são schema: o build monta `<tabela>_new`,
# renomeia a viva pra `<tabela>_old` e dropa. Um autogenerate rodado enquanto
# uma delas existe — durante um import, ou depois de um que morreu no meio —
# as veria no banco e não no metadata, e geraria uma migration com `drop_table`
# pra cada uma. Aplicá-la no meio de um build apagaria a tabela que o import
# está montando.
TRANSIENT_TABLE_SUFFIXES = ("_new", "_old")


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table":
        if name in TABLES_NOT_MANAGED_BY_ALEMBIC:
            return False
        if name.endswith(TRANSIENT_TABLE_SUFFIXES):
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
