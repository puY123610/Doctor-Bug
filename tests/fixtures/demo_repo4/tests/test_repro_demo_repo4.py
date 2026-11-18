from durlib import format_duration


def test_format_duration_includes_hours():
    assert format_duration(5400) == "1h30m"
    assert format_duration(3600) == "1h"
    assert format_duration(3661) == "1h1m1s"
    assert format_duration(7200) == "2h"
    assert format_duration(90) == "1m30s"
