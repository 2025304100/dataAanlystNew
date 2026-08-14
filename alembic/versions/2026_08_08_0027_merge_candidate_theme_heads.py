"""Merge the notification and investment-theme migration heads."""

revision = "wps_0027_027_merge_candidate_theme_heads"
down_revision = (
    "wps_0026_026_investment_theme_evidence",
    "wps_0026_026_notification_inbox_read_state",
)
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
