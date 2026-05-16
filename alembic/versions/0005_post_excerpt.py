from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('posts', sa.Column('excerpt', sa.Text(), nullable=True))

def downgrade():
    op.drop_column('posts', 'excerpt')
