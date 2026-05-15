"""add private password to albums

Revision ID: 0002
Revises: 0001
Create Date: 2024-01-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("albums", sa.Column("is_private", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("albums", sa.Column("password_hash", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("albums", "password_hash")
    op.drop_column("albums", "is_private")
