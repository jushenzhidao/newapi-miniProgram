"""初始迁移：多小程序统一 BFF 全部表结构

revision: 0001
down_revision: None
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- users：手机号主键，游客态 phone 可空 ----
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("nickname", sa.String(64), server_default="微信用户", nullable=False),
        sa.Column("avatar_url", sa.String(512), server_default="", nullable=False),
        sa.Column("credits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        comment="用户：phone 跨小程序唯一（游客态为空）",
    )
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)

    # ---- user_identities：一个 user 对 N 个小程序的 openid ----
    op.create_table(
        "user_identities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("app_id", sa.String(32), nullable=False),
        sa.Column("openid", sa.String(64), nullable=False),
        sa.Column("unionid", sa.String(64), server_default="", nullable=False),
        sa.Column("session_key", sa.String(128), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", "openid", name="uq_identity_app_openid"),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])

    # ---- apps：多小程序注册表（新增小程序 = 插一行配置） ----
    op.create_table(
        "apps",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("app_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("secret", sa.String(128), server_default="", nullable=False),
        sa.Column("sign_key", sa.String(128), nullable=False),
        sa.Column("tools", sa.JSON(), server_default=sa.text("('[]')"), nullable=False),
        sa.Column("theme", sa.JSON(), server_default=sa.text("('{}')"), nullable=False),
        sa.Column("vpay_offer_id", sa.String(64), server_default="", nullable=False),
        sa.Column("status", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", name="uq_apps_app_id"),
    )

    # ---- tasks：AI 工具任务（通用，type 区分工具） ----
    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_no", sa.String(32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("app_id", sa.String(32), nullable=False),
        sa.Column("type", sa.String(32), server_default="cutout", nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("params", sa.JSON(), server_default=sa.text("('{}')"), nullable=False),
        sa.Column("result_url", sa.String(1024), server_default="", nullable=False),
        sa.Column("cost_credits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.String(512), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_no", name="uq_tasks_task_no"),
    )
    op.create_index("ix_tasks_user_id", "tasks", ["user_id"])
    op.create_index("ix_tasks_app_id", "tasks", ["app_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    # ---- credit_logs：积分流水（对账审计） ----
    op.create_table(
        "credit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("app_id", sa.String(32), server_default="", nullable=False),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("ref_type", sa.String(32), server_default="", nullable=False),
        sa.Column("ref_id", sa.String(64), server_default="", nullable=False),
        sa.Column("remark", sa.String(255), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_credit_logs_user_id", "credit_logs", ["user_id"])

    # ---- checkin_logs：每日签到（唯一约束保证幂等） ----
    op.create_table(
        "checkin_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("app_id", sa.String(32), server_default="", nullable=False),
        sa.Column("checkin_date", sa.Date(), nullable=False),
        sa.Column("credits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "checkin_date", name="uq_checkin_user_date"),
    )

    # ---- ad_reward_logs：激励视频奖励 ----
    op.create_table(
        "ad_reward_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("app_id", sa.String(32), server_default="", nullable=False),
        sa.Column("credits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ad_reward_logs_user_id", "ad_reward_logs", ["user_id"])

    # ---- vpay_orders：虚拟支付订单（二期；MVP 建表不开通） ----
    op.create_table(
        "vpay_orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_no", sa.String(32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("app_id", sa.String(32), server_default="", nullable=False),
        sa.Column("offer_id", sa.String(64), server_default="", nullable=False),
        sa.Column("amount", sa.Integer(), server_default="0", nullable=False),
        sa.Column("credits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(16), server_default="created", nullable=False),
        sa.Column("wx_order_id", sa.String(64), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_no", name="uq_vpay_orders_order_no"),
    )
    op.create_index("ix_vpay_orders_user_id", "vpay_orders", ["user_id"])


def downgrade() -> None:
    op.drop_table("vpay_orders")
    op.drop_table("ad_reward_logs")
    op.drop_table("checkin_logs")
    op.drop_table("credit_logs")
    op.drop_table("tasks")
    op.drop_table("apps")
    op.drop_table("user_identities")
    op.drop_table("users")
