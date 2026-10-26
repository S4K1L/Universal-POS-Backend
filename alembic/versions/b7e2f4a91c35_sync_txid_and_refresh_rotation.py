"""sync change txid, refresh-token rotation time

Revision ID: b7e2f4a91c35
Revises: a3c1d9e47f20
Create Date: 2026-10-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7e2f4a91c35'
down_revision: Union[str, None] = 'a3c1d9e47f20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows get this migration's transaction id, which is older than any later snapshot — so they are served at once.
    op.add_column('sync_changes', sa.Column('txid', sa.BigInteger(), server_default=sa.text('(pg_current_xact_id()::text::bigint)'), nullable=False))
    op.create_index('ix_sync_changes_entity', 'sync_changes', ['tenant_id', 'entity', 'entity_id'])
    op.add_column('refresh_tokens', sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('refresh_tokens', 'rotated_at')
    op.drop_index('ix_sync_changes_entity', table_name='sync_changes')
    op.drop_column('sync_changes', 'txid')
