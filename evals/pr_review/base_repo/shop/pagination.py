"""Page arithmetic. Pages are 1-based."""

import math

MAX_PER_PAGE = 100


def clamp_per_page(per_page: int) -> int:
    return max(1, min(per_page, MAX_PER_PAGE))


def offset_for(page: int, per_page: int) -> int:
    if page < 1:
        raise ValueError("page must be >= 1")
    return (page - 1) * per_page


def page_count(total_items: int, per_page: int) -> int:
    return max(1, math.ceil(total_items / per_page))
