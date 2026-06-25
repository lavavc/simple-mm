from decimal import Decimal

from research.backtester.cone_features import TimestampedValue, causal_percentile
from research.backtester.cone_features import previous_or_equal_with_age


def test_causal_percentile_uses_only_prior_values():
    assert causal_percentile(
        [Decimal("1"), Decimal("3"), Decimal("5")],
        Decimal("4"),
    ) == Decimal("0.6666666667")
    assert causal_percentile([], Decimal("4")) is None


def test_previous_or_equal_reports_age_and_respects_max_age():
    rows = [TimestampedValue(1000, "a"), TimestampedValue(2000, "b")]
    assert previous_or_equal_with_age(rows, 2500, 600).age_ms == 500
    assert previous_or_equal_with_age(rows, 2500, 400) is None
