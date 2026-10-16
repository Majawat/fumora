import pytest

from fumora.numbers import normalize_number, sort_number


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("1", "001"),
        ("1.50", "1.5"),
        ("1.0", "1"),
        ("½", "0.5"),
        ("-1", "-01"),
        ("#5", "5"),
        ("Alpha", "alpha"),
    ],
)
def test_equal_after_normalizing(a, b):
    assert normalize_number(a) == normalize_number(b)


def test_distinct_numbers_stay_distinct():
    assert normalize_number("258.1") != normalize_number("258")
    assert normalize_number("0") != normalize_number("-0.5")


def test_sort_number():
    assert sort_number("1.MU") == 1.0
    assert sort_number("½") == 0.5
    assert sort_number("-1") == -1.0
    assert sort_number("Alpha") is None
