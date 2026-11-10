"""C and C++ extraction against tests/fixtures/cpp (a tiny game-server layout)."""

from pathlib import Path

from gita.indexer.parsers import FileStructure, Symbol, parse_file

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "cpp"


def _parse(rel: str, language: str = "cpp") -> FileStructure:
    path = FIXTURES_DIR / rel
    return parse_file(path, path.read_text(encoding="utf-8"), language)


def _fn(structure: FileStructure, name: str) -> Symbol:
    return next(f for f in structure.functions if f.name == name)


class TestHeader:
    def test_class_with_doxygen_brief(self):
        s = _parse("src/game/Player.h")
        player = next(c for c in s.classes if c.name == "Player")
        assert player.kind == "class"
        assert player.docstring == "A connected player character."
        assert (player.start_line, player.end_line) == (15, 33)

    def test_nested_struct_knows_its_class(self):
        stats = next(c for c in _parse("src/game/Player.h").classes if c.name == "Stats")
        assert stats.kind == "struct" and stats.parent_class == "Player"

    def test_enum_class(self):
        team = next(c for c in _parse("src/game/Player.h").classes if c.name == "Team")
        assert team.kind == "enum"

    def test_inline_methods_belong_to_the_class(self):
        s = _parse("src/game/Player.h")
        ctor, alive = _fn(s, "Player"), _fn(s, "IsAlive")
        assert ctor.kind == alive.kind == "method"
        assert ctor.parent_class == alive.parent_class == "Player"
        # The initializer list isn't part of the signature
        assert ctor.signature == "explicit Player(Session* session)"
        assert alive.signature == "bool IsAlive() const"

    def test_declarations_without_a_body_are_not_functions(self):
        names = {f.name for f in _parse("src/game/Player.h").functions}
        assert "Update" not in names and "~Player" not in names

    def test_template_function_spans_its_template_line(self):
        clamp = _fn(_parse("src/game/Player.h"), "Clamp")
        assert clamp.kind == "function" and clamp.parent_class is None
        assert clamp.start_line == 37 and clamp.end_line == 38
        assert clamp.signature == "template <typename T> T Clamp(T value, T lo, T hi)"

    def test_includes_in_order(self):
        raws = [i.raw for i in _parse("src/game/Player.h").imports]
        assert raws == ['#include "Common.h"', '#include "net/Session.h"', "#include <vector>"]


class TestSourceFile:
    def test_out_of_line_definitions_belong_to_their_class(self):
        s = _parse("src/game/Player.cpp")
        update = _fn(s, "Update")
        assert update.kind == "method" and update.parent_class == "Player"
        assert update.signature == "void Player::Update(uint32 diff)"
        assert (update.start_line, update.end_line) == (13, 16)

    def test_plain_comment_above_is_the_docstring(self):
        update = _fn(_parse("src/game/Player.cpp"), "Update")
        assert update.docstring == "Regenerates health, then ticks spell timers."

    def test_destructor_and_nested_qualifier(self):
        s = _parse("src/game/Player.cpp")
        assert _fn(s, "~Player").parent_class == "Player"
        get = _fn(s, "GetSession")  # Game::Player::GetSession, returns a reference
        assert get.parent_class == "Player"
        assert get.signature == "const Session& Game::Player::GetSession() const"

    def test_free_operator(self):
        op = _fn(_parse("src/game/Player.cpp"), "operator==")
        assert op.kind == "function" and op.parent_class is None

    def test_functions_sorted_by_line(self):
        lines = [f.start_line for f in _parse("src/game/Player.cpp").functions]
        assert lines == sorted(lines)


class TestC:
    def test_function_struct_and_block_comment(self):
        s = _parse("src/net/checksum.c", "c")
        add = _fn(s, "add_bytes")
        assert add.kind == "function"
        assert add.signature == "static unsigned add_bytes(const unsigned char* buf, int len)"
        assert add.docstring == "Adds every byte of buf."
        assert [(c.name, c.kind) for c in s.classes] == [("Packet", "struct")]
        assert [i.raw for i in s.imports] == ['#include "Session.h"']


class TestRobustness:
    def test_macro_heavy_code_still_yields_what_parses(self):
        source = (
            "#define DECLARE(x) void x();\n"
            "DECLARE(Foo)\n"
            "BEGIN_NAMESPACE\n"
            "int Real(int a) { return a; }\n"
            "END_NAMESPACE\n"
        )
        s = parse_file(Path("m.cpp"), source, "cpp")
        assert "Real" in {f.name for f in s.functions}

    def test_trailing_comment_on_previous_line_is_not_a_docstring(self):
        source = "int x = 0; // counter\nint Next() { return ++x; }\n"
        assert parse_file(Path("t.cpp"), source, "cpp").functions[0].docstring is None
