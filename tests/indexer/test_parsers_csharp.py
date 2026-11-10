"""C# extraction."""

from pathlib import Path

from gita.indexer.parsers import parse_file

SOURCE = """\
using System;
using Shop.Payments;

namespace Shop.Orders;

/// <summary>
/// Computes order totals.
/// </summary>
[Serializable]
public sealed class OrderService : IOrderService
{
    private readonly IRepo _repo;

    public OrderService(IRepo repo) { _repo = repo; }

    /// <summary>Sums the lines of an order.</summary>
    [HttpGet("total")]
    public async Task<decimal> TotalAsync(int id) => await _repo.Sum(id);

    private static int Helper(int x)
    {
        int Twice(int y) => y * 2;
        return Twice(x);
    }

    ~OrderService() { }
}

public interface IOrderService
{
    Task<decimal> TotalAsync(int id);
}

public record Money(decimal Amount);
public struct Point { public int X; }
public enum Status { Open, Closed }
"""


def _parse():
    return parse_file(Path("OrderService.cs"), SOURCE, "csharp")


def test_types_and_kinds():
    kinds = {c.name: c.kind for c in _parse().classes}
    assert kinds == {
        "OrderService": "class",
        "IOrderService": "interface",
        "Money": "record",
        "Point": "struct",
        "Status": "enum",
    }


def test_class_xml_doc_and_attribute():
    cls = next(c for c in _parse().classes if c.name == "OrderService")
    assert cls.docstring == "Computes order totals."
    assert cls.decorators == ["[Serializable]"]
    assert cls.start_line == 9  # the attribute line belongs to the declaration


def test_methods_constructor_and_async():
    fns = {(f.name, f.parent_class): f for f in _parse().functions}
    total = fns[("TotalAsync", "OrderService")]
    assert total.kind == "async_method"
    assert total.signature == "public async Task<decimal> TotalAsync(int id)"
    assert total.docstring == "Sums the lines of an order."
    assert total.decorators == ['[HttpGet("total")]']
    assert fns[("OrderService", "OrderService")].kind == "method"  # constructor
    assert fns[("TotalAsync", "IOrderService")].signature == "Task<decimal> TotalAsync(int id)"


def test_local_function_is_a_function():
    twice = next(f for f in _parse().functions if f.name == "Twice")
    assert twice.kind == "function"


def test_usings_are_imports():
    assert [i.raw for i in _parse().imports] == ["using System;", "using Shop.Payments;"]
