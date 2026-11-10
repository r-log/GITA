"""Model routing: which tier reviews a PR, decided in code with its reasons."""

from __future__ import annotations

from types import SimpleNamespace

from gita.agents.pr_reviewer.diff_parser import ChangedLineRange, DiffHunk
from gita.agents.routing import ModelTiers, path_words, route_review
from gita.views.diff_context import FileContext, language_for

TIERS = ModelTiers(small="m-small", standard="m-standard", large="m-large")


def _file(path: str, additions: int = 10, importers: int = 0) -> FileContext:
    hunk = DiffHunk(
        file_path=path,
        status="modified",
        additions=additions,
        deletions=0,
        patch="@@ -1 +1 @@\n+x",
        changed_ranges=[ChangedLineRange(start=1, count=1)],
    )
    return FileContext(
        file_path=path,
        diff_hunk=hunk,
        indexed=True,
        language=language_for(path),
        imported_by=[f"src/f{i}.cpp" for i in range(importers)],
    )


def test_path_words_are_whole_words():
    assert {"session", "world"} <= path_words("src/game/session/WorldSession.cpp")
    assert "lock" not in path_words("src/game/time/Clock.cpp")
    assert "token" not in path_words("src/parse/Tokenizer.py")
    assert {"srp6", "auth"} <= path_words("src/auth/Srp6.cpp")


def test_no_product_code_goes_to_the_small_tier():
    route = route_review([_file("docs/guide.md", 80), _file("tests/test_pay.py", 40)], TIERS)
    assert (route.tier, route.model) == ("small", "m-small")


def test_ordinary_code_goes_to_standard():
    route = route_review([_file("src/game/Quest.cpp", 120)], TIERS)
    assert (route.tier, route.model) == ("standard", "m-standard")
    assert route.reasons == ("1 source file, 120 added lines",)


def test_sensitive_code_goes_to_large_and_says_why():
    route = route_review([_file("src/auth/Srp6.cpp", 30)], TIERS)
    assert route.tier == "large"
    assert any("sensitive: auth, srp6" in r for r in route.reasons)


def test_size_and_fan_in_go_to_large():
    assert route_review([_file("src/game/Quest.cpp", 600)], TIERS).tier == "large"
    many = [_file(f"src/game/F{i}.cpp", 5) for i in range(10)]
    assert route_review(many, TIERS).tier == "large"
    shared = route_review([_file("src/shared/Define.h", 3, importers=480)], TIERS)
    assert shared.tier == "large" and "included by 480 files" in shared.reasons


def test_deletion_only_source_doesnt_count_as_product_code_added():
    assert route_review([_file("src/game/Old.cpp", 0)], TIERS).tier == "small"


def test_unset_tiers_fall_back_to_the_default_model():
    settings = SimpleNamespace(
        ai_default_model="m-default",
        ai_model_small=None,
        ai_model_standard="m-std",
        ai_model_large=None,
    )
    assert ModelTiers.from_settings(settings) == ModelTiers("m-default", "m-std", "m-default")
