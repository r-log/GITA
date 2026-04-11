"""Application config. DO NOT USE IN PRODUCTION — this fixture has a
deliberately planted hardcoded secret for checklist testing."""

# PLANTED ISSUE: hardcoded API key / secret in source.
API_KEY = "sk-prod-abc123def456ghi789"
DATABASE_URL = "postgresql://admin:hunter2@prod-db.example.com:5432/app"

DEBUG = True
