"""Space model calls across the hourly collect window.

One scheduled collect is one call. That is the widest gap that still
classifies newly stored items before the next collect. A run that must
make more than one call splits the same window evenly.
"""

COLLECT_WINDOW_SECONDS = 3600


def maximum_gap(call_count, window_seconds):
    if type(call_count) is not int or call_count < 1:
        raise ValueError("call_count")
    if type(window_seconds) is not int or window_seconds < 0:
        raise ValueError("window_seconds")
    if call_count == 1:
        return window_seconds
    return window_seconds // (call_count - 1)


def call_is_due(elapsed, gap):
    """elapsed is None when this database has never called the model."""
    if elapsed is None:
        return True
    if type(gap) is not int or gap < 0:
        raise ValueError("window_seconds")
    return elapsed >= gap


def one_call_ids(candidates, fresh, batch_size):
    """Pick the single next model call.

    Fresh social posts share one classification batch. A paper is its own
    call. Fresh items go before the older backlog, and social batches go
    before papers so one call covers as many of this hour's posts as it can.
    """
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size")
    fresh = set(fresh)

    def key(row):
        return (row["platform"], row["content_id"])

    def take(rows, limit):
        return [key(row) for row in rows[:limit]]

    social = [row for row in candidates if row["platform"] != "paper"]
    papers = [row for row in candidates if row["platform"] == "paper"]
    fresh_social = [row for row in social if key(row) in fresh]
    fresh_papers = [row for row in papers if key(row) in fresh]
    for rows, limit in (
        (fresh_social, batch_size),
        (fresh_papers, 1),
        (social, batch_size),
        (papers, 1),
    ):
        if rows:
            return take(rows, limit)
    return []
