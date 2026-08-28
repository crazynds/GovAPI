"""store raw address as json when the cep has no match

Um estabelecimento agora está num de dois estados, nunca nos dois:

  * vinculado a um CEP -- `cep` preenchido, e logradouro/bairro/município/UF
    vêm de `correios_cep` no momento da leitura;
  * sem vínculo -- `cep` NULL e o registro de endereço da Receita inteiro em
    `address`.

Sem vínculo cobre CEP ausente no arquivo da Receita e CEP que não existe na
base dos Correios (digitado errado, extinto, endereço no exterior). Guardar
esse CEP na coluna não resolveria endereço nenhum e impediria uma FOREIGN KEY
para `correios_cep`.

JSONB e não colunas porque são a exceção, não a regra: a grande maioria das
linhas casa com um CEP e fica com `address` NULL, que não ocupa nada além do
bit no null bitmap.

ADD COLUMN nullable é instantâneo. A coluna nasce vazia -- o endereço só é
redistribuído no próximo `import-cnpj` completo.

Revision ID: 0a7f993196e6
Revises: 1764b9ea943d
Create Date: 2026-08-28 17:17:52.137429

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0a7f993196e6'
down_revision: Union[str, Sequence[str], None] = '1764b9ea943d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('establishments', sa.Column('address', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('establishments', 'address')
    # ### end Alembic commands ###
