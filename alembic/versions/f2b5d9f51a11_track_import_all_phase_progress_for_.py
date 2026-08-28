"""track import-all phase progress for resume

Uma linha (id=1) com o status de cada uma das 5 fases que `import-all`
encadeia. Cancelar no meio (Ctrl-C ou qualquer falha) e chamar de novo
retomava do zero -- fase 1 de novo, mesmo já tendo passado da fase 2 -- porque
nada persistia entre chamadas. Agora cada fase grava seu próprio status antes
e depois de rodar, e a próxima chamada pula toda fase já 'success', a não ser
que a tentativa anterior tenha terminado com sucesso (aí é tratado como um
refresh periódico de verdade, e as 5 rodam de novo). Ver app.cli.import_all.

Revision ID: f2b5d9f51a11
Revises: d037328b3558
Create Date: 2026-08-28 18:57:55.530468

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b5d9f51a11'
down_revision: Union[str, Sequence[str], None] = 'd037328b3558'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('import_all_run',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('ceps', sa.String(length=20), nullable=False),
    sa.Column('ceps_osm', sa.String(length=20), nullable=False),
    sa.Column('cnpj', sa.String(length=20), nullable=False),
    sa.Column('ibge', sa.String(length=20), nullable=False),
    sa.Column('municipios_geo', sa.String(length=20), nullable=False),
    sa.Column('message', sa.String(length=255), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('import_all_run')
