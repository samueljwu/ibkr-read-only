"""Published v1 import scope; passing scope checks does not prove completeness."""
from datetime import date

CONTRACT_VERSION = 1
EXECUTION_TYPES = frozenset({"STK", "OPT", "BOND", "FUT"})
COMPLETE_RESULT_TYPES = frozenset({"STK", "OPT", "BOND"})


def flex_earliest(today):
    return date(today.year - 4, 1, 1)


def support_issues(*, account_type, customer_type, capabilities, base_currency,
                   instruments, report_format, first, last, today):
    """Assess explicit evidence, never infer account structure from its ID."""
    issues = []
    if account_type != "Individual (Single)":
        issues.append("Account type is unsupported or unverified")
    if customer_type not in {"Individual", "Joint"}:
        issues.append("Customer type is unsupported or unverified")
    if capabilities not in {"Cash", "Margin"}:
        issues.append("Account capabilities are unsupported or unverified")
    if base_currency != "USD":
        issues.append("USD base currency must be established")
    if not set(instruments).issubset(COMPLETE_RESULT_TYPES):
        issues.append("Instrument coverage is unsupported")
    if report_format != "activity-flex-xml":
        issues.append("This format cannot establish complete account coverage")
    if first > last or last >= today:
        issues.append("Choose completed ordered dates before today")
    if first < flex_earliest(today):
        issues.append("Requested dates precede Flex availability")
    return issues


COMPLETENESS_LIMIT = (
    "Saved report windows do not prove a complete account result. Account "
    "type/capabilities, unfiltered queries, instrument lifecycle support and "
    "opening balances/basis require separate verification."
)
