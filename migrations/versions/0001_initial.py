"""initial schema (users, sessions, channels, oauth_tokens, videos, settings)

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-05

Databases created by the app before Alembic was added are "stamped" at this revision
by app/migrations.py instead of running it.
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_approved", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "channels",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_channels_user_id", "channels", ["user_id"], unique=False)

    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)

    op.create_table(
        "videos",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.Column("blob_key", sa.Text(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_videos_channel_id", "videos", ["channel_id"], unique=False)
    op.create_index("ix_videos_created_at", "videos", ["created_at"], unique=False)
    op.create_index("ix_videos_status", "videos", ["status"], unique=False)
    op.create_index("ix_videos_user_id", "videos", ["user_id"], unique=False)

    op.create_table(
        "oauth_tokens",
        sa.Column("channel_id", sa.String(length=16), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("channel_id", "platform"),
    )
    op.create_index("ix_oauth_tokens_user_id", "oauth_tokens", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("oauth_tokens")
    op.drop_table("videos")
    op.drop_table("sessions")
    op.drop_table("channels")
    op.drop_table("users")
    op.drop_table("settings")
