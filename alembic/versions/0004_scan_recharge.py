"""P2：扫码登录会话 + 充值订单（补全 BFF 缺失端点）。

revision: 0004
down_revision: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- wx_scan_sessions：PC 扫码登录状态机（state 由 BFF 生成防伪造） ----
    op.create_table(
        "wx_scan_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(32), nullable=False),
        sa.Column("scene", sa.String(32), server_default="login", nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("wx_openid", sa.String(64), server_default="", nullable=False),
        sa.Column("expire_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state", name="uq_wx_scan_sessions_state"),
        comment="PC 扫码登录会话：pending->scanned->confirmed/cancelled/expired",
    )
    op.create_index("ix_wx_scan_sessions_state", "wx_scan_sessions", ["state"])
    op.create_index("ix_wx_scan_sessions_status", "wx_scan_sessions", ["status"])
    op.create_index("ix_wx_scan_sessions_user_id", "wx_scan_sessions", ["user_id"])

    # ---- recharge_orders：安卓 JSAPI / iOS 引导统一订单表 ----
    op.create_table(
        "recharge_orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_no", sa.String(32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("app_id", sa.String(32), server_default="", nullable=False),
        sa.Column("platform", sa.String(16), server_default="android", nullable=False),
        sa.Column("amount_fen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("credits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(16), server_default="created", nullable=False),
        sa.Column("wx_order_id", sa.String(64), server_default="", nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_no", name="uq_recharge_orders_no"),
        comment="充值订单：created->paid->syncing->synced，配额发放走 quota_grants 幂等闸门",
    )
    op.create_index("ix_recharge_orders_no", "recharge_orders", ["order_no"])
    op.create_index("ix_recharge_orders_user_id", "recharge_orders", ["user_id"])
    op.create_index("ix_recharge_orders_status", "recharge_orders", ["status"])


def downgrade() -> None:
    op.drop_table("recharge_orders")
    op.drop_index("ix_wx_scan_sessions_user_id", table_name="wx_scan_sessions")
    op.drop_index("ix_wx_scan_sessions_status", table_name="wx_scan_sessions")
    op.drop_index("ix_wx_scan_sessions_state", table_name="wx_scan_sessions")
    op.drop_table("wx_scan_sessions")
