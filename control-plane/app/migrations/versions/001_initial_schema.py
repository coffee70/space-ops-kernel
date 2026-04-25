"""Initial control-plane schema."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "managed_units",
        sa.Column("unit_id", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("package_owner", sa.String(length=64), nullable=False),
        sa.Column("runtime_kind", sa.String(length=64), nullable=False),
        sa.Column("runtime_template", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.String(length=1024), nullable=False),
        sa.Column("active_deployment_id", sa.String(length=64), nullable=True),
        sa.Column("deployment_status", sa.String(length=64), nullable=False),
        sa.Column("health_status", sa.String(length=64), nullable=False),
        sa.Column("discovery_metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "runtime_kind IN ('service', 'frontend_application', 'frontend_shell')",
            name="ck_managed_units_runtime_kind",
        ),
    )

    op.create_table(
        "deployments",
        sa.Column("deployment_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("unit_id", sa.String(length=255), sa.ForeignKey("managed_units.unit_id"), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("health_status", sa.String(length=64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("build_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("build_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("artifact_ref", sa.String(length=1024), nullable=True),
        sa.Column("runtime_ref", sa.JSON(), nullable=True),
    )
    op.create_index("ix_deployments_unit_id", "deployments", ["unit_id"])

    op.create_table(
        "deployment_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("deployment_id", sa.String(length=64), sa.ForeignKey("deployments.deployment_id"), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_deployment_events_deployment_id", "deployment_events", ["deployment_id"])

    op.create_table(
        "unit_health_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("unit_id", sa.String(length=255), sa.ForeignKey("managed_units.unit_id"), nullable=False),
        sa.Column("deployment_id", sa.String(length=64), sa.ForeignKey("deployments.deployment_id"), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_unit_health_snapshots_unit_id", "unit_health_snapshots", ["unit_id"])
    op.create_index("ix_unit_health_snapshots_deployment_id", "unit_health_snapshots", ["deployment_id"])

    op.create_table(
        "applications",
        sa.Column("application_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=320), nullable=False),
        sa.Column("icon_key", sa.String(length=64), nullable=False),
        sa.Column("icon_color", sa.String(length=64), nullable=False),
        sa.Column("icon_background", sa.String(length=64), nullable=False),
        sa.Column("application_type", sa.String(length=32), nullable=False),
        sa.Column("route_path", sa.String(length=255), nullable=False),
        sa.Column("loader_key", sa.String(length=64), nullable=True),
        sa.Column("embedded_url", sa.String(length=255), nullable=True),
        sa.Column("proxy_base_path", sa.String(length=255), nullable=True),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("iframe_sandbox", sa.String(length=255), nullable=True),
        sa.Column("iframe_allow", sa.String(length=255), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("owner", sa.String(length=120), nullable=True),
        sa.Column("health_status", sa.String(length=64), nullable=False, server_default="unknown"),
        sa.Column("deployment_status", sa.String(length=64), nullable=False, server_default="seeded"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "application_type IN ('native', 'embedded')",
            name="ck_applications_application_type",
        ),
        sa.CheckConstraint(
            "char_length(application_id) > 0",
            name="ck_applications_application_id_nonempty",
        ),
        sa.CheckConstraint(
            "route_path LIKE '/apps/%'",
            name="ck_applications_route_path_prefix",
        ),
    )
    op.create_index("ux_applications_route_path", "applications", ["route_path"], unique=True)
    op.create_index("ux_applications_proxy_base_path", "applications", ["proxy_base_path"], unique=True)

    op.create_table(
        "application_capabilities",
        sa.Column(
            "application_id",
            sa.String(length=64),
            sa.ForeignKey("applications.application_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("application_id", "capability"),
    )

    op.create_table(
        "application_deployments",
        sa.Column("deployment_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column(
            "application_id",
            sa.String(length=64),
            sa.ForeignKey("applications.application_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("artifact_ref", sa.String(length=1024), nullable=True),
        sa.Column("runtime_ref", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("health_status", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_application_deployments_application_id", "application_deployments", ["application_id"])

    op.create_table(
        "application_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "application_id",
            sa.String(length=64),
            sa.ForeignKey("applications.application_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_application_audit_events_application_id", "application_audit_events", ["application_id"])


def downgrade() -> None:
    op.drop_index("ix_application_audit_events_application_id", table_name="application_audit_events")
    op.drop_table("application_audit_events")

    op.drop_index("ix_application_deployments_application_id", table_name="application_deployments")
    op.drop_table("application_deployments")

    op.drop_table("application_capabilities")

    op.drop_index("ux_applications_proxy_base_path", table_name="applications")
    op.drop_index("ux_applications_route_path", table_name="applications")
    op.drop_table("applications")

    op.drop_index("ix_unit_health_snapshots_deployment_id", table_name="unit_health_snapshots")
    op.drop_index("ix_unit_health_snapshots_unit_id", table_name="unit_health_snapshots")
    op.drop_table("unit_health_snapshots")

    op.drop_index("ix_deployment_events_deployment_id", table_name="deployment_events")
    op.drop_table("deployment_events")

    op.drop_index("ix_deployments_unit_id", table_name="deployments")
    op.drop_table("deployments")

    op.drop_table("managed_units")
