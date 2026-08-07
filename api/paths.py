"""Per-user data access — the seam that keeps multi-user from being a rewrite.

This module used to resolve FILE PATHS for a user (data/users/<id>/portfolios.json).
Now that storage is SQLite it resolves REPOSITORIES instead — but the shape of
the seam is unchanged, and that's the point: routes still ask "give me this
user's portfolios" and never know or care what's underneath.

Because every caller already threaded a user_id through this module, swapping
JSON files for SQL tables touched this file and the repository layer, and left
the routes almost untouched.
"""

from __future__ import annotations

import os

from engine.repository import (
    AlertRepository,
    JournalRepository,
    PriceRepository,
    SnapshotRepository,
    SqlitePortfolioManager,
    TrackerRepository,
    UserRepository,
    WatchlistRepository,
)

DB_NAME = "papertrading.db"


def db_file(root: str) -> str:
    return os.path.join(root, DB_NAME)


class UserData:
    """Everything one authenticated user can touch, in one object.

    Built once per request by @require_auth and attached to flask.g, so routes
    just say g.data.portfolios() instead of constructing repositories (and
    definitely instead of opening a file by name).
    """

    def __init__(self, db_path: str, user_id: str):
        self.db_path = db_path
        self.user_id = user_id
        # Kept so existing callers that read `.root` still work.
        self.root = os.path.dirname(db_path) or "."

    def portfolios(self) -> SqlitePortfolioManager:
        """A loaded manager. Call .save() after mutating."""
        return SqlitePortfolioManager(self.db_path, self.user_id).load()

    def alerts(self) -> AlertRepository:
        return AlertRepository(self.db_path, self.user_id)

    def snapshots(self) -> SnapshotRepository:
        return SnapshotRepository(self.db_path, self.user_id)

    def journal(self) -> JournalRepository:
        return JournalRepository(self.db_path, self.user_id)

    def watchlist(self) -> WatchlistRepository:
        return WatchlistRepository(self.db_path, self.user_id)

    def tracker(self) -> TrackerRepository:
        """Hand-picked touches this user is following day by day. User-scoped
        on purpose: the scan is shared market data, the selection is personal."""
        return TrackerRepository(self.db_path, self.user_id)

    def prices(self) -> PriceRepository:
        """Market data is SHARED across users — a price is a fact about the
        market, not about a person — so this one isn't user-scoped."""
        return PriceRepository(self.db_path)


def user_repository(root: str) -> UserRepository:
    return UserRepository(db_file(root))


def all_user_ids(root: str) -> list[str]:
    """Every user — used by the background jobs, which must sweep across ALL
    users rather than assuming a single one."""
    return UserRepository(db_file(root)).all_ids()
