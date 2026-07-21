"""Atomic transaction boundary for repository operations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from types import TracebackType

from . import db
from .repositories import LegacyRepositoryCatalog, RepositoryCatalog


RepositoryFactory = Callable[[sqlite3.Connection], RepositoryCatalog]


class UnitOfWorkError(RuntimeError):
    """Raised for invalid unit-of-work lifecycle use."""


class SqliteUnitOfWork:
    """Share one SQLite transaction across legacy and future repositories.

    ``commit()`` requests a commit at clean context exit. An exception after the
    request still rolls the whole transaction back.
    """

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection] = db.connect,
        repository_factory: RepositoryFactory = LegacyRepositoryCatalog,
    ) -> None:
        self._connection_factory = connection_factory
        self._repository_factory = repository_factory
        self._connection: sqlite3.Connection | None = None
        self._repositories: RepositoryCatalog | None = None
        self._commit_requested = False
        self._force_rollback = False

    @property
    def repositories(self) -> RepositoryCatalog:
        if self._repositories is None:
            raise UnitOfWorkError("unit of work is not active")
        return self._repositories

    @property
    def connection(self) -> sqlite3.Connection:
        """Transaction connection for repository implementors, not core modules."""
        if self._connection is None:
            raise UnitOfWorkError("unit of work is not active")
        return self._connection

    def __enter__(self) -> SqliteUnitOfWork:
        if self._connection is not None:
            raise UnitOfWorkError("unit of work cannot be re-entered")
        self._commit_requested = False
        self._force_rollback = False
        connection = self._connection_factory()
        try:
            connection.execute("BEGIN IMMEDIATE")
            repositories = self._repository_factory(connection)
        except Exception:
            connection.rollback()
            connection.close()
            raise
        self._connection = connection
        self._repositories = repositories
        return self

    def commit(self) -> None:
        if self._connection is None:
            raise UnitOfWorkError("unit of work is not active")
        self._commit_requested = True

    def rollback(self) -> None:
        if self._connection is None:
            raise UnitOfWorkError("unit of work is not active")
        self._force_rollback = True

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        if connection is None:
            raise UnitOfWorkError("unit of work is not active")
        try:
            if exception_type is None and self._commit_requested and not self._force_rollback:
                connection.commit()
            else:
                connection.rollback()
        finally:
            connection.close()
            self._connection = None
            self._repositories = None


def unit_of_work() -> SqliteUnitOfWork:
    return SqliteUnitOfWork()
