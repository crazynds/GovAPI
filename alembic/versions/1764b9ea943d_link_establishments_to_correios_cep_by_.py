"""link establishments to correios_cep by cep

Guarda o endereço do estabelecimento, que até agora era lido do CSV e
descartado. O vínculo com a base dos Correios (`correios_cep`) é feito pelo
próprio CEP, que é a chave primária de lá.

Sem FOREIGN KEY, e não por descuido: `correios_cep` pertence ao
edne-correios-loader, que a cada `import-ceps` faz DELETE de todas as linhas e
repovoa. Uma FK travaria esse DELETE (NO ACTION), e CASCADE/SET NULL apagariam
o endereço de dezenas de milhões de empresas. Além disso a Receita traz CEP
inexistente e malformado, que nenhuma FK aceitaria.

Logradouro e bairro só são gravados quando o CEP NÃO os resolve -- CEP de
localidade (cidade pequena com um CEP só) não tem rua na base dos Correios.
Quando resolve, ficam NULL e vêm do join na leitura, sem duplicar o texto em
~63M linhas. Isso é decidido no build, então `import-ceps` precisa rodar antes
de `import-cnpj` (é a ordem do `import-all`).

ADD COLUMN nullable é instantâneo no Postgres, e as colunas nascem vazias: o
endereço só aparece depois do próximo `import-cnpj` completo.

Revision ID: 1764b9ea943d
Revises: 7a6d0dfd18cb
Create Date: 2026-08-28 17:00:30.259582

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1764b9ea943d'
down_revision: Union[str, Sequence[str], None] = '7a6d0dfd18cb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('estabelecimentos_staging', sa.Column('cep', sa.Integer(), nullable=True))
    op.add_column('estabelecimentos_staging', sa.Column('logradouro', sa.Text(), nullable=True))
    op.add_column('estabelecimentos_staging', sa.Column('numero', sa.Text(), nullable=True))
    op.add_column('estabelecimentos_staging', sa.Column('complemento', sa.Text(), nullable=True))
    op.add_column('estabelecimentos_staging', sa.Column('bairro', sa.Text(), nullable=True))
    op.add_column('establishments', sa.Column('cep', sa.Integer(), nullable=True))
    op.add_column('establishments', sa.Column('address_number', sa.Text(), nullable=True))
    op.add_column('establishments', sa.Column('address_complement', sa.Text(), nullable=True))
    op.add_column('establishments', sa.Column('street', sa.Text(), nullable=True))
    op.add_column('establishments', sa.Column('district', sa.Text(), nullable=True))
    op.create_index('ix_establishments_cep', 'establishments', ['cep'], unique=False, postgresql_where=sa.text('cep IS NOT NULL'))

    # Mesmo lz4 das outras colunas de texto (ver a migration c076b6b91515);
    # ignorado se o servidor não tiver sido compilado com suporte.
    for table, column in (
        ("establishments", "address_number"),
        ("establishments", "address_complement"),
        ("establishments", "street"),
        ("establishments", "district"),
        ("estabelecimentos_staging", "logradouro"),
        ("estabelecimentos_staging", "complemento"),
        ("estabelecimentos_staging", "bairro"),
    ):
        savepoint = op.get_bind().begin_nested()
        try:
            op.get_bind().execute(sa.text(f'ALTER TABLE {table} ALTER COLUMN "{column}" SET COMPRESSION lz4'))
            savepoint.commit()
        except sa.exc.DatabaseError:
            savepoint.rollback()
            break


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_establishments_cep', table_name='establishments', postgresql_where=sa.text('cep IS NOT NULL'))
    op.drop_column('establishments', 'district')
    op.drop_column('establishments', 'street')
    op.drop_column('establishments', 'address_complement')
    op.drop_column('establishments', 'address_number')
    op.drop_column('establishments', 'cep')
    op.drop_column('estabelecimentos_staging', 'bairro')
    op.drop_column('estabelecimentos_staging', 'complemento')
    op.drop_column('estabelecimentos_staging', 'numero')
    op.drop_column('estabelecimentos_staging', 'logradouro')
    op.drop_column('estabelecimentos_staging', 'cep')
    # ### end Alembic commands ###
