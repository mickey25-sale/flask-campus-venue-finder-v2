"""Add secure administrator password recovery.

Revision ID: 20261111_0122
Revises: 75172f89b744
Create Date: 2026-11-11 01:22:00
"""
from alembic import op
import sqlalchemy as sa


revision = "20261111_0122"
down_revision = "75172f89b744"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "admins",
        sa.Column(
            "auth_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_table(
        "admin_password_reset_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["admin_id"], ["admins.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_password_reset_tokens_admin_id",
        "admin_password_reset_tokens",
        ["admin_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_password_reset_tokens_expires_at",
        "admin_password_reset_tokens",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_password_reset_tokens_token_hash",
        "admin_password_reset_tokens",
        ["token_hash"],
        unique=True,
    )


def downgrade():
    op.drop_index(
        "ix_admin_password_reset_tokens_token_hash",
        table_name="admin_password_reset_tokens",
    )
    op.drop_index(
        "ix_admin_password_reset_tokens_expires_at",
        table_name="admin_password_reset_tokens",
    )
    op.drop_index(
        "ix_admin_password_reset_tokens_admin_id",
        table_name="admin_password_reset_tokens",
    )
    op.drop_table("admin_password_reset_tokens")
    op.drop_column("admins", "auth_version")
