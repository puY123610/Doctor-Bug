"""Role-based access rules for back-office operations."""

from dataclasses import dataclass


class PermissionDenied(Exception):
    """Raised when a user does not hold a role required by an operation."""


@dataclass(frozen=True)
class User:
    user_id: str
    roles: tuple[str, ...] = ()


def require_role(user: User, role: str) -> None:
    if role not in user.roles:
        raise PermissionDenied(f"user {user.user_id!r} lacks role {role!r}")


def can_refund(user: User) -> bool:
    return "finance" in user.roles or "support" in user.roles
