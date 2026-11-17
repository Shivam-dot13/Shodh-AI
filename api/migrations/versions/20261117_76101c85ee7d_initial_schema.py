"""initial schema

One revision creates the whole model. The only hand-written part is the final
problems.current_version_id foreign key: it points at problem_versions while
problem_versions points back at problems, so it has to be added after both tables exist.

Revision ID: 76101c85ee7d
Revises: 
Create Date: 2026-11-17 11:17:32.969475+00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '76101c85ee7d'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('organizations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('contests',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('scoring_mode', sa.Enum('ICPC', 'POINTS', name='scoring_mode', native_enum=False, length=40), nullable=False),
    sa.Column('penalty_minutes', sa.Integer(), nullable=False),
    sa.Column('max_attempts_per_problem', sa.Integer(), nullable=True),
    sa.Column('standings_version', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('ends_at > starts_at', name='ck_contests_window'),
    sa.CheckConstraint('penalty_minutes >= 0', name='ck_contests_penalty'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('org_id', 'slug', name='uq_contests_org_slug')
    )
    op.create_index('ix_contests_org', 'contests', ['org_id'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('role', sa.Enum('ORGANIZER', 'PARTICIPANT', name='role', native_enum=False, length=40), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email', name='uq_users_email')
    )
    op.create_index('ix_users_org', 'users', ['org_id'], unique=False)
    op.create_table('api_tokens',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.Enum('USER', 'SERVICE', name='token_kind', native_enum=False, length=40), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('label', sa.String(length=120), nullable=False),
    sa.Column('token_sha256', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(kind = 'SERVICE' AND user_id IS NULL) OR (kind = 'USER' AND user_id IS NOT NULL)", name='ck_api_tokens_kind_user'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_sha256')
    )
    op.create_table('audit_log',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('actor_label', sa.String(length=120), nullable=False),
    sa.Column('action', sa.String(length=80), nullable=False),
    sa.Column('target_type', sa.String(length=60), nullable=False),
    sa.Column('target_id', sa.String(length=80), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_target', 'audit_log', ['target_type', 'target_id'], unique=False)
    op.create_table('failpoints',
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('contest_participants',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('contest_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('joined_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['contest_id'], ['contests.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('contest_id', 'user_id', name='uq_participant_contest_user')
    )
    op.create_index('ix_participants_user', 'contest_participants', ['user_id'], unique=False)
    op.create_table('problems',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('contest_id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('current_version_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['contest_id'], ['contests.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('contest_id', 'slug', name='uq_problems_contest_slug')
    )
    op.create_index('ix_problems_contest', 'problems', ['contest_id', 'ordinal'], unique=False)
    op.create_table('problem_scores',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('contest_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('problem_id', sa.UUID(), nullable=False),
    sa.Column('authoritative_attempt_id', sa.UUID(), nullable=True),
    sa.Column('submission_id', sa.UUID(), nullable=True),
    sa.Column('points', sa.Integer(), nullable=False),
    sa.Column('penalty_minutes', sa.Integer(), nullable=False),
    sa.Column('rejected_before_solve', sa.Integer(), nullable=False),
    sa.Column('solved', sa.Boolean(), nullable=False),
    sa.Column('solved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_progress_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['contest_id'], ['contests.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['problem_id'], ['problems.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('contest_id', 'user_id', 'problem_id', name='uq_problem_scores_cell')
    )
    op.create_index('ix_problem_scores_contest', 'problem_scores', ['contest_id', 'user_id'], unique=False)
    op.create_table('submissions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('contest_id', sa.UUID(), nullable=False),
    sa.Column('problem_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('language', sa.Enum('PYTHON3', 'CPP17', name='language', native_enum=False, length=40), nullable=False),
    sa.Column('source_code', sa.Text(), nullable=False),
    sa.Column('source_sha256', sa.String(length=64), nullable=False),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('request_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum('QUEUED', 'RUNNING', 'JUDGED', 'JUDGE_FAILED', name='submission_status', native_enum=False, length=40), nullable=False),
    sa.Column('authoritative_attempt_id', sa.UUID(), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['contest_id'], ['contests.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['problem_id'], ['problems.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('contest_id', 'user_id', 'idempotency_key', name='uq_submissions_idempotency')
    )
    op.create_index('ix_submissions_contest_listing', 'submissions', ['contest_id', 'submitted_at', 'id'], unique=False)
    op.create_index('ix_submissions_contest_user_problem', 'submissions', ['contest_id', 'user_id', 'problem_id'], unique=False)
    op.create_index('ix_submissions_problem', 'submissions', ['problem_id'], unique=False)
    op.create_table('test_suites',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('problem_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('label', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('version >= 1', name='ck_test_suites_version'),
    sa.ForeignKeyConstraint(['problem_id'], ['problems.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('problem_id', 'version', name='uq_test_suites_problem_version')
    )
    op.create_table('problem_versions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('problem_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('time_limit_ms', sa.Integer(), nullable=False),
    sa.Column('memory_limit_mb', sa.Integer(), nullable=False),
    sa.Column('output_limit_bytes', sa.Integer(), nullable=False),
    sa.Column('max_points', sa.Integer(), nullable=False),
    sa.Column('compare_mode', sa.Enum('EXACT', 'TRIM_TRAILING_WS', name='compare_mode', native_enum=False, length=40), nullable=False),
    sa.Column('default_test_suite_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('max_points >= 0', name='ck_problem_versions_points'),
    sa.CheckConstraint('memory_limit_mb BETWEEN 16 AND 2048', name='ck_problem_versions_ml'),
    sa.CheckConstraint('output_limit_bytes BETWEEN 256 AND 8388608', name='ck_problem_versions_ol'),
    sa.CheckConstraint('time_limit_ms BETWEEN 100 AND 30000', name='ck_problem_versions_tl'),
    sa.CheckConstraint('version >= 1', name='ck_problem_versions_version'),
    sa.ForeignKeyConstraint(['default_test_suite_id'], ['test_suites.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['problem_id'], ['problems.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('problem_id', 'version', name='uq_problem_versions_problem_version')
    )
    op.create_table('test_cases',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('test_suite_id', sa.UUID(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('is_sample', sa.Boolean(), nullable=False),
    sa.Column('weight', sa.Integer(), nullable=False),
    sa.Column('input_data', sa.Text(), nullable=False),
    sa.Column('expected_output', sa.Text(), nullable=False),
    sa.CheckConstraint('weight >= 0', name='ck_test_cases_weight'),
    sa.ForeignKeyConstraint(['test_suite_id'], ['test_suites.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('test_suite_id', 'ordinal', name='uq_test_cases_suite_ordinal')
    )
    op.create_table('judging_attempts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('submission_id', sa.UUID(), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Enum('INITIAL', 'REJUDGE', name='attempt_kind', native_enum=False, length=40), nullable=False),
    sa.Column('problem_version_id', sa.UUID(), nullable=False),
    sa.Column('test_suite_id', sa.UUID(), nullable=False),
    sa.Column('runtime_config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('state', sa.Enum('PENDING', 'DISPATCHED', 'RUNNING', 'COMPLETED', 'INFRA_FAILED', 'CANCELLED', name='attempt_state', native_enum=False, length=40), nullable=False),
    sa.Column('verdict', sa.Enum('ACCEPTED', 'WRONG_ANSWER', 'COMPILE_ERROR', 'RUNTIME_ERROR', 'TIME_LIMIT_EXCEEDED', 'MEMORY_LIMIT_EXCEEDED', 'OUTPUT_LIMIT_EXCEEDED', name='verdict', native_enum=False, length=40), nullable=True),
    sa.Column('score', sa.Integer(), nullable=False),
    sa.Column('max_score', sa.Integer(), nullable=False),
    sa.Column('tests_total', sa.Integer(), nullable=False),
    sa.Column('tests_passed', sa.Integer(), nullable=False),
    sa.Column('time_ms', sa.Integer(), nullable=True),
    sa.Column('memory_kb', sa.Integer(), nullable=True),
    sa.Column('exec_mode', sa.String(length=16), nullable=True),
    sa.Column('runner_image', sa.String(length=200), nullable=True),
    sa.Column('worker_id', sa.String(length=80), nullable=True),
    sa.Column('lease_epoch', sa.Integer(), nullable=False),
    sa.Column('failure_reason', sa.Text(), nullable=True),
    sa.Column('compile_message', sa.Text(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(state <> 'COMPLETED') OR (verdict IS NOT NULL)", name='ck_attempts_completed_has_verdict'),
    sa.CheckConstraint("(state <> 'INFRA_FAILED') OR (verdict IS NULL)", name='ck_attempts_infra_no_verdict'),
    sa.CheckConstraint('attempt_no >= 1', name='ck_attempts_no'),
    sa.CheckConstraint('score >= 0 AND score <= max_score', name='ck_attempts_score_range'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['problem_version_id'], ['problem_versions.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['submission_id'], ['submissions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['test_suite_id'], ['test_suites.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('submission_id', 'attempt_no', name='uq_attempts_submission_no')
    )
    op.create_index('ix_attempts_state', 'judging_attempts', ['state'], unique=False)
    op.create_index('ix_attempts_submission', 'judging_attempts', ['submission_id', 'attempt_no'], unique=False)
    op.create_table('attempt_test_results',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('attempt_id', sa.UUID(), nullable=False),
    sa.Column('test_case_id', sa.UUID(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('is_sample', sa.Boolean(), nullable=False),
    sa.Column('status', sa.Enum('PASSED', 'WRONG_OUTPUT', 'RUNTIME_ERROR', 'TIMEOUT', 'MEMORY_EXCEEDED', 'OUTPUT_EXCEEDED', 'SKIPPED', name='test_status', native_enum=False, length=40), nullable=False),
    sa.Column('time_ms', sa.Integer(), nullable=True),
    sa.Column('memory_kb', sa.Integer(), nullable=True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('output_bytes', sa.Integer(), nullable=False),
    sa.Column('truncated', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['attempt_id'], ['judging_attempts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['test_case_id'], ['test_cases.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('attempt_id', 'test_case_id', name='uq_results_attempt_case')
    )
    op.create_index('ix_results_attempt', 'attempt_test_results', ['attempt_id', 'ordinal'], unique=False)
    op.create_table('judge_jobs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('attempt_id', sa.UUID(), nullable=False),
    sa.Column('state', sa.Enum('READY', 'LEASED', 'DONE', 'DEAD', name='job_state', native_enum=False, length=40), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('leased_by', sa.String(length=80), nullable=True),
    sa.Column('delivery_count', sa.Integer(), nullable=False),
    sa.Column('max_deliveries', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('delivery_count >= 0', name='ck_jobs_delivery_count'),
    sa.ForeignKeyConstraint(['attempt_id'], ['judging_attempts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('attempt_id')
    )
    op.create_index('ix_jobs_claim', 'judge_jobs', ['state', 'priority', 'available_at'], unique=False)
    op.create_index('ix_jobs_lease_expiry', 'judge_jobs', ['state', 'lease_expires_at'], unique=False)


    # Deliberately added after both tables exist: a problem points at its published version
    # and a version points back at its problem, which is a cycle CREATE TABLE cannot express.
    op.create_foreign_key(
        "fk_problems_current_version",
        "problems",
        "problem_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_problems_current_version", "problems", type_="foreignkey")
    op.drop_index('ix_jobs_lease_expiry', table_name='judge_jobs')
    op.drop_index('ix_jobs_claim', table_name='judge_jobs')
    op.drop_table('judge_jobs')
    op.drop_index('ix_results_attempt', table_name='attempt_test_results')
    op.drop_table('attempt_test_results')
    op.drop_index('ix_attempts_submission', table_name='judging_attempts')
    op.drop_index('ix_attempts_state', table_name='judging_attempts')
    op.drop_table('judging_attempts')
    op.drop_table('test_cases')
    op.drop_table('problem_versions')
    op.drop_table('test_suites')
    op.drop_index('ix_submissions_problem', table_name='submissions')
    op.drop_index('ix_submissions_contest_user_problem', table_name='submissions')
    op.drop_index('ix_submissions_contest_listing', table_name='submissions')
    op.drop_table('submissions')
    op.drop_index('ix_problem_scores_contest', table_name='problem_scores')
    op.drop_table('problem_scores')
    op.drop_index('ix_problems_contest', table_name='problems')
    op.drop_table('problems')
    op.drop_index('ix_participants_user', table_name='contest_participants')
    op.drop_table('contest_participants')
    op.drop_table('failpoints')
    op.drop_index('ix_audit_target', table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_table('api_tokens')
    op.drop_index('ix_users_org', table_name='users')
    op.drop_table('users')
    op.drop_index('ix_contests_org', table_name='contests')
    op.drop_table('contests')
    op.drop_table('organizations')
