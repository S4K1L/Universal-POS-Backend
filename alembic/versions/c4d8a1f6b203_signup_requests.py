"""signup requests: self-service signup waits for super-admin approval

Revision ID: c4d8a1f6b203
Revises: b7e2f4a91c35
Create Date: 2026-11-08 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4d8a1f6b203'
down_revision: Union[str, None] = 'b7e2f4a91c35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'signup_requests',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('phone', sa.String(length=20), nullable=False),
        sa.Column('password_hash', sa.String(length=200), nullable=False),
        sa.Column('business_name', sa.String(length=160), nullable=False),
        sa.Column('business_type_key', sa.String(length=40), nullable=False),
        sa.Column('language', sa.String(length=2), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('reviewed_by_name', sa.String(length=160), nullable=False),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reject_reason', sa.String(length=300), nullable=False),
        sa.Column('plan_key', sa.String(length=40), nullable=False),
        sa.Column('tenant_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_signup_requests_phone', 'signup_requests', ['phone'])
    op.create_index('uq_signup_requests_live_phone', 'signup_requests', ['phone'], unique=True, postgresql_where=sa.text("status = 'pending'"))


def downgrade() -> None:
    op.drop_index('uq_signup_requests_live_phone', table_name='signup_requests')
    op.drop_index('ix_signup_requests_phone', table_name='signup_requests')
    op.drop_table('signup_requests')
