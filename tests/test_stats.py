from datetime import date

from postpilot.stats import Stats, TrafficStats, format_stats


def test_format_empty_stats():
    assert format_stats(Stats()) == (
        "<b>Engagement (30 days)</b>\nNone.\n\n"
        "<b>Published posts (30 days)</b>\nNone.\n\n"
        "<b>Short-link clicks</b>\nTotal: 0 · Last 30 days: 0\n\n"
        "<b>GitHub traffic</b>\nNo traffic snapshot."
    )


def test_format_full_stats_escapes_labels():
    stats = Stats(
        engagement=(("linkedin<&", "com<ment", 2),),
        published=(("instagram<> (reels)", 4),),
        total_clicks=10,
        recent_clicks=5,
        top_links=(("example<&", 'd"e', 9),),
        traffic=TrafficStats(date(2026, 11, 20), 7, 6, 5, (("repo<&", 7),)),
    )
    assert format_stats(stats) == (
        "<b>Engagement (30 days)</b>\nlinkedin&lt;&amp; · com&lt;ment: 2\n\n"
        "<b>Published posts (30 days)</b>\ninstagram&lt;&gt; (reels): 4\n\n"
        "<b>Short-link clicks</b>\nTotal: 10 · Last 30 days: 5\n"
        "example&lt;&amp; (d&quot;e): 9\n\n"
        "<b>GitHub traffic</b>\n2026-11-20 · Views: 7 · Unique views: 6 · Clones: 5\n"
        "repo&lt;&amp;: 7"
    )


def test_format_stats_bounds_message_and_keeps_all_sections():
    label = "'😀<" * 500
    result = format_stats(
        Stats(
            engagement=tuple((label, label, count) for count in range(50)),
            published=tuple((label, count) for count in range(50)),
            top_links=tuple((label, label, count) for count in (7, 6, 5)),
        )
    )
    assert len(result.encode("utf-16-le")) // 2 < 4096
    assert [f"): {count}" in result for count in (7, 6, 5)] == [True] * 3
    assert result.count("<b>") == result.count("</b>") == 4
