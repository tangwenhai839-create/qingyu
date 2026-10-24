"""record archive integrity errors"""
import sqlalchemy as sa
from alembic import op

revision='0010_archive_integrity_error'; down_revision='0009_revision_fingerprint'; branch_labels=None; depends_on=None
def upgrade(): op.add_column('archive_objects', sa.Column('integrity_error', sa.Text(), nullable=True))
def downgrade(): op.drop_column('archive_objects','integrity_error')
