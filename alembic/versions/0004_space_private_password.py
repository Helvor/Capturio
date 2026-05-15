"""add private password to spaces

Revision ID: 0004
Revises: 0003
Create Date: 2024-01-04 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("spaces", sa.Column("is_private", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("spaces", sa.Column("password_hash", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("spaces", "password_hash")
    op.drop_column("spaces", "is_private")
