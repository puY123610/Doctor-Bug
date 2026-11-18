"""Tests for the in-memory stock ledger."""

import pytest

from backoffice.inventory import InsufficientStock, Inventory


def test_add_stock_accumulates():
    inventory = Inventory()
    inventory.add_stock("sku-1", 3)
    inventory.add_stock("sku-1", 4)
    assert inventory.available("sku-1") == 7


def test_available_unknown_sku_is_zero():
    inventory = Inventory()
    assert inventory.available("missing") == 0


def test_reserve_decrements_available_once():
    inventory = Inventory()
    inventory.add_stock("sku-1", 10)
    inventory.reserve("sku-1", 3)
    assert inventory.available("sku-1") == 7


def test_reserve_beyond_available_raises_and_keeps_state():
    inventory = Inventory()
    inventory.add_stock("sku-1", 5)
    inventory.reserve("sku-1", 2)
    with pytest.raises(InsufficientStock):
        inventory.reserve("sku-1", 4)
    inventory.release("sku-1", 2)
    assert inventory.available("sku-1") == 5


def test_release_all_restores_available():
    inventory = Inventory()
    inventory.add_stock("sku-1", 4)
    inventory.reserve("sku-1", 4)
    inventory.release("sku-1", 4)
    assert inventory.available("sku-1") == 4
