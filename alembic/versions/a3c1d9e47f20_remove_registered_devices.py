"""remove registered devices

Revision ID: a3c1d9e47f20
Revises: 6bae64dd74a2
Create Date: 2026-10-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a3c1d9e47f20'
down_revision: Union[str, None] = '6bae64dd74a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEVICE_ID_TABLES = ("sync_events", "expenses", "shifts", "sales", "sale_payments", "stock_movements")


def upgrade() -> None:
    op.drop_constraint('fk_sales_device_id_devices', 'sales', type_='foreignkey')
    for table in DEVICE_ID_TABLES:
        op.drop_column(table, 'device_id')
    op.drop_index(op.f('ix_devices_tenant_id'), table_name='devices')
    op.drop_index(op.f('ix_devices_branch_id'), table_name='devices')
    op.drop_table('devices')
    op.execute("DELETE FROM tenant_counters WHERE name = 'device'")
    op.execute("UPDATE plans SET limits = limits - 'devices'")


def downgrade() -> None:
    op.create_table('devices',
    sa.Column('branch_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('device_code', sa.String(length=8), nullable=False),
    sa.Column('platform', sa.String(length=32), nullable=False),
    sa.Column('app_version', sa.String(length=32), nullable=False),
    sa.Column('registered_by', sa.Uuid(), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], name=op.f('fk_devices_branch_id_branches'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_devices_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_devices')),
    sa.UniqueConstraint('tenant_id', 'device_code', name='uq_devices_tenant_code')
    )
    op.create_index(op.f('ix_devices_branch_id'), 'devices', ['branch_id'], unique=False)
    op.create_index(op.f('ix_devices_tenant_id'), 'devices', ['tenant_id'], unique=False)
    for table in DEVICE_ID_TABLES:
        op.add_column(table, sa.Column('device_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(op.f('fk_sales_device_id_devices'), 'sales', 'devices', ['device_id'], ['id'], ondelete='SET NULL')
