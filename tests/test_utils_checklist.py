"""Tests for src.utils.checklist — parse, update, and add checklist items."""

from src.utils.checklist import parse_checklist, update_checklist, add_checklist_items


class TestParseChecklist:
    def test_checked_and_unchecked(self):
        body = "- [x] Task A (#1)\n- [ ] Task B (#2)"
        items = parse_checklist(body)
        assert len(items) == 2
        assert items[0]["checked"] is True
        assert items[0]["text"] == "Task A"
        assert items[0]["issue_number"] == 1
        assert items[1]["checked"] is False
        assert items[1]["issue_number"] == 2

    def test_capital_x(self):
        body = "- [X] Done task (#5)"
        items = parse_checklist(body)
        assert len(items) == 1
        assert items[0]["checked"] is True

    def test_item_without_issue_number(self):
        body = "- [ ] Task without number"
        items = parse_checklist(body)
        assert len(items) == 1
        assert items[0]["issue_number"] is None
        assert items[0]["text"] == "Task without number"

    def test_empty_body(self):
        assert parse_checklist("") == []

    def test_body_without_checklist(self):
        assert parse_checklist("Just some text\nNo checklist here") == []

    def test_mixed_content(self):
        body = "# Milestone v1\n\nSome text\n\n- [x] Auth (#1)\n- [ ] DB (#2)\n\nMore text"
        items = parse_checklist(body)
        assert len(items) == 2


class TestUpdateChecklist:
    def test_checks_closed_issue(self):
        body = "- [ ] Task A (#1)\n- [ ] Task B (#2)"
        result = update_checklist(body, {1: "closed"})
        assert result is not None
        assert "[x]" in result
        assert "Task A" in result

    def test_unchecks_open_issue(self):
        body = "- [x] Task A (#1)"
        result = update_checklist(body, {1: "open"})
        assert result is not None
        assert "[ ]" in result

    def test_no_changes_returns_none(self):
        body = "- [x] Done (#1)\n- [ ] Todo (#2)"
        result = update_checklist(body, {1: "closed", 2: "open"})
        assert result is None

    def test_issue_not_in_checklist(self):
        body = "- [ ] Task A (#1)"
        result = update_checklist(body, {999: "closed"})
        assert result is None


class TestAddChecklistItems:
    def test_appends_after_last_item(self):
        body = "# Milestone\n- [x] Task A (#1)\n- [ ] Task B (#2)\n\nFooter"
        new_items = ["- [ ] Task C (#3)"]
        result = add_checklist_items(body, new_items)
        assert "Task C (#3)" in result
        # Task C should come after Task B
        assert result.index("Task C") > result.index("Task B")

    def test_creates_tasks_section_when_empty(self):
        body = "# Milestone\n\nSome description"
        new_items = ["- [ ] First task (#1)"]
        result = add_checklist_items(body, new_items)
        assert "### Tasks" in result
        assert "First task (#1)" in result

    def test_empty_items_returns_unchanged(self):
        body = "Some body"
        result = add_checklist_items(body, [])
        assert result == body

    def test_multiple_items_added(self):
        body = "- [ ] Existing (#1)"
        new_items = ["- [ ] New A (#2)", "- [ ] New B (#3)"]
        result = add_checklist_items(body, new_items)
        assert "New A" in result
        assert "New B" in result
