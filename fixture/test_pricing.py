"""Read-only tests for the demo fixture. PatchPilot never modifies this file."""

import pytest

from pricing import apply_discount


def test_normal_discount():
    assert apply_discount(100, 20) == pytest.approx(80)
    assert apply_discount(50, 10) == pytest.approx(45)  # a price other than 100


def test_zero_discount():
    assert apply_discount(100, 0) == pytest.approx(100)


def test_full_discount():
    assert apply_discount(100, 100) == pytest.approx(0)
