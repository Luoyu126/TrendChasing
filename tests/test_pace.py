import unittest

from trendradar.content_pool.pace import (
    COLLECT_WINDOW_SECONDS,
    call_is_due,
    maximum_gap,
    one_call_ids,
)


def row(platform, content_id):
    return {"platform": platform, "content_id": content_id}


class PaceTests(unittest.TestCase):
    def test_one_call_uses_the_whole_collect_window(self):
        self.assertEqual(maximum_gap(1, COLLECT_WINDOW_SECONDS), 3600)
        self.assertEqual(COLLECT_WINDOW_SECONDS, 3600)

    def test_several_calls_split_the_window_evenly(self):
        self.assertEqual(maximum_gap(4, 3600), 1200)

    def test_a_call_inside_the_window_waits(self):
        self.assertTrue(call_is_due(None, 3600))
        self.assertFalse(call_is_due(3599, 3600))
        self.assertTrue(call_is_due(3600, 3600))

    def test_fresh_social_posts_share_one_call_ahead_of_papers(self):
        candidates = [
            row("paper", "old-paper"),
            row("twitter", "old-post"),
            row("paper", "new-paper"),
            row("twitter", "new-a"),
            row("zhihu", "new-b"),
        ]
        fresh = {("paper", "new-paper"), ("twitter", "new-a"), ("zhihu", "new-b")}
        self.assertEqual(
            one_call_ids(candidates, fresh, 10),
            [("twitter", "new-a"), ("zhihu", "new-b")],
        )

    def test_without_fresh_posts_the_oldest_social_batch_is_next(self):
        candidates = [row("paper", "p"), row("twitter", "a"), row("twitter", "b")]
        self.assertEqual(one_call_ids(candidates, set(), 1), [("twitter", "a")])

    def test_a_paper_is_a_single_call(self):
        self.assertEqual(
            one_call_ids([row("paper", "a"), row("paper", "b")], {("paper", "a")}, 10),
            [("paper", "a")],
        )
