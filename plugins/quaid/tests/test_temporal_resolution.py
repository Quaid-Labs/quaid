"""Tests for temporal resolution: relative date replacement in fact text."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from janitor import _resolve_relative_date


class TestTomorrowResolution:
    """'tomorrow' resolves to created_at + 1 day."""

    def test_tomorrow_simple(self):
        result = _resolve_relative_date(
            "Quaid is meeting Hauser tomorrow for tea",
            "2026-02-05T15:16:27.535993"
        )
        assert result is not None
        assert "on 2026-02-06" in result
        assert "tomorrow" not in result

    def test_tomorrow_past_tense(self):
        """If created_at is in the past, tense should be adjusted."""
        result = _resolve_relative_date(
            "Quaid is meeting Hauser tomorrow for tea",
            "2026-01-15T10:00:00"
        )
        assert result is not None
        assert "on 2026-01-16" in result
        assert "met" in result  # "is meeting" → "met"


class TestYesterdayResolution:
    def test_yesterday(self):
        result = _resolve_relative_date(
            "Quaid visited Hauser yesterday",
            "2026-02-05T10:00:00"
        )
        assert result is not None
        assert "on 2026-02-04" in result
        assert "yesterday" not in result


class TestTodayResolution:
    def test_today(self):
        result = _resolve_relative_date(
            "Quaid prefers tackling tasks today.",
            "2026-02-02T13:58:12.163002"
        )
        assert result is not None
        assert "on 2026-02-02" in result
        assert "today" not in result.replace("2026-02-02", "")

    def test_tonight(self):
        result = _resolve_relative_date(
            "Quaid is having dinner tonight",
            "2026-02-05T18:00:00"
        )
        assert result is not None
        assert "on 2026-02-05" in result
        assert "tonight" not in result

    def test_this_morning(self):
        result = _resolve_relative_date(
            "Quaid had coffee this morning",
            "2026-02-05T09:00:00"
        )
        assert result is not None
        assert "on 2026-02-05" in result
        assert "this morning" not in result


class TestWeekResolution:
    def test_next_week(self):
        result = _resolve_relative_date(
            "Quaid plans to visit next week",
            "2026-02-05T10:00:00"
        )
        assert result is not None
        assert "on 2026-02-12" in result

    def test_last_week(self):
        result = _resolve_relative_date(
            "Quaid went hiking last week",
            "2026-02-05T10:00:00"
        )
        assert result is not None
        assert "on 2026-01-29" in result


class TestMonthResolution:
    def test_next_month(self):
        result = _resolve_relative_date(
            "Quaid is traveling next month",
            "2026-02-05T10:00:00"
        )
        assert result is not None
        assert "on 2026-03-07" in result

    def test_last_month(self):
        result = _resolve_relative_date(
            "Quaid started a project last month",
            "2026-02-05T10:00:00"
        )
        assert result is not None
        assert "on 2026-01-06" in result


class TestYearResolution:
    def test_next_year(self):
        result = _resolve_relative_date(
            "Quaid wants to move next year",
            "2026-02-05T10:00:00"
        )
        assert result is not None
        assert "on 2027-02-05" in result


class TestNoChange:
    """Facts without temporal references should return None."""

    def test_no_temporal_reference(self):
        result = _resolve_relative_date(
            "Quaid lives in Bali",
            "2026-02-05T10:00:00"
        )
        assert result is None

    def test_absolute_date_untouched(self):
        result = _resolve_relative_date(
            "Quaid was born on July 22, 1986",
            "2026-02-05T10:00:00"
        )
        assert result is None

    def test_bad_created_at(self):
        result = _resolve_relative_date(
            "Quaid is meeting Hauser tomorrow",
            "not-a-date"
        )
        assert result is None

    def test_none_created_at(self):
        result = _resolve_relative_date(
            "Quaid is meeting Hauser tomorrow",
            None
        )
        assert result is None


class TestTenseAdjustment:
    """Past tense adjustment for facts with past created_at."""

    def test_is_meeting_becomes_met(self):
        result = _resolve_relative_date(
            "Quaid is meeting Hauser tomorrow for tea",
            "2026-01-01T10:00:00"
        )
        assert "met" in result
        assert "is meeting" not in result

    def test_is_having_becomes_had(self):
        result = _resolve_relative_date(
            "Quaid is having dinner tonight",
            "2026-01-01T18:00:00"
        )
        assert "had" in result
        assert "is having" not in result

    def test_will_meet_becomes_met(self):
        result = _resolve_relative_date(
            "Quaid will meet Hauser tomorrow",
            "2026-01-01T10:00:00"
        )
        assert "met" in result
        assert "will meet" not in result


class TestRealFactsFromDB:
    """Test with actual facts found in the database."""

    def test_dashboard_next_run_tomorrow(self):
        result = _resolve_relative_date(
            "Quaid's dashboard now shows the next run as tomorrow at 4:30 AM",
            "2026-02-02T13:35:04.112655"
        )
        assert result is not None
        assert "2026-02-03" in result
        assert "tomorrow" not in result

    def test_log_file_created_today(self):
        result = _resolve_relative_date(
            "Quaid's log file was created today at 9:48 AM for a dry-run decay test.",
            "2026-02-02T13:47:48.553301"
        )
        assert result is not None
        assert "on 2026-02-02" in result

    def test_meeting_shannon_tea_tomorrow(self):
        result = _resolve_relative_date(
            "Quaid is meeting his sister Hauser for tea tomorrow. This is an important family meetup to remember.",
            "2026-02-05T07:26:17.846864"
        )
        assert result is not None
        assert "on 2026-02-06" in result
        assert "tomorrow" not in result

    def test_prefers_tackling_today(self):
        result = _resolve_relative_date(
            "Quaid prefers tackling tasks today.",
            "2026-02-02T13:58:12.163002"
        )
        assert result is not None
        assert "on 2026-02-02" in result
