"""municipios bootstrap: ibge_code as fk target, correios_cep fk to municipio

`municipios.ibge_code` era VARCHAR(7) e sem constraint de unicidade -- passa a
Integer com índice único, pra poder ser alvo de uma FOREIGN KEY (Postgres
exige tipos comparáveis e um índice único do lado referenciado). Isso é o que
permite `correios_cep.municipio_cod_ibge` (já Integer) referenciar `municipios`
de verdade.

`municipios.receita_code` vira nullable: `municipios` agora nasce da API de
Localidades do IBGE (`import-municipios`, roda antes de tudo) com ibge_code
exato; o `receita_code` só chega depois, quando o `Municipios.zip` da própria
Receita (sem UF nem ibge_code, só nome) casa por nome contra as linhas que já
existem -- ver app.importer.pipeline._merge_municipios_receita. Uma linha pode
ficar sem receita_code até esse import rodar, ou pra sempre no raro caso de
nome sem correspondência exata entre as duas fontes.

`import_all_run.municipios`: nova fase 1/6 de `import-all` (o bootstrap tem
que rodar antes de CEPs e CNPJ).

Revision ID: ce6ff13ae5c4
Revises: f2b5d9f51a11
Create Date: 2026-08-29 02:15:27.326160

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ce6ff13ae5c4'
down_revision: Union[str, Sequence[str], None] = 'f2b5d9f51a11'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default explicito: `import_all_run` pode ja ter a linha unica
    # (id=1) de um `import-all` anterior -- ADD COLUMN NOT NULL sem default
    # falharia numa tabela nao vazia. O default python-side ("pending") nao
    # ajuda aqui, so vale pra INSERTs futuros feitos pela ORM.
    op.add_column(
        'import_all_run',
        sa.Column('municipios', sa.String(length=20), nullable=False, server_default='pending'),
    )
    op.alter_column('municipios', 'receita_code',
               existing_type=sa.INTEGER(),
               nullable=True)
    # USING explicito: sem ele o Postgres recusa varchar -> integer
    # ("column cannot be cast automatically"). Os valores existentes (se
    # houver) sao sempre so digitos -- vieram de `localidade["id"]` da API do
    # IBGE, nunca digitados a mao.
    op.alter_column('municipios', 'ibge_code',
               existing_type=sa.VARCHAR(length=7),
               type_=sa.Integer(),
               existing_nullable=True,
               postgresql_using='ibge_code::integer')
    op.drop_index('ix_municipios_ibge_code', table_name='municipios')
    op.create_index(op.f('ix_municipios_ibge_code'), 'municipios', ['ibge_code'], unique=True)
    op.create_foreign_key(
        'correios_cep_municipio_cod_ibge_fkey', 'correios_cep', 'municipios',
        ['municipio_cod_ibge'], ['ibge_code'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('correios_cep_municipio_cod_ibge_fkey', 'correios_cep', type_='foreignkey')
    op.drop_index(op.f('ix_municipios_ibge_code'), table_name='municipios')
    op.create_index('ix_municipios_ibge_code', 'municipios', ['ibge_code'], unique=False)
    op.alter_column('municipios', 'ibge_code',
               existing_type=sa.Integer(),
               type_=sa.VARCHAR(length=7),
               existing_nullable=True,
               postgresql_using='ibge_code::text')
    op.alter_column('municipios', 'receita_code',
               existing_type=sa.INTEGER(),
               nullable=False)
    op.drop_column('import_all_run', 'municipios')
