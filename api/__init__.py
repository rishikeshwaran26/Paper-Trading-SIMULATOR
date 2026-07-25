"""Flask REST API that exposes the Phase 1 engine over HTTP.

Nothing in engine/ changes shape for the web — the API is a *second caller* of
the same Portfolio / PortfolioManager methods the CLI uses. It translates HTTP
requests into engine calls and engine results (or errors) back into JSON.

Use the application factory pattern (create_app) rather than a module-level
`app = Flask(...)`: the factory lets tests spin up an app pointed at a throwaway
data directory, and lets you configure CORS / paths without editing code.

Note the config is a DATA_ROOT (a directory), not a file path. Every concrete
file location is derived per-user from it (api/paths.py) — that indirection is
what keeps multi-user from being a rewrite.
"""

from __future__ import annotations

import os

from flask import Flask, jsonify
from flask_cors import CORS

from engine.backtest import YFinanceBacktestData
from engine.bhavcopy import NSEBhavcopyData
from engine.db import init_db
from engine.prices import build_source
from engine.screener import YFinanceScreenerData

from .auth import load_or_create_secret
from .paths import db_file
from .auth_routes import auth_bp
from .backtest_service import BacktestService
from .band_study_service import BandStudyService
from .errors import register_error_handlers
from .jobs import BackgroundWorker, should_start_worker
from .routes import bhavcopy_cache_dir, bp, screener_cache_path
from .screener_service import ScreenerService

DEFAULT_DATA_ROOT = "data"

# The React dev servers we allow during local development. CRA defaults to 3000,
# Vite to 5173. We list them explicitly instead of "*", because browsers block
# credentialed requests to a wildcard origin and being explicit is safer.
DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def create_app(
    data_root: str = DEFAULT_DATA_ROOT,
    cors_origins: list[str] | None = None,
    start_worker: bool = False,
    alert_interval: int = 15,
    snapshot_interval: int = 300,
    price_interval: int = 60,
    price_source_mode: str | None = None,
    price_cache_ttl: int = 60,
) -> Flask:
    app = Flask(__name__)
    os.makedirs(data_root, exist_ok=True)
    app.config["DATA_ROOT"] = data_root

    # Create the SQLite schema if it doesn't exist. Idempotent, so it's safe to
    # run on every startup — a fresh install just works with no setup step.
    init_db(db_file(data_root))

    # Live price source. Configurable via env so you can force a mode without
    # editing code: PAPERTRADING_PRICE_SOURCE=nse|yfinance|auto|manual
    mode = price_source_mode or os.environ.get("PAPERTRADING_PRICE_SOURCE", "auto")
    app.config["PRICE_SOURCE_MODE"] = mode
    # "manual" means tests and offline use never touch the network.
    app.config["PRICE_SOURCE"] = None if mode == "manual" else build_source(mode, price_cache_ttl)

    # Screener: one long-lived service (owns the scan thread + progress) and a
    # market-data provider. In manual/offline mode the provider is None, which
    # makes POST /screener/scan return 503 instead of hitting the network — tests
    # inject a fake provider into SCREENER_DATA when they want to exercise a scan.
    app.config["SCREENER_SERVICE"] = ScreenerService()
    app.config["SCREENER_DATA"] = None if mode == "manual" else YFinanceScreenerData()

    # Backtest: same async-service pattern as the screener, its own market-data
    # provider (historical bars over an arbitrary date range, not "recent").
    app.config["BACKTEST_SERVICE"] = BacktestService()
    app.config["BACKTEST_DATA"] = None if mode == "manual" else YFinanceBacktestData()

    # Band-touch study (Phase 1). Two sources: BACKTEST_DATA (Yahoo, above) for
    # the "yahoo" path and for the bhavcopy path's narrow corporate-action
    # cross-check; BHAVCOPY_DATA (NSE's own daily file) for the default,
    # rate-limit-immune "bhavcopy" path. "manual" mode disables the network
    # source but bhavcopy still reads from its local cache if one exists.
    app.config["BAND_STUDY_SERVICE"] = BandStudyService()
    app.config["BHAVCOPY_DATA"] = NSEBhavcopyData(bhavcopy_cache_dir(data_root))

    # Signing key for auth tokens. Persisted so a restart doesn't log you out.
    app.config["SECRET_KEY"] = load_or_create_secret(data_root)

    # CORS: React runs on a different origin (port) than Flask, so the browser
    # refuses to read our responses unless we send Access-Control-Allow-Origin.
    # flask-cors adds it and answers the OPTIONS preflight.
    CORS(app, resources={r"/*": {"origins": cors_origins or DEFAULT_CORS_ORIGINS}})

    register_error_handlers(app)
    app.register_blueprint(auth_bp)  # /auth/* — the only unauthenticated routes
    app.register_blueprint(bp)

    # Background jobs are opt-in so the test suite never starts a thread.
    app.worker = None
    if start_worker:
        worker = BackgroundWorker(
            data_root,
            alert_interval,
            snapshot_interval,
            price_interval,
            source=app.config["PRICE_SOURCE"],
            screener_service=app.config["SCREENER_SERVICE"],
            screener_data=app.config["SCREENER_DATA"],
            screener_cache_path=screener_cache_path(data_root),
        )
        worker.start()
        app.worker = worker

    @app.get("/")
    def index():
        return jsonify(
            {
                "service": "paper-trading-api",
                "status": "ok",
                "endpoints": [
                    "POST   /auth/register",
                    "POST   /auth/login",
                    "GET    /auth/me",
                    "GET    /auth/status",
                    "POST   /strategies",
                    "GET    /strategies",
                    "GET    /strategies/<name>",
                    "DELETE /strategies/<name>",
                    "POST   /strategies/<name>/buy",
                    "POST   /strategies/<name>/sell",
                    "POST   /strategies/<name>/short",
                    "POST   /strategies/<name>/cover",
                    "GET    /strategies/<name>/transactions",
                    "POST   /strategies/<name>/transactions/<id>/review",
                    "GET    /strategies/<name>/analytics",
                    "GET    /leaderboard",
                    "GET    /snapshots",
                    "POST   /snapshots/capture",
                    "GET    /alerts",
                    "POST   /alerts",
                    "POST   /alerts/<id>/dismiss",
                    "DELETE /alerts/<id>",
                    "GET    /prices",
                    "PUT    /prices/<symbol>",
                    "POST   /prices/refresh",
                    "GET    /prices/sources",
                    "GET    /prices/<symbol>/history",
                    "GET    /prices/quote/<symbol>",
                    "GET    /symbols/search",
                    "GET    /watchlists",
                    "POST   /watchlists",
                    "DELETE /watchlists/<id>",
                    "POST   /watchlists/<id>/symbols",
                    "DELETE /watchlists/<id>/symbols/<symbol>",
                    "GET    /screener",
                    "POST   /screener/scan",
                    "GET    /screener/status",
                    "POST   /backtest/run",
                    "GET    /backtest/status",
                    "GET    /backtest/runs",
                    "GET    /backtest/runs/<id>",
                    "POST   /band-study/run",
                    "GET    /band-study/status",
                    "GET    /band-study/runs",
                    "GET    /band-study/runs/<id>",
                ],
            }
        )

    return app
