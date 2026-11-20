"""Add a scheduled procurement date to orders."""
from alembic import op
import sqlalchemy as sa

revision = '0003_order_planned_date'
down_revision = '0002_order_approval'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column['name'] for column in sa.inspect(bind).get_columns('orders')}
    indexes = {index['name'] for index in sa.inspect(bind).get_indexes('orders')}
    if 'planned_date' not in columns:
        op.add_column('orders', sa.Column('planned_date', sa.Date(), nullable=True))
    if 'ix_orders_planned_date' not in indexes:
        op.create_index('ix_orders_planned_date', 'orders', ['planned_date'])


def downgrade():
    bind = op.get_bind()
    columns = {column['name'] for column in sa.inspect(bind).get_columns('orders')}
    indexes = {index['name'] for index in sa.inspect(bind).get_indexes('orders')}
    if 'ix_orders_planned_date' in indexes:
        op.drop_index('ix_orders_planned_date', table_name='orders')
    if 'planned_date' in columns:
        op.drop_column('orders', 'planned_date')
