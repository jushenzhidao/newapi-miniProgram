"""新底座 P0：user_upstream 映射表 + users 网关密码三列

revision: 0002
down_revision: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- users：网关密码哈希 + 手机号验证元数据 ----
    op.add_column("users", sa.Column("password_hash", sa.String(128), server_default="", nullable=False,
                                     comment="bcrypt；网关/web 登录校验，明文不落盘"))
    op.add_column("users", sa.Column("phone_verified_at", sa.DateTime(), nullable=True,
                                     comment="微信授权/短信验证通过时间"))
    op.add_column("users", sa.Column("phone_source", sa.String(16), server_default="", nullable=False,
                                     comment="wechat_mp/sms/import"))

    # ---- user_upstream：手机号用户 ↔ new-api 影子账号 一对一映射 ----
    op.create_table(
        "user_upstream",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("upstream", sa.String(16), server_default="newapi", nullable=False),
        sa.Column("upstream_uid", sa.BigInteger(), nullable=False, comment="new-api users.id"),
        sa.Column("upstream_name", sa.String(64), nullable=False, comment="派生影子账号 mp<手机号>"),
        sa.Column("pat_enc", sa.String(256), server_default="", nullable=False,
                  comment="影子账号长期 PAT（Fernet 加密）"),
        sa.Column("quota_cache", sa.BigInteger(), server_default="0", nullable=False,
                  comment="余额展示缓存，非账本"),
        sa.Column("quota_cached_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "upstream", name="uq_user_upstream_user"),
        sa.UniqueConstraint("upstream", "upstream_uid", name="uq_user_upstream_uid"),
        sa.UniqueConstraint("upstream", "upstream_name", name="uq_user_upstream_name"),
        comment="BFF 手机号用户 ↔ new-api 影子账号映射（新底座核心表）",
    )
    op.create_index("ix_user_upstream_user_id", "user_upstream", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_upstream_user_id", table_name="user_upstream")
    op.drop_table("user_upstream")
    op.drop_column("users", "phone_source")
    op.drop_column("users", "phone_verified_at")
    op.drop_column("users", "password_hash")
