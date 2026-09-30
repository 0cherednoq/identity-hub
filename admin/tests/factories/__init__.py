"""Stable factories shared by unit, integration, and E2E tests."""

from tests.factories.domain import AccountFactory, AdminPrincipalFixture, SeededSpaces
from tests.factories.services import ADMIN_TEST_SERVICE

__all__ = ["ADMIN_TEST_SERVICE", "AccountFactory", "AdminPrincipalFixture", "SeededSpaces"]
