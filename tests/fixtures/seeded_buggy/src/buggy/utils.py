"""Utility helpers for the seeded_buggy fixture."""


# PLANTED ISSUE: mutable default argument accumulates across calls.
def accumulate(data, result={}):
    for key, value in data.items():
        result[key] = value
    return result


# PLANTED ISSUE: silently swallows exceptions and returns a misleading value.
def safe_parse_int(value, default=0):
    try:
        return int(value)
    except:
        return default
