"""Shared decorators for the buggy app.

This file is **deliberately valid Python**. It exists as the Week 3 Day 5
regression fixture for a specific hallucination pattern:

A prior onboarding run against a real repo (``AMASS/decorators.py:170``)
flagged the following as "unclosed parenthesis — unparseable code":

    user_id = getattr(request, 'current_user', {}
                      ).get('user_id', 'anonymous')

That's valid Python. ``getattr(obj, name, {})`` is a complete call whose
closing ``)`` merely lives on the following line for formatting. The
``{}`` is a complete dict literal (the default), not an unclosed open.

We plant the same pattern here. The ``seeded_buggy`` checklist's
``must_not_mention`` list includes ``unclosed paren`` / ``syntax error``
so any finding that re-hallucinates the parse failure fails the gated
golden test loudly. Do not change the shape without updating the
checklist.
"""
from functools import wraps


def log_access(f):
    """Log access to a wrapped function with a best-effort user context."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        # Valid Python — getattr(obj, name, default).method()
        # where the closing ) of getattr() is on the continuation line.
        context = getattr(args[0] if args else None, 'context', {}
                          ).get('user_id', 'anonymous')
        return f(*args, _access_context=context, **kwargs)

    return wrapper
