"""Core schema: the complete AgentTape data model at its first release.

Revision ID: 0001_core
Revises:
Create Date: 2026-09-23 13:05:39.619873
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import agenttape.infrastructure.db.base

revision: str = "0001_core"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column(
            "value",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_settings")),
    )
    op.create_table(
        "scheduler_leases",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=200), nullable=False),
        sa.Column(
            "acquired_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "renewed_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_scheduler_leases")),
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("active_count", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_heartbeat_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "stopped_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "info",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("worker_id", name=op.f("pk_worker_heartbeats")),
    )
    with op.batch_alter_table("worker_heartbeats", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_worker_heartbeats_last_heartbeat_at"),
            ["last_heartbeat_at"],
            unique=False,
        )

    op.create_table(
        "workspaces",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "settings",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("slug", name=op.f("uq_workspaces_slug")),
    )
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_workspaces_is_demo"), ["is_demo"], unique=False)

    op.create_table(
        "audit_entries",
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column(
            "ts", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=False
        ),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column(
            "details",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_audit_entries_workspace_id_workspaces"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_entries")),
    )
    with op.batch_alter_table("audit_entries", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_audit_entries_action"), ["action"], unique=False)
        batch_op.create_index(
            "ix_audit_entries_resource", ["resource_type", "resource_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_audit_entries_run_id"), ["run_id"], unique=False)
        batch_op.create_index("ix_audit_entries_ts", ["ts"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_audit_entries_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "evaluation_definitions",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("evaluator_type", sa.String(length=64), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_evaluation_definitions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_definitions")),
        sa.UniqueConstraint(
            "workspace_id", "name", name=op.f("uq_evaluation_definitions_workspace_id_name")
        ),
    )
    with op.batch_alter_table("evaluation_definitions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_evaluation_definitions_is_demo"), ["is_demo"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_evaluation_definitions_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "evaluation_suites",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "members",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_evaluation_suites_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_suites")),
        sa.UniqueConstraint(
            "workspace_id", "name", name=op.f("uq_evaluation_suites_workspace_id_name")
        ),
    )
    with op.batch_alter_table("evaluation_suites", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_evaluation_suites_is_demo"), ["is_demo"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_evaluation_suites_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "projects",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("repo_path", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=True),
        sa.Column(
            "tags",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column(
            "settings",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "git_state",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "git_checked_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_projects_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_projects_workspace_id_name")),
    )
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_projects_archived"), ["archived"], unique=False)
        batch_op.create_index(batch_op.f("ix_projects_is_demo"), ["is_demo"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_projects_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "secret_references",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("locator", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "last_resolved_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_secret_references_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secret_references")),
        sa.UniqueConstraint(
            "workspace_id", "name", name=op.f("uq_secret_references_workspace_id_name")
        ),
    )
    with op.batch_alter_table("secret_references", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_secret_references_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "agent_profiles",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("adapter_type", sa.String(length=64), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column(
            "tags",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_agent_profiles_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_agent_profiles_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_profiles")),
        sa.UniqueConstraint(
            "workspace_id", "name", name=op.f("uq_agent_profiles_workspace_id_name")
        ),
    )
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_agent_profiles_adapter_type"), ["adapter_type"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_agent_profiles_archived"), ["archived"], unique=False)
        batch_op.create_index(batch_op.f("ix_agent_profiles_is_demo"), ["is_demo"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_agent_profiles_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_agent_profiles_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "evaluation_definition_versions",
        sa.Column("definition_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "config",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "locked_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["definition_id"],
            ["evaluation_definitions.id"],
            name=op.f("fk_evaluation_definition_versions_definition_id_evaluation_definitions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_definition_versions")),
        sa.UniqueConstraint(
            "definition_id",
            "version",
            name=op.f("uq_evaluation_definition_versions_definition_id_version"),
        ),
    )
    with op.batch_alter_table("evaluation_definition_versions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_evaluation_definition_versions_definition_id"),
            ["definition_id"],
            unique=False,
        )

    op.create_table(
        "prompt_templates",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("owner", sa.String(length=200), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("duplicated_from_id", sa.String(length=36), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["duplicated_from_id"],
            ["prompt_templates.id"],
            name=op.f("fk_prompt_templates_duplicated_from_id_prompt_templates"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_prompt_templates_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_prompt_templates_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_templates")),
        sa.UniqueConstraint(
            "workspace_id", "name", name=op.f("uq_prompt_templates_workspace_id_name")
        ),
    )
    with op.batch_alter_table("prompt_templates", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_prompt_templates_archived"), ["archived"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_prompt_templates_duplicated_from_id"),
            ["duplicated_from_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_prompt_templates_is_demo"), ["is_demo"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_prompt_templates_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_prompt_templates_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "workflows",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_workflows_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workflows_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflows")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_workflows_workspace_id_name")),
    )
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_workflows_archived"), ["archived"], unique=False)
        batch_op.create_index(batch_op.f("ix_workflows_is_demo"), ["is_demo"], unique=False)
        batch_op.create_index(batch_op.f("ix_workflows_project_id"), ["project_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_workflows_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "agent_profile_versions",
        sa.Column("profile_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("adapter_type", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "config",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "locked_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["agent_profiles.id"],
            name=op.f("fk_agent_profile_versions_profile_id_agent_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_profile_versions")),
        sa.UniqueConstraint(
            "profile_id", "version", name=op.f("uq_agent_profile_versions_profile_id_version")
        ),
    )
    with op.batch_alter_table("agent_profile_versions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_agent_profile_versions_profile_id"), ["profile_id"], unique=False
        )

    op.create_table(
        "prompt_versions",
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "variables",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "settings",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "locked_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_prompt_versions_template_id_prompt_templates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_versions")),
        sa.UniqueConstraint(
            "template_id", "version", name=op.f("uq_prompt_versions_template_id_version")
        ),
    )
    with op.batch_alter_table("prompt_versions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_prompt_versions_template_id"), ["template_id"], unique=False
        )

    op.create_table(
        "workflow_versions",
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "definition",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "input_schema",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "locked_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_workflow_versions_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_versions")),
        sa.UniqueConstraint(
            "workflow_id", "version", name=op.f("uq_workflow_versions_workflow_id_version")
        ),
    )
    with op.batch_alter_table("workflow_versions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_workflow_versions_workflow_id"), ["workflow_id"], unique=False
        )

    op.create_table(
        "schedules",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("prompt_template_id", sa.String(length=36), nullable=True),
        sa.Column("prompt_version_id", sa.String(length=36), nullable=True),
        sa.Column("agent_profile_id", sa.String(length=36), nullable=True),
        sa.Column("workflow_id", sa.String(length=36), nullable=True),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=True),
        sa.Column(
            "inputs",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("cron_expression", sa.String(length=200), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "run_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "start_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "end_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column("misfire_policy", sa.String(length=16), nullable=False),
        sa.Column("concurrency_policy", sa.String(length=32), nullable=False),
        sa.Column("max_queued", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column(
            "next_run_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_run_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_run_id", sa.String(length=36), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_profile_id"],
            ["agent_profiles.id"],
            name=op.f("fk_schedules_agent_profile_id_agent_profiles"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_schedules_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_schedules_prompt_template_id_prompt_templates"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
            name=op.f("fk_schedules_prompt_version_id_prompt_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_schedules_workflow_id_workflows"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name=op.f("fk_schedules_workflow_version_id_workflow_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_schedules_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedules")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_schedules_workspace_id_name")),
    )
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_schedules_agent_profile_id"), ["agent_profile_id"], unique=False
        )
        batch_op.create_index(
            "ix_schedules_enabled_next_run_at", ["enabled", "next_run_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_schedules_is_demo"), ["is_demo"], unique=False)
        batch_op.create_index(batch_op.f("ix_schedules_project_id"), ["project_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_schedules_prompt_template_id"), ["prompt_template_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_schedules_prompt_version_id"), ["prompt_version_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_schedules_workflow_id"), ["workflow_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_schedules_workflow_version_id"), ["workflow_version_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_schedules_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "triggers",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("prompt_template_id", sa.String(length=36), nullable=True),
        sa.Column("prompt_version_id", sa.String(length=36), nullable=True),
        sa.Column("agent_profile_id", sa.String(length=36), nullable=True),
        sa.Column("workflow_id", sa.String(length=36), nullable=True),
        sa.Column("token_hash", sa.String(length=128), nullable=True),
        sa.Column("token_prefix", sa.String(length=32), nullable=True),
        sa.Column(
            "input_mapping",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key_path", sa.String(length=200), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("max_payload_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "config",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "last_delivery_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_profile_id"],
            ["agent_profiles.id"],
            name=op.f("fk_triggers_agent_profile_id_agent_profiles"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_triggers_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_triggers_prompt_template_id_prompt_templates"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
            name=op.f("fk_triggers_prompt_version_id_prompt_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_triggers_workflow_id_workflows"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_triggers_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_triggers")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_triggers_workspace_id_name")),
    )
    with op.batch_alter_table("triggers", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_triggers_agent_profile_id"), ["agent_profile_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_triggers_is_demo"), ["is_demo"], unique=False)
        batch_op.create_index(batch_op.f("ix_triggers_project_id"), ["project_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_triggers_prompt_template_id"), ["prompt_template_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_triggers_prompt_version_id"), ["prompt_version_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_triggers_workflow_id"), ["workflow_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_triggers_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "workflow_edges",
        sa.Column("workflow_version_id", sa.String(length=36), nullable=False),
        sa.Column("from_node_key", sa.String(length=100), nullable=False),
        sa.Column("to_node_key", sa.String(length=100), nullable=False),
        sa.Column(
            "condition",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name=op.f("fk_workflow_edges_workflow_version_id_workflow_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_edges")),
        sa.UniqueConstraint(
            "workflow_version_id",
            "from_node_key",
            "to_node_key",
            name=op.f("uq_workflow_edges_workflow_version_id_from_node_key_to_node_key"),
        ),
    )
    with op.batch_alter_table("workflow_edges", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_workflow_edges_workflow_version_id"),
            ["workflow_version_id"],
            unique=False,
        )

    op.create_table(
        "workflow_nodes",
        sa.Column("workflow_version_id", sa.String(length=36), nullable=False),
        sa.Column("node_key", sa.String(length=100), nullable=False),
        sa.Column("node_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "config",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "input_schema",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "output_schema",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("timeout_seconds", sa.Float(), nullable=True),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "retry",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("continue_on_failure", sa.Boolean(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column(
            "position",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name=op.f("fk_workflow_nodes_workflow_version_id_workflow_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_nodes")),
        sa.UniqueConstraint(
            "workflow_version_id",
            "node_key",
            name=op.f("uq_workflow_nodes_workflow_version_id_node_key"),
        ),
    )
    with op.batch_alter_table("workflow_nodes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_workflow_nodes_workflow_version_id"),
            ["workflow_version_id"],
            unique=False,
        )

    op.create_table(
        "runs",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_category", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("adapter_type", sa.String(length=64), nullable=True),
        sa.Column("prompt_template_id", sa.String(length=36), nullable=True),
        sa.Column("prompt_version_id", sa.String(length=36), nullable=True),
        sa.Column("agent_profile_id", sa.String(length=36), nullable=True),
        sa.Column("agent_profile_version_id", sa.String(length=36), nullable=True),
        sa.Column("workflow_id", sa.String(length=36), nullable=True),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=True),
        sa.Column("schedule_id", sa.String(length=36), nullable=True),
        sa.Column("trigger_id", sa.String(length=36), nullable=True),
        sa.Column("parent_run_id", sa.String(length=36), nullable=True),
        sa.Column("parent_relation", sa.String(length=32), nullable=True),
        sa.Column("parent_node_key", sa.String(length=100), nullable=True),
        sa.Column(
            "inputs",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("rendered_prompt", sa.Text(), nullable=True),
        sa.Column(
            "snapshot",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "options",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "outputs",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "tags",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("queue_generation", sa.Integer(), nullable=False),
        sa.Column(
            "queued_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "started_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "finished_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "cancel_requested_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column(
            "usage",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "cost",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "budget",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_profile_id"],
            ["agent_profiles.id"],
            name=op.f("fk_runs_agent_profile_id_agent_profiles"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["agent_profile_version_id"],
            ["agent_profile_versions.id"],
            name=op.f("fk_runs_agent_profile_version_id_agent_profile_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["runs.id"],
            name=op.f("fk_runs_parent_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_runs_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_template_id"],
            ["prompt_templates.id"],
            name=op.f("fk_runs_prompt_template_id_prompt_templates"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
            name=op.f("fk_runs_prompt_version_id_prompt_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedules.id"],
            name=op.f("fk_runs_schedule_id_schedules"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_id"],
            ["triggers.id"],
            name=op.f("fk_runs_trigger_id_triggers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_runs_workflow_id_workflows"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name=op.f("fk_runs_workflow_version_id_workflow_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_runs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint(
            "source", "idempotency_key", name=op.f("uq_runs_source_idempotency_key")
        ),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runs_adapter_type"), ["adapter_type"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_runs_agent_profile_id"), ["agent_profile_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_runs_agent_profile_version_id"),
            ["agent_profile_version_id"],
            unique=False,
        )
        batch_op.create_index("ix_runs_created_at", ["created_at"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_runs_failure_category"), ["failure_category"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_runs_is_demo"), ["is_demo"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_parent_run_id"), ["parent_run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_project_id"), ["project_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_runs_prompt_template_id"), ["prompt_template_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_runs_prompt_version_id"), ["prompt_version_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_runs_schedule_id"), ["schedule_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_source"), ["source"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_status"), ["status"], unique=False)
        batch_op.create_index("ix_runs_status_created_at", ["status", "created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_trigger_id"), ["trigger_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_workflow_id"), ["workflow_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_runs_workflow_version_id"), ["workflow_version_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_runs_workspace_id"), ["workspace_id"], unique=False)

    op.create_table(
        "queue_items",
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("queue_name", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "payload",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "enqueued_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column(
            "lease_expires_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("claim_count", sa.Integer(), nullable=False),
        sa.Column(
            "last_claimed_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "completed_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_queue_items_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_items")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_queue_items_dedupe_key")),
    )
    with op.batch_alter_table("queue_items", schema=None) as batch_op:
        batch_op.create_index(
            "ix_queue_items_claim",
            ["status", "priority", "available_at", "enqueued_at"],
            unique=False,
        )
        batch_op.create_index("ix_queue_items_lease", ["status", "lease_expires_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_queue_items_run_id"), ["run_id"], unique=False)

    op.create_table(
        "run_attempts",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_category", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.String(length=200), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("workdir", sa.Text(), nullable=True),
        sa.Column("log_dir", sa.Text(), nullable=True),
        sa.Column("stdout_bytes", sa.Integer(), nullable=False),
        sa.Column("stderr_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "checkpoint",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "outputs",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "usage",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "cost",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "started_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "finished_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "heartbeat_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "lease_expires_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "retry_scheduled_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_attempts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_attempts")),
        sa.UniqueConstraint(
            "run_id", "attempt_number", name=op.f("uq_run_attempts_run_id_attempt_number")
        ),
    )
    with op.batch_alter_table("run_attempts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_attempts_run_id"), ["run_id"], unique=False)
        batch_op.create_index(
            "ix_run_attempts_status_lease", ["status", "lease_expires_at"], unique=False
        )

    op.create_table(
        "schedule_occurrences",
        sa.Column("schedule_id", sa.String(length=36), nullable=False),
        sa.Column(
            "occurrence_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_schedule_occurrences_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedules.id"],
            name=op.f("fk_schedule_occurrences_schedule_id_schedules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule_occurrences")),
        sa.UniqueConstraint(
            "schedule_id",
            "occurrence_at",
            name=op.f("uq_schedule_occurrences_schedule_id_occurrence_at"),
        ),
    )
    with op.batch_alter_table("schedule_occurrences", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_schedule_occurrences_run_id"), ["run_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_schedule_occurrences_schedule_id"), ["schedule_id"], unique=False
        )

    op.create_table(
        "step_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("node_key", sa.String(length=100), nullable=False),
        sa.Column("node_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("child_run_id", sa.String(length=36), nullable=True),
        sa.Column("approval_request_id", sa.String(length=36), nullable=True),
        sa.Column(
            "inputs",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "outputs",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "state",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("failure_category", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "finished_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["child_run_id"],
            ["runs.id"],
            name=op.f("fk_step_runs_child_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_step_runs_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_step_runs")),
        sa.UniqueConstraint("run_id", "node_key", name=op.f("uq_step_runs_run_id_node_key")),
    )
    with op.batch_alter_table("step_runs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_step_runs_child_run_id"), ["child_run_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_step_runs_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_step_runs_status"), ["status"], unique=False)

    op.create_table(
        "trigger_deliveries",
        sa.Column("trigger_id", sa.String(length=36), nullable=False),
        sa.Column(
            "received_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("payload_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "metadata",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_trigger_deliveries_run_id_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_id"],
            ["triggers.id"],
            name=op.f("fk_trigger_deliveries_trigger_id_triggers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trigger_deliveries")),
        sa.UniqueConstraint(
            "trigger_id",
            "idempotency_key",
            name=op.f("uq_trigger_deliveries_trigger_id_idempotency_key"),
        ),
    )
    with op.batch_alter_table("trigger_deliveries", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_trigger_deliveries_run_id"), ["run_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_trigger_deliveries_trigger_id"), ["trigger_id"], unique=False
        )

    op.create_table(
        "approval_requests",
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("step_run_id", sa.String(length=36), nullable=True),
        sa.Column("node_key", sa.String(length=100), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column(
            "requested_action",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resume_handler", sa.String(length=64), nullable=False),
        sa.Column("requester", sa.String(length=200), nullable=False),
        sa.Column("reviewer", sa.String(length=200), nullable=True),
        sa.Column("reviewer_note", sa.Text(), nullable=True),
        sa.Column(
            "expires_at", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "resolved_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_approval_requests_attempt_id_run_attempts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_approval_requests_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_run_id"],
            ["step_runs.id"],
            name=op.f("fk_approval_requests_step_run_id_step_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_approval_requests_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_requests")),
    )
    with op.batch_alter_table("approval_requests", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_approval_requests_attempt_id"), ["attempt_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_approval_requests_is_demo"), ["is_demo"], unique=False)
        batch_op.create_index(batch_op.f("ix_approval_requests_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_approval_requests_status"), ["status"], unique=False)
        batch_op.create_index(
            "ix_approval_requests_status_expires_at", ["status", "expires_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_approval_requests_step_run_id"), ["step_run_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_approval_requests_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "artifacts",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("step_run_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("is_text", sa.Boolean(), nullable=False),
        sa.Column(
            "metadata",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_artifacts_attempt_id_run_attempts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_artifacts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["step_run_id"],
            ["step_runs.id"],
            name=op.f("fk_artifacts_step_run_id_step_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
    )
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_artifacts_attempt_id"), ["attempt_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_artifacts_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_artifacts_step_run_id"), ["step_run_id"], unique=False)

    op.create_table(
        "evaluation_results",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("step_run_id", sa.String(length=36), nullable=True),
        sa.Column("definition_id", sa.String(length=36), nullable=True),
        sa.Column("definition_version_id", sa.String(length=36), nullable=True),
        sa.Column("suite_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("evaluator_type", sa.String(length=64), nullable=False),
        sa.Column("evaluator_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column(
            "evidence",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column(
            "config",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("rerun_of_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_evaluation_results_attempt_id_run_attempts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["definition_id"],
            ["evaluation_definitions.id"],
            name=op.f("fk_evaluation_results_definition_id_evaluation_definitions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["definition_version_id"],
            ["evaluation_definition_versions.id"],
            name=op.f("fk_evaluation_results_definition_version_id_evaluation_definition_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_evaluation_results_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_run_id"],
            ["step_runs.id"],
            name=op.f("fk_evaluation_results_step_run_id_step_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["suite_id"],
            ["evaluation_suites.id"],
            name=op.f("fk_evaluation_results_suite_id_evaluation_suites"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_results")),
    )
    with op.batch_alter_table("evaluation_results", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_evaluation_results_attempt_id"), ["attempt_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_evaluation_results_definition_id"), ["definition_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_evaluation_results_definition_version_id"),
            ["definition_version_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_evaluation_results_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_evaluation_results_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_evaluation_results_step_run_id"), ["step_run_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_evaluation_results_suite_id"), ["suite_id"], unique=False
        )

    op.create_table(
        "events",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("step_run_id", sa.String(length=36), nullable=True),
        sa.Column("node_key", sa.String(length=100), nullable=True),
        sa.Column("parent_event_id", sa.String(length=36), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "ts", agenttape.infrastructure.db.base.UTCDateTime(timezone=True), nullable=False
        ),
        sa.Column("monotonic_ms", sa.Float(), nullable=False),
        sa.Column("type", sa.String(length=48), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "redaction",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "usage",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "cost",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_events_attempt_id_run_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_events_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["step_run_id"],
            ["step_runs.id"],
            name=op.f("fk_events_step_run_id_step_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("run_id", "sequence", name=op.f("uq_events_run_id_sequence")),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_events_attempt_id"), ["attempt_id"], unique=False)
        batch_op.create_index("ix_events_run_id_type", ["run_id", "type"], unique=False)
        batch_op.create_index(batch_op.f("ix_events_severity"), ["severity"], unique=False)
        batch_op.create_index(batch_op.f("ix_events_step_run_id"), ["step_run_id"], unique=False)

    op.create_table(
        "file_changes",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("step_run_id", sa.String(length=36), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("old_path", sa.Text(), nullable=True),
        sa.Column("change_type", sa.String(length=16), nullable=False),
        sa.Column("insertions", sa.Integer(), nullable=True),
        sa.Column("deletions", sa.Integer(), nullable=True),
        sa.Column("is_binary", sa.Boolean(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("is_protected", sa.Boolean(), nullable=False),
        sa.Column("diff_artifact_id", sa.String(length=36), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            agenttape.infrastructure.db.base.UTCDateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_file_changes_attempt_id_run_attempts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["diff_artifact_id"],
            ["artifacts.id"],
            name=op.f("fk_file_changes_diff_artifact_id_artifacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_file_changes_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["step_run_id"],
            ["step_runs.id"],
            name=op.f("fk_file_changes_step_run_id_step_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_changes")),
    )
    with op.batch_alter_table("file_changes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_file_changes_attempt_id"), ["attempt_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_file_changes_diff_artifact_id"), ["diff_artifact_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_file_changes_run_id"), ["run_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_file_changes_step_run_id"), ["step_run_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("file_changes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_file_changes_step_run_id"))
        batch_op.drop_index(batch_op.f("ix_file_changes_run_id"))
        batch_op.drop_index(batch_op.f("ix_file_changes_diff_artifact_id"))
        batch_op.drop_index(batch_op.f("ix_file_changes_attempt_id"))

    op.drop_table("file_changes")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_events_step_run_id"))
        batch_op.drop_index(batch_op.f("ix_events_severity"))
        batch_op.drop_index("ix_events_run_id_type")
        batch_op.drop_index(batch_op.f("ix_events_attempt_id"))

    op.drop_table("events")
    with op.batch_alter_table("evaluation_results", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_evaluation_results_suite_id"))
        batch_op.drop_index(batch_op.f("ix_evaluation_results_step_run_id"))
        batch_op.drop_index(batch_op.f("ix_evaluation_results_status"))
        batch_op.drop_index(batch_op.f("ix_evaluation_results_run_id"))
        batch_op.drop_index(batch_op.f("ix_evaluation_results_definition_version_id"))
        batch_op.drop_index(batch_op.f("ix_evaluation_results_definition_id"))
        batch_op.drop_index(batch_op.f("ix_evaluation_results_attempt_id"))

    op.drop_table("evaluation_results")
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifacts_step_run_id"))
        batch_op.drop_index(batch_op.f("ix_artifacts_run_id"))
        batch_op.drop_index(batch_op.f("ix_artifacts_attempt_id"))

    op.drop_table("artifacts")
    with op.batch_alter_table("approval_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_approval_requests_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_approval_requests_step_run_id"))
        batch_op.drop_index("ix_approval_requests_status_expires_at")
        batch_op.drop_index(batch_op.f("ix_approval_requests_status"))
        batch_op.drop_index(batch_op.f("ix_approval_requests_run_id"))
        batch_op.drop_index(batch_op.f("ix_approval_requests_is_demo"))
        batch_op.drop_index(batch_op.f("ix_approval_requests_attempt_id"))

    op.drop_table("approval_requests")
    with op.batch_alter_table("trigger_deliveries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_trigger_deliveries_trigger_id"))
        batch_op.drop_index(batch_op.f("ix_trigger_deliveries_run_id"))

    op.drop_table("trigger_deliveries")
    with op.batch_alter_table("step_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_step_runs_status"))
        batch_op.drop_index(batch_op.f("ix_step_runs_run_id"))
        batch_op.drop_index(batch_op.f("ix_step_runs_child_run_id"))

    op.drop_table("step_runs")
    with op.batch_alter_table("schedule_occurrences", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_schedule_occurrences_schedule_id"))
        batch_op.drop_index(batch_op.f("ix_schedule_occurrences_run_id"))

    op.drop_table("schedule_occurrences")
    with op.batch_alter_table("run_attempts", schema=None) as batch_op:
        batch_op.drop_index("ix_run_attempts_status_lease")
        batch_op.drop_index(batch_op.f("ix_run_attempts_run_id"))

    op.drop_table("run_attempts")
    with op.batch_alter_table("queue_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_queue_items_run_id"))
        batch_op.drop_index("ix_queue_items_lease")
        batch_op.drop_index("ix_queue_items_claim")

    op.drop_table("queue_items")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_runs_workflow_version_id"))
        batch_op.drop_index(batch_op.f("ix_runs_workflow_id"))
        batch_op.drop_index(batch_op.f("ix_runs_trigger_id"))
        batch_op.drop_index("ix_runs_status_created_at")
        batch_op.drop_index(batch_op.f("ix_runs_status"))
        batch_op.drop_index(batch_op.f("ix_runs_source"))
        batch_op.drop_index(batch_op.f("ix_runs_schedule_id"))
        batch_op.drop_index(batch_op.f("ix_runs_prompt_version_id"))
        batch_op.drop_index(batch_op.f("ix_runs_prompt_template_id"))
        batch_op.drop_index(batch_op.f("ix_runs_project_id"))
        batch_op.drop_index(batch_op.f("ix_runs_parent_run_id"))
        batch_op.drop_index(batch_op.f("ix_runs_kind"))
        batch_op.drop_index(batch_op.f("ix_runs_is_demo"))
        batch_op.drop_index(batch_op.f("ix_runs_failure_category"))
        batch_op.drop_index("ix_runs_created_at")
        batch_op.drop_index(batch_op.f("ix_runs_agent_profile_version_id"))
        batch_op.drop_index(batch_op.f("ix_runs_agent_profile_id"))
        batch_op.drop_index(batch_op.f("ix_runs_adapter_type"))

    op.drop_table("runs")
    with op.batch_alter_table("workflow_nodes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_workflow_nodes_workflow_version_id"))

    op.drop_table("workflow_nodes")
    with op.batch_alter_table("workflow_edges", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_workflow_edges_workflow_version_id"))

    op.drop_table("workflow_edges")
    with op.batch_alter_table("triggers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_triggers_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_triggers_workflow_id"))
        batch_op.drop_index(batch_op.f("ix_triggers_prompt_version_id"))
        batch_op.drop_index(batch_op.f("ix_triggers_prompt_template_id"))
        batch_op.drop_index(batch_op.f("ix_triggers_project_id"))
        batch_op.drop_index(batch_op.f("ix_triggers_is_demo"))
        batch_op.drop_index(batch_op.f("ix_triggers_agent_profile_id"))

    op.drop_table("triggers")
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_schedules_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_schedules_workflow_version_id"))
        batch_op.drop_index(batch_op.f("ix_schedules_workflow_id"))
        batch_op.drop_index(batch_op.f("ix_schedules_prompt_version_id"))
        batch_op.drop_index(batch_op.f("ix_schedules_prompt_template_id"))
        batch_op.drop_index(batch_op.f("ix_schedules_project_id"))
        batch_op.drop_index(batch_op.f("ix_schedules_is_demo"))
        batch_op.drop_index("ix_schedules_enabled_next_run_at")
        batch_op.drop_index(batch_op.f("ix_schedules_agent_profile_id"))

    op.drop_table("schedules")
    with op.batch_alter_table("workflow_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_workflow_versions_workflow_id"))

    op.drop_table("workflow_versions")
    with op.batch_alter_table("prompt_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_prompt_versions_template_id"))

    op.drop_table("prompt_versions")
    with op.batch_alter_table("agent_profile_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_agent_profile_versions_profile_id"))

    op.drop_table("agent_profile_versions")
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_workflows_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_workflows_project_id"))
        batch_op.drop_index(batch_op.f("ix_workflows_is_demo"))
        batch_op.drop_index(batch_op.f("ix_workflows_archived"))

    op.drop_table("workflows")
    with op.batch_alter_table("prompt_templates", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_prompt_templates_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_prompt_templates_project_id"))
        batch_op.drop_index(batch_op.f("ix_prompt_templates_is_demo"))
        batch_op.drop_index(batch_op.f("ix_prompt_templates_duplicated_from_id"))
        batch_op.drop_index(batch_op.f("ix_prompt_templates_archived"))

    op.drop_table("prompt_templates")
    with op.batch_alter_table("evaluation_definition_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_evaluation_definition_versions_definition_id"))

    op.drop_table("evaluation_definition_versions")
    with op.batch_alter_table("agent_profiles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_agent_profiles_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_agent_profiles_project_id"))
        batch_op.drop_index(batch_op.f("ix_agent_profiles_is_demo"))
        batch_op.drop_index(batch_op.f("ix_agent_profiles_archived"))
        batch_op.drop_index(batch_op.f("ix_agent_profiles_adapter_type"))

    op.drop_table("agent_profiles")
    with op.batch_alter_table("secret_references", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_secret_references_workspace_id"))

    op.drop_table("secret_references")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projects_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_projects_is_demo"))
        batch_op.drop_index(batch_op.f("ix_projects_archived"))

    op.drop_table("projects")
    with op.batch_alter_table("evaluation_suites", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_evaluation_suites_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_evaluation_suites_is_demo"))

    op.drop_table("evaluation_suites")
    with op.batch_alter_table("evaluation_definitions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_evaluation_definitions_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_evaluation_definitions_is_demo"))

    op.drop_table("evaluation_definitions")
    with op.batch_alter_table("audit_entries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_entries_workspace_id"))
        batch_op.drop_index("ix_audit_entries_ts")
        batch_op.drop_index(batch_op.f("ix_audit_entries_run_id"))
        batch_op.drop_index("ix_audit_entries_resource")
        batch_op.drop_index(batch_op.f("ix_audit_entries_action"))

    op.drop_table("audit_entries")
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_workspaces_is_demo"))

    op.drop_table("workspaces")
    with op.batch_alter_table("worker_heartbeats", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_worker_heartbeats_last_heartbeat_at"))

    op.drop_table("worker_heartbeats")
    op.drop_table("scheduler_leases")
    op.drop_table("app_settings")
