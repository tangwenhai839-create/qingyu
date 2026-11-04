"""bigram GIN index for evidence text search

Revision ID: 0017_evidence_search_bigram
Revises: 0016_real_export_evidence

Chinese substring search cannot depend on pg_trgm alone. On the postgres:16
locale (en_US.utf8), a 2-character ILIKE such as '%运维%' does not use a
trigram GIN index: the planner sequential-scans, and forcing the index returns
essentially every row for the recheck to discard. pg_trgm's gram extraction
follows LC_CTYPE, so those two-character queries never become a selective
index condition.

public.qingyu_text_bigrams(text) is plain PostgreSQL. It stores every
consecutive pair of case-folded characters. A GIN index on that expression
supports ``bigrams(content_text) @> bigrams(query)`` for a two-character
query, and the same predicate is a correct prefilter for longer queries.
Search still requires strpos(lower(content_text), lower(query)) > 0 so a pair
that occurs out of order cannot become a hit. No extension is required.

The index is built with CONCURRENTLY so an upgrade does not take an
ACCESS EXCLUSIVE lock for the whole build. Downgrade drops that index and
the function. It does not delete message text.
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_evidence_search_bigram"
down_revision = "0016_real_export_evidence"
branch_labels = None
depends_on = None

_FUNCTION = sa.text(
    """
    CREATE OR REPLACE FUNCTION public.qingyu_text_bigrams(t text)
    RETURNS text[]
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
    STRICT
    SET search_path = pg_catalog
    AS $qingyu$
      SELECT ARRAY(
        SELECT DISTINCT pg_catalog.substr(pg_catalog.lower(t), gs.i, 2)
        FROM pg_catalog.generate_series(1, pg_catalog.length(t) - 1) AS gs(i)
      );
    $qingyu$
    """
)

_INDEX = "ix_message_revisions_content_bigrams"


def upgrade() -> None:
    op.execute(_FUNCTION)
    with op.get_context().autocommit_block():
        # A failed CONCURRENTLY build can leave an invalid index. Drop that
        # leftover before creating the index again. The migration runs once
        # on success, so this does not drop a live index on later upgrades.
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
        op.execute(
            sa.text(
                f"CREATE INDEX CONCURRENTLY {_INDEX} "
                "ON message_revisions USING gin (public.qingyu_text_bigrams(content_text))"
            )
        )


def downgrade() -> None:
    # Not CONCURRENTLY. A later revision in the same downgrade command can fail
    # closed and roll this transaction back. A concurrent drop would already
    # have committed, leaving head without its index.
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX}"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS public.qingyu_text_bigrams(text)"))
