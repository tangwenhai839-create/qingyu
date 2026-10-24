"""add complete message revision fingerprint"""
import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision='0009_revision_fingerprint'; down_revision='0008_import_result_codes'; branch_labels=None; depends_on=None

def _fp(row):
    payload={"role":row.role,"content_text":row.content_text,"content_json":row.content_json,"model":row.model,"raw_timestamp":row.raw_timestamp}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
def upgrade():
    op.add_column('message_revisions', sa.Column('revision_fingerprint', sa.String(64), nullable=True))
    conn=op.get_bind()
    rows=conn.execute(sa.text('SELECT id, role, content_text, content_json, model, raw_timestamp FROM message_revisions')).mappings()
    for row in rows: conn.execute(sa.text('UPDATE message_revisions SET revision_fingerprint=:fp WHERE id=:id'), {'fp':_fp(row), 'id':row['id']})
    op.create_index('ix_message_revisions_revision_fingerprint','message_revisions',['revision_fingerprint'])
def downgrade():
    op.drop_index('ix_message_revisions_revision_fingerprint', table_name='message_revisions'); op.drop_column('message_revisions','revision_fingerprint')
