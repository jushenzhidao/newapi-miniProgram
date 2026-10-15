"""P1：quota_grants 发放队列 + app_models 上架模型 + users.is_admin

revision: 0003
down_revision: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("0"), nullable=False,
                  comment="小程序管理台管理员"),
    )

    # ---- quota_grants：一切 add_quota 的唯一入口（幂等占位 + 补偿） ----
    op.create_table(
        "quota_grants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("app_id", sa.String(32), server_default="", nullable=False),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("ref_id", sa.String(64), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False, comment="积分语义（展示用）"),
        sa.Column("quota_value", sa.Integer(), nullable=False, comment="实际打到网关的 quota"),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("retries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(512), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ref_id", name="uq_quota_grants_ref"),
        comment="网关加额发放队列：占位->add_quota->synced，pending 由补偿 worker 重试",
    )
    op.create_index("ix_quota_grants_user_id", "quota_grants", ["user_id"])
    op.create_index("ix_quota_grants_status", "quota_grants", ["status"])

    # ---- app_models：上架模型（管理台配置，代码零写死） ----
    op.create_table(
        "app_models",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("model_name", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("ability", sa.String(16), server_default="t2v", nullable=False),
        sa.Column("price_credits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("remark", sa.String(255), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_name", name="uq_app_models_model_name"),
        comment="上架模型表：模型/定价/排序/上下架全由管理台配置",
    )


def downgrade() -> None:
    op.drop_table("app_models")
    op.drop_index("ix_quota_grants_status", table_name="quota_grants")
    op.drop_index("ix_quota_grants_user_id", table_name="quota_grants")
    op.drop_table("quota_grants")
    op.drop_column("users", "is_admin")
