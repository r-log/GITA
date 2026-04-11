"""
Shared utilities for parsing and updating Milestone Tracker checklists.

Used by both the reconciliation worker and the onboarding agent's progressive flow.
"""

import re


# Compiled regex for matching a checked/unchecked sub-issue reference:
#   - [x] Some task (#42)
#   - [ ] Another task (#43)
# Capture groups: (1) mark char, (2) description, (3) issue number.
# Use with `.findall(body)` to get a list of (mark, desc, num_str) tuples.
CHECKLIST_ITEM_RE = re.compile(r"- \[([ xX])\] (.+?)\(#(\d+)\)")


def parse_checklist(body: str) -> list[dict]:
    """
    Parse markdown checklist items from a Milestone Tracker body.

    Returns list of dicts with: checked, text, issue_number, full_match.
    """
    pattern = re.compile(r"- \[([ xX])\] (.+?)(?:\(#(\d+)\))?$", re.MULTILINE)
    items = []
    for match in pattern.finditer(body):
        items.append({
            "checked": match.group(1).lower() == "x",
            "text": match.group(2).strip(),
            "issue_number": int(match.group(3)) if match.group(3) else None,
            "full_match": match.group(0),
        })
    return items


def update_checklist(body: str, issue_states: dict[int, str]) -> str | None:
    """
    Update checklist marks based on current issue states.
    Returns updated body or None if no changes needed.
    """
    updated = body
    changed = False

    for number, state in issue_states.items():
        should_be_checked = state == "closed"

        pattern = re.compile(rf"- \[([ xX])\] (.+?)\(#{number}\)")
        match = pattern.search(updated)
        if not match:
            continue

        currently_checked = match.group(1).lower() == "x"
        if should_be_checked and not currently_checked:
            updated = updated[:match.start()] + updated[match.start():match.end()].replace("[ ]", "[x]") + updated[match.end():]
            changed = True
        elif not should_be_checked and currently_checked:
            updated = updated[:match.start()] + updated[match.start():match.end()].replace("[x]", "[ ]").replace("[X]", "[ ]") + updated[match.end():]
            changed = True

    return updated if changed else None


def add_checklist_items(body: str, items: list[str]) -> str:
    """
    Add new checklist items to a Milestone Tracker body.

    Items should be formatted like: "- [ ] Task description (#123)"
    They are appended after the last existing checklist item.
    """
    if not items:
        return body

    # Find the last checklist item position
    pattern = re.compile(r"- \[[ xX]\] .+$", re.MULTILINE)
    last_match = None
    for match in pattern.finditer(body):
        last_match = match

    new_lines = "\n".join(items)

    if last_match:
        # Insert after the last checklist item
        insert_pos = last_match.end()
        return body[:insert_pos] + "\n" + new_lines + body[insert_pos:]
    else:
        # No existing checklist — append at end
        return body.rstrip() + "\n\n### Tasks\n" + new_lines + "\n"
