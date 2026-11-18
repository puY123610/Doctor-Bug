"""Tests for role checks and refund permission."""

import pytest

from backoffice.auth import PermissionDenied, User, can_refund, require_role


def test_finance_role_can_refund():
    user = User("u-finance", ("finance", "ops"))
    assert can_refund(user) is True


def test_support_role_cannot_refund():
    user = User("u-support", ("support",))
    assert can_refund(user) is False


def test_require_role_allows_held_role():
    user = User("u-support", ("support",))
    require_role(user, "support")


def test_require_role_denies_missing_role():
    user = User("u-support", ("support",))
    with pytest.raises(PermissionDenied):
        require_role(user, "finance")


def test_can_refund_false_without_finance_or_support():
    user = User("u-ops", ("ops", "sales"))
    assert can_refund(user) is False
