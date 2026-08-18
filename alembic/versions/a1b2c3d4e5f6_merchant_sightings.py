"""merchant sightings: payees that have a name but no account number

Revision ID: a1b2c3d4e5f6
Revises: d4f6367271f9
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "d4f6367271f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant_sightings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("probe", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("merchant_name", sa.String(length=200), nullable=False),
        sa.Column("page_url", sa.String(length=500), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["collection_runs.id"]),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_merchant_sightings_site_id", "merchant_sightings", ["site_id"])
    op.create_index("ix_merchant_sightings_run_id", "merchant_sightings", ["run_id"])
    op.create_index("ix_merchant_sightings_probe", "merchant_sightings", ["probe"])
    op.create_index("ix_merchant_sightings_channel", "merchant_sightings", ["channel"])
    op.create_index("ix_merchant_sightings_merchant_name", "merchant_sightings", ["merchant_name"])
    op.create_index("ix_merchant_sightings_seen_at", "merchant_sightings", ["seen_at"])
    op.create_index("ix_merchant_name_time", "merchant_sightings", ["merchant_name", "seen_at"])


def downgrade() -> None:
    op.drop_table("merchant_sightings")
