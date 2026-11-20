"""Names of the built-in workflows that schedules may reference (validated by config)."""

DAILY_WORKFLOWS = (
    "data_validation",
    "finance_review",
    "sales_review",
    "operations_review",
    "customer_success_review",
    "marketing_review",
    "daily_brief",
    "end_of_day_reconciliation",
)
WEEKLY_WORKFLOWS = ("weekly_management_review",)

KNOWN_WORKFLOWS = frozenset(DAILY_WORKFLOWS + WEEKLY_WORKFLOWS)

# Specialist review workflows -> the agent that runs them.
REVIEW_WORKFLOW_AGENTS = {
    "data_validation": "data_analyst",
    "finance_review": "cfo",
    "sales_review": "sales_director",
    "operations_review": "operations",
    "customer_success_review": "customer_success",
    "marketing_review": "marketing_director",
    "end_of_day_reconciliation": "chief_of_staff",
}
