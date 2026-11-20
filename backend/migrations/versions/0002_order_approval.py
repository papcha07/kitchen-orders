"""Add chef approval state to orders."""
from alembic import op
import sqlalchemy as sa

revision = '0002_order_approval'
down_revision = '0001_initial'
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    columns = {column['name'] for column in sa.inspect(bind).get_columns('orders')}
    indexes = {index['name'] for index in sa.inspect(bind).get_indexes('orders')}
    if 'is_approved' not in columns:
        op.add_column(
            'orders',
            sa.Column('is_approved', sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.alter_column('orders', 'is_approved', server_default=None)
    if 'ix_orders_is_approved' not in indexes:
        op.create_index('ix_orders_is_approved', 'orders', ['is_approved'])

def downgrade():
    bind = op.get_bind()
    columns = {column['name'] for column in sa.inspect(bind).get_columns('orders')}
    indexes = {index['name'] for index in sa.inspect(bind).get_indexes('orders')}
    if 'ix_orders_is_approved' in indexes:
        op.drop_index('ix_orders_is_approved', table_name='orders')
    if 'is_approved' in columns:
        op.drop_column('orders', 'is_approved')
