"""Add the terminal checklist and revision workflow."""
from pathlib import Path
import json

from alembic import op
import sqlalchemy as sa


revision = '0004_daily_checklists'
down_revision = '0003_order_planned_date'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    product_columns = {column['name'] for column in sa.inspect(bind).get_columns('products')}
    if 'terminal_order' not in product_columns:
        op.add_column('products', sa.Column('terminal_order', sa.Integer(), nullable=True))
        op.create_index('ix_products_terminal_order', 'products', ['terminal_order'])
    if 'terminal_category' not in product_columns:
        op.add_column('products', sa.Column('terminal_category', sa.String(length=160), nullable=True))

    order_columns = {column['name'] for column in sa.inspect(bind).get_columns('orders')}
    if 'requires_revision' not in order_columns:
        op.add_column('orders', sa.Column('requires_revision', sa.Boolean(), nullable=False, server_default=sa.false()))
        op.create_index('ix_orders_requires_revision', 'orders', ['requires_revision'])
    if 'review_note' not in order_columns:
        op.add_column('orders', sa.Column('review_note', sa.Text(), nullable=True))

    inspector = sa.inspect(bind)
    if not inspector.has_table('daily_checklists'):
        op.create_table(
            'daily_checklists',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('author_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('planned_date', sa.Date(), nullable=False),
            sa.Column('submitted_order_id', sa.Integer(), sa.ForeignKey('orders.id'), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint('author_id', 'planned_date', name='uq_daily_checklists_author_date'),
        )
        op.create_index('ix_daily_checklists_author_id', 'daily_checklists', ['author_id'])
        op.create_index('ix_daily_checklists_planned_date', 'daily_checklists', ['planned_date'])
    if not inspector.has_table('daily_checklist_entries'):
        op.create_table(
            'daily_checklist_entries',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('checklist_id', sa.Integer(), sa.ForeignKey('daily_checklists.id', ondelete='CASCADE'), nullable=False),
            sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=False),
            sa.Column('sort_order', sa.Integer(), nullable=False),
            sa.Column('category', sa.String(length=160), nullable=True),
            sa.Column('state', sa.String(length=20), nullable=False, server_default='pending'),
            sa.Column('quantity', sa.Float(), nullable=True),
            sa.Column('unit', sa.String(length=40), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.UniqueConstraint('checklist_id', 'sort_order', name='uq_checklist_sort_order'),
        )
        op.create_index('ix_daily_checklist_entries_checklist_id', 'daily_checklist_entries', ['checklist_id'])
        op.create_index('ix_daily_checklist_entries_product_id', 'daily_checklist_entries', ['product_id'])
        op.create_index('ix_daily_checklist_entries_sort_order', 'daily_checklist_entries', ['sort_order'])
        op.create_index('ix_daily_checklist_entries_state', 'daily_checklist_entries', ['state'])

    catalog_path = Path(__file__).resolve().parents[2] / 'terminal_catalog.json'
    if not catalog_path.exists():
        catalog_path = Path(__file__).resolve().parents[3] / 'data' / 'terminal_catalog.json'
    catalog = json.loads(catalog_path.read_text(encoding='utf-8')) if catalog_path.exists() else []
    for item in catalog:
        bind.execute(
            sa.text('UPDATE products SET terminal_order=:sort_order, terminal_category=:category WHERE name=:name'),
            {'sort_order': item['terminal_order'], 'category': item['category'], 'name': item['name']},
        )


def downgrade():
    op.drop_index('ix_daily_checklist_entries_state', table_name='daily_checklist_entries')
    op.drop_index('ix_daily_checklist_entries_sort_order', table_name='daily_checklist_entries')
    op.drop_index('ix_daily_checklist_entries_product_id', table_name='daily_checklist_entries')
    op.drop_index('ix_daily_checklist_entries_checklist_id', table_name='daily_checklist_entries')
    op.drop_table('daily_checklist_entries')
    op.drop_index('ix_daily_checklists_planned_date', table_name='daily_checklists')
    op.drop_index('ix_daily_checklists_author_id', table_name='daily_checklists')
    op.drop_table('daily_checklists')
    op.drop_column('orders', 'review_note')
    op.drop_index('ix_orders_requires_revision', table_name='orders')
    op.drop_column('orders', 'requires_revision')
    op.drop_column('products', 'terminal_category')
    op.drop_index('ix_products_terminal_order', table_name='products')
    op.drop_column('products', 'terminal_order')
