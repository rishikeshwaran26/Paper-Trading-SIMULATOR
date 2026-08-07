"""The REST endpoints.

Three design ideas run through this file:

REST resource conventions
-------------------------
URLs name *resources* (nouns), HTTP methods are the *verbs*:
  - a collection:  /strategies         (GET = list, POST = create one)
  - an item:       /strategies/<name>  (GET = read that one)
  - a sub-action:  /strategies/<name>/buy   (POST = append a transaction)
We don't put verbs in paths like /createStrategy — the method already says what
we're doing, so GET (safe, cacheable) stays distinct from POST (creates data).

Two layers of validation
-------------------------
  1. SYNTACTIC (api/validation.py): is the body JSON? right types? -> 400 before
     the engine is ever touched. Never trust a client.
  2. SEMANTIC (the engine): enough cash? enough shares? -> typed engine errors
     that the handlers map to 409/400.
The engine stays the single source of truth for trading rules.

Every route is user-scoped
--------------------------
@require_auth resolves the bearer token to a user and puts g.data on the
request. No route ever opens a hardcoded file path, which is what makes
multi-user a data change rather than a rewrite.
"""

from __future__ import annotations

import os
from datetime import date

from flask import Blueprint, current_app, g, jsonify, request

from engine import backtest, symbols
from engine.alerts import ABOVE, BELOW
from engine.backtest import DEFAULT_THRESHOLD_PCT, DEFAULT_WINDOW_DAYS
from engine.prices import NsePythonPriceSource, market_is_open
from engine.journal import (
    performance_by_confidence,
    performance_by_side,
    performance_by_tag,
    winners_vs_losers,
)
from engine.manager import DEFAULT_STARTING_CASH
from engine.portfolio import Portfolio
from engine.repository import BacktestRepository, BandStudyRepository

from .auth import require_auth
from .errors import ApiError
from .jobs import capture_snapshot_for_user, refresh_live_prices
from .paths import db_file
from .validation import json_body, opt_str, opt_tags, req_int, req_num, req_str

# Where the cached NSE universe CSV lives, relative to the data root.
UNIVERSE_CACHE_FILE = "nse_universe.csv"


def universe_cache_path(root: str) -> str:
    return os.path.join(root, UNIVERSE_CACHE_FILE)


# Where downloaded NSE bhavcopy day-files are cached, relative to the data root.
BHAVCOPY_CACHE_DIR = "bhavcopy_cache"


def bhavcopy_cache_dir(root: str) -> str:
    return os.path.join(root, BHAVCOPY_CACHE_DIR)


bp = Blueprint("api", __name__)


# --- per-request wiring (all derived from g.data, never hardcoded) ----------
# g.data is the authenticated user's UserData, built by @require_auth. Routes
# never construct a repository from a raw path or open a connection themselves.
def _manager():
    return g.data.portfolios()


def _price_store():
    return g.data.prices()


def _alert_store():
    return g.data.alerts()


def _snapshot_store():
    return g.data.snapshots()


def _prices_for(manager, store) -> dict[str, float]:
    """Current price for every symbol held anywhere. Missing prices are simply
    absent — the engine values those holdings at cost, so nothing crashes."""
    symbols = set()
    for p in manager.portfolios.values():
        symbols.update(p.holdings.keys())
    return store.get_prices(list(symbols))


def _get_portfolio(manager, name: str) -> Portfolio:
    p = manager.portfolios.get(name)
    if p is None:
        raise ApiError(404, "NotFound", f"no strategy named '{name}'")
    return p


# --- response shaping --------------------------------------------------------
def _summary(p: Portfolio, prices: dict[str, float]) -> dict:
    return {
        "name": p.name,
        "cash": p.cash,
        "starting_cash": p.starting_cash,
        "holdings_value": p.holdings_value(prices),
        "total_value": p.total_value(prices),
        "return_pct": p.total_return_pct(prices),
        "realized_pnl": p.realized_pnl(),
        "unrealized_pnl": p.unrealized_pnl(prices),
        "num_holdings": len(p.holdings),
    }


def _detail(p: Portfolio, prices: dict[str, float]) -> dict:
    holdings = []
    for symbol, h in p.holdings.items():
        ltp = prices.get(symbol, h.avg_price)
        holdings.append(
            {
                "symbol": symbol,
                "side": "short" if h.is_short else "long",
                # quantity/avg_price stay signed/positive as the engine stores
                # them; the frontend shows abs(quantity) with a side badge.
                "quantity": h.quantity,
                "avg_price": h.avg_price,
                "last_price": ltp,
                "priced": symbol in prices,
                "market_value": h.market_value(ltp),
                "unrealized_pnl": h.unrealized_pnl(ltp),
            }
        )
    d = _summary(p, prices)
    d["holdings"] = holdings
    d["transactions"] = [t.to_dict() for t in p.transactions]
    return d


# --- strategy collection -----------------------------------------------------
@bp.post("/strategies")
@require_auth
def create_strategy():
    body = json_body()
    name = req_str(body, "name")
    starting_cash = float(body.get("starting_cash", DEFAULT_STARTING_CASH))

    manager = _manager()
    if name in manager.portfolios:
        raise ApiError(409, "StrategyExists", f"strategy '{name}' already exists")

    p = manager.create_strategy(name, starting_cash)
    manager.save()
    prices = _prices_for(manager, _price_store())
    resp = jsonify(_summary(p, prices))
    resp.status_code = 201
    resp.headers["Location"] = f"/strategies/{name}"
    return resp


@bp.get("/strategies")
@require_auth
def list_strategies():
    manager = _manager()
    prices = _prices_for(manager, _price_store())
    return jsonify({"strategies": [_summary(p, prices) for p in manager.portfolios.values()]})


@bp.get("/strategies/<name>")
@require_auth
def get_strategy(name: str):
    manager = _manager()
    p = _get_portfolio(manager, name)
    prices = _prices_for(manager, _price_store())
    return jsonify(_detail(p, prices))


@bp.delete("/strategies/<name>")
@require_auth
def delete_strategy(name: str):
    """Permanently remove a strategy — its holdings, transactions and closed
    lots all cascade-delete with it (see the FK ON DELETE CASCADE in db.py).
    There's no undo, which is why the frontend gates this behind a confirm."""
    manager = _manager()
    _get_portfolio(manager, name)  # 404 if it doesn't exist / isn't yours
    manager.delete_strategy(name)
    manager.save()
    return jsonify({"deleted": name})


# --- trading -----------------------------------------------------------------
@bp.post("/strategies/<name>/buy")
@require_auth
def buy(name: str):
    # reason + confidence are required because the trade journal is the core
    # feature of this project — a buy without a thesis defeats the point.
    body = json_body()
    symbol = req_str(body, "symbol")
    quantity = req_int(body, "quantity")
    price = req_num(body, "price")
    reason = req_str(body, "reason")
    confidence = req_int(body, "confidence")
    tags = opt_tags(body)

    manager = _manager()
    p = _get_portfolio(manager, name)
    txn = p.buy(symbol, quantity, price, reason, confidence, tags)
    manager.save()
    _price_store().set_price(symbol, price)

    resp = jsonify({"transaction": txn.to_dict(), "cash": p.cash})
    resp.status_code = 201
    return resp


@bp.post("/strategies/<name>/sell")
@require_auth
def sell(name: str):
    body = json_body()
    symbol = req_str(body, "symbol")
    quantity = req_int(body, "quantity")
    price = req_num(body, "price")
    reason = req_str(body, "reason")

    manager = _manager()
    p = _get_portfolio(manager, name)
    txn = p.sell(symbol, quantity, price, reason)
    manager.save()
    _price_store().set_price(symbol, price)

    resp = jsonify({"transaction": txn.to_dict(), "cash": p.cash, "realized_pnl": txn.realized_pnl})
    resp.status_code = 201
    return resp


@bp.post("/strategies/<name>/short")
@require_auth
def short(name: str):
    # Same required fields as buy() — a short is still an opening trade with a
    # thesis, just betting on a fall instead of a rise.
    body = json_body()
    symbol = req_str(body, "symbol")
    quantity = req_int(body, "quantity")
    price = req_num(body, "price")
    reason = req_str(body, "reason")
    confidence = req_int(body, "confidence")
    tags = opt_tags(body)

    manager = _manager()
    p = _get_portfolio(manager, name)
    txn = p.short(symbol, quantity, price, reason, confidence, tags)
    manager.save()
    _price_store().set_price(symbol, price)

    resp = jsonify({"transaction": txn.to_dict(), "cash": p.cash})
    resp.status_code = 201
    return resp


@bp.post("/strategies/<name>/cover")
@require_auth
def cover(name: str):
    body = json_body()
    symbol = req_str(body, "symbol")
    quantity = req_int(body, "quantity")
    price = req_num(body, "price")
    reason = req_str(body, "reason")

    manager = _manager()
    p = _get_portfolio(manager, name)
    txn = p.cover(symbol, quantity, price, reason)
    manager.save()
    _price_store().set_price(symbol, price)

    resp = jsonify({"transaction": txn.to_dict(), "cash": p.cash, "realized_pnl": txn.realized_pnl})
    resp.status_code = 201
    return resp


@bp.get("/strategies/<name>/transactions")
@require_auth
def transactions(name: str):
    manager = _manager()
    p = _get_portfolio(manager, name)
    txns = p.transactions
    t_type = request.args.get("type")
    symbol = request.args.get("symbol")
    if t_type:
        txns = [t for t in txns if t.type == t_type.upper()]
    if symbol:
        txns = [t for t in txns if t.symbol == symbol.strip().upper()]
    return jsonify({"transactions": [t.to_dict() for t in txns]})


@bp.post("/strategies/<name>/transactions/<txn_id>/review")
@require_auth
def review(name: str, txn_id: str):
    body = json_body()
    notes = req_str(body, "notes")
    manager = _manager()
    p = _get_portfolio(manager, name)
    if not any(t.id == txn_id for t in p.transactions):
        raise ApiError(404, "NotFound", f"no transaction '{txn_id}' in '{name}'")
    txn = p.review(txn_id, notes)
    manager.save()
    return jsonify({"transaction": txn.to_dict()})


# --- journal analytics (the insight layer) ----------------------------------
@bp.get("/strategies/<name>/analytics")
@require_auth
def analytics(name: str):
    """Everything needed to answer 'what kind of trader am I?':
    win rate + avg P&L by tag, by confidence, winners vs losers, and long vs
    short performance side-by-side (a short strategy can be judged on its own
    terms rather than blended into your long-side numbers)."""
    manager = _manager()
    p = _get_portfolio(manager, name)
    return jsonify(
        {
            "by_confidence": [s.to_dict() for s in performance_by_confidence(p.transactions)],
            "by_tag": [s.to_dict() for s in performance_by_tag(p.transactions)],
            "winners_vs_losers": winners_vs_losers(p.transactions),
            "by_side": performance_by_side(p.transactions),
        }
    )


# --- leaderboard -------------------------------------------------------------
@bp.get("/leaderboard")
@require_auth
def leaderboard():
    manager = _manager()
    prices = _prices_for(manager, _price_store())
    return jsonify({"leaderboard": manager.leaderboard(prices)})


# --- snapshots + comparison --------------------------------------------------
@bp.get("/snapshots")
@require_auth
def snapshots():
    """Chart-ready history. ?strategies=A,B narrows the overlay.

    Returns `series` already reshaped into one row per date with one key per
    strategy — exactly what recharts wants, so the frontend does no reshaping.
    """
    wanted = request.args.get("strategies")
    names = [s.strip() for s in wanted.split(",") if s.strip()] if wanted else None
    store = _snapshot_store()
    return jsonify(
        {
            "series": store.series(names),
            "strategies": store.strategy_names(),
            "count": len(store.snapshots),
        }
    )


@bp.post("/snapshots/capture")
@require_auth
def capture_snapshot():
    """Force a snapshot now instead of waiting for the daily job — useful for
    testing the comparison chart without waiting a day."""
    taken = capture_snapshot_for_user(g.data.root, g.user_id)
    return jsonify({"captured": [s.to_dict() for s in taken]}), 201


# --- price alerts ------------------------------------------------------------
@bp.get("/alerts")
@require_auth
def list_alerts():
    store = _alert_store()
    return jsonify(
        {
            "alerts": [a.to_dict() for a in store.alerts],
            # what the in-app banner shows: fired but not yet acknowledged
            "triggered": [a.to_dict() for a in store.triggered()],
        }
    )


@bp.post("/alerts")
@require_auth
def create_alert():
    body = json_body()
    symbol = req_str(body, "symbol")
    target_price = req_num(body, "target_price")
    direction = opt_str(body, "direction", ABOVE).lower()
    note = opt_str(body, "note")
    if direction not in (ABOVE, BELOW):
        raise ApiError(400, "BadRequest", f"direction must be '{ABOVE}' or '{BELOW}'")
    if target_price <= 0:
        raise ApiError(400, "BadRequest", "target_price must be positive")
    alert = _alert_store().add(symbol, target_price, direction, note)
    return jsonify({"alert": alert.to_dict()}), 201


@bp.post("/alerts/<alert_id>/dismiss")
@require_auth
def dismiss_alert(alert_id: str):
    alert = _alert_store().dismiss(alert_id)
    if not alert:
        raise ApiError(404, "NotFound", f"no alert '{alert_id}'")
    return jsonify({"alert": alert.to_dict()})


@bp.delete("/alerts/<alert_id>")
@require_auth
def delete_alert(alert_id: str):
    if not _alert_store().remove(alert_id):
        raise ApiError(404, "NotFound", f"no alert '{alert_id}'")
    return jsonify({"deleted": alert_id})


# --- symbol search -----------------------------------------------------------
@bp.get("/symbols/search")
@require_auth
def search_symbols():
    """Autocomplete: ?q=rel -> RELIANCE first. Backed by a bundled list of the
    ~150 most-traded NSE names (engine/symbols.py) — instant and offline, but
    not the whole exchange; unknown symbols can still be typed manually."""
    q = request.args.get("q", "")
    return jsonify({"results": symbols.search(q)})


# --- watchlist ---------------------------------------------------------------
def _quotes_for(syms: list[str]) -> list[dict]:
    """Groww-style quote rows: last price + change vs previous close.

    Current prices come from the shared price table (the background worker keeps
    it fresh); previous closes come from the live source in ONE batched call,
    cached 10 minutes. The route itself never fans out per-symbol.
    """
    if not syms:
        return []
    stored = _price_store().get_prices(syms)
    source = current_app.config.get("PRICE_SOURCE")
    prev = {}
    if source and hasattr(source, "get_prev_closes"):
        try:
            prev = source.get_prev_closes(syms)
        except Exception:
            prev = {}
    rows = []
    for s in syms:
        price = stored.get(s)
        pc = prev.get(s)
        change = round(price - pc, 2) if price is not None and pc else None
        rows.append(
            {
                "symbol": s,
                "name": symbols.name_of(s),
                "price": price,
                "prev_close": pc,
                "change": change,
                "change_pct": round(change / pc * 100, 2) if change is not None and pc else None,
            }
        )
    return rows


@bp.get("/watchlists")
@require_auth
def list_watchlists():
    """Every one of the user's named watchlists, each with live quotes for its
    own symbols. No auto-created default — a brand-new user just sees an empty
    list and the UI prompts them to create the first one."""
    lists = g.data.watchlist().list_all()
    for wl in lists:
        wl["stocks"] = _quotes_for(wl.pop("symbols"))
    return jsonify({"watchlists": lists, "market_open": market_is_open()})


@bp.post("/watchlists")
@require_auth
def create_watchlist():
    body = json_body()
    wl = g.data.watchlist().create(req_str(body, "name"))
    wl["stocks"] = wl.pop("symbols")
    return jsonify({"watchlist": wl}), 201


@bp.delete("/watchlists/<int:watchlist_id>")
@require_auth
def delete_watchlist(watchlist_id: int):
    if not g.data.watchlist().delete(watchlist_id):
        raise ApiError(404, "NotFound", f"no watchlist with id {watchlist_id}")
    return jsonify({"deleted": watchlist_id})


@bp.post("/watchlists/<int:watchlist_id>/symbols")
@require_auth
def add_watchlist_symbol(watchlist_id: int):
    body = json_body()
    sym = g.data.watchlist().add_symbol(watchlist_id, req_str(body, "symbol"))
    if sym is None:
        raise ApiError(404, "NotFound", f"no watchlist with id {watchlist_id}")
    # Fetch a price immediately so the new row isn't blank until the next
    # worker tick.
    source = current_app.config.get("PRICE_SOURCE")
    if source:
        try:
            got = source.get_prices([sym])
            if got:
                _price_store().set_many(got)
        except Exception:
            pass
    return jsonify({"symbol": sym}), 201


@bp.delete("/watchlists/<int:watchlist_id>/symbols/<symbol>")
@require_auth
def remove_watchlist_symbol(watchlist_id: int, symbol: str):
    if not g.data.watchlist().remove_symbol(watchlist_id, symbol):
        raise ApiError(404, "NotFound", f"'{symbol.upper()}' is not in watchlist {watchlist_id}")
    return jsonify({"removed": symbol.strip().upper()})


# --- backtest (does the mean-reversion idea actually hold up historically?) --
# Same GLOBAL-market-data reasoning: a backtest of a given past date is the
# same for every user, so it's shared, not per-user.
def _backtest_repo() -> BacktestRepository:
    return BacktestRepository(db_file(g.data.root))


@bp.post("/backtest/run")
@require_auth
def start_backtest():
    """Replay a past date's whole-market movers in the background. Returns 202
    with the initial status; the client then polls GET /backtest/status."""
    body = json_body()
    date_str = req_str(body, "target_date")
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        raise ApiError(400, "BadRequest", "'target_date' must be an ISO date, e.g. '2026-03-01'")
    if target_date >= date.today():
        raise ApiError(400, "BadRequest", "'target_date' must be in the past — there's nothing to replay yet")

    direction = opt_str(body, "direction", "up")
    if direction not in ("up", "down"):
        raise ApiError(400, "BadRequest", "'direction' must be 'up' or 'down'")
    threshold_pct = float(body.get("threshold_pct", DEFAULT_THRESHOLD_PCT))
    window_days = int(body.get("window_days", DEFAULT_WINDOW_DAYS))
    if not (1 <= window_days <= 90):
        raise ApiError(400, "BadRequest", "'window_days' must be between 1 and 90")

    service = current_app.config["BACKTEST_SERVICE"]
    data = current_app.config.get("BACKTEST_DATA")
    if data is None:
        raise ApiError(
            503, "BacktestUnavailable",
            "the backtest needs a market data source; it's disabled in manual/offline mode",
        )
    started = service.start(
        db_path=db_file(g.data.root),
        cache_path=universe_cache_path(g.data.root),
        data=data,
        target_date=target_date,
        direction=direction,
        threshold_pct=threshold_pct,
        window_days=window_days,
    )
    if not started:
        raise ApiError(409, "RunInProgress", "a backtest is already running — check its status")
    return jsonify({"started": True, "status": service.snapshot()}), 202


@bp.get("/backtest/status")
@require_auth
def backtest_status():
    """Live progress of the current/last backtest (for the progress bar)."""
    return jsonify(current_app.config["BACKTEST_SERVICE"].snapshot())


@bp.get("/backtest/runs")
@require_auth
def list_backtest_runs():
    """Every backtest run so far (any status), newest first — a history list
    to revisit past results without re-running them."""
    return jsonify({"runs": _backtest_repo().list_runs()})


@bp.get("/backtest/runs/<int:run_id>")
@require_auth
def get_backtest_run(run_id: int):
    detail = _backtest_repo().get_run(run_id)
    if detail is None:
        raise ApiError(404, "NotFound", f"no backtest run with id {run_id}")
    return jsonify(detail)


# --- band-touch study (Phase 1: tight band, date range, next-day-only) ------
def _band_study_repo() -> BandStudyRepository:
    return BandStudyRepository(db_file(g.data.root))


@bp.post("/band-study/run")
@require_auth
def start_band_study():
    """Scan a date range for stocks whose intraday move touched [band_low,
    band_high]% and record what their very next trading day did. Runs in the
    background; the client polls GET /band-study/status."""
    body = json_body()
    start_str = req_str(body, "start_date")
    end_str = req_str(body, "end_date")
    try:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
    except ValueError:
        raise ApiError(400, "BadRequest", "'start_date'/'end_date' must be ISO dates, e.g. '2026-01-01'")
    if start_date > end_date:
        raise ApiError(400, "BadRequest", "'start_date' must not be after 'end_date'")
    if end_date >= date.today():
        raise ApiError(400, "BadRequest", "'end_date' must be in the past — there's nothing to replay yet")

    direction = opt_str(body, "direction", "up")
    if direction not in ("up", "down"):
        raise ApiError(400, "BadRequest", "'direction' must be 'up' or 'down'")
    band_low = float(body.get("band_low", backtest.DEFAULT_BAND_LOW))
    band_high = float(body.get("band_high", backtest.DEFAULT_BAND_HIGH))
    if not (0 < band_low < band_high <= 100):
        raise ApiError(400, "BadRequest", "'band_low' must be less than 'band_high', both between 0 and 100")
    source = opt_str(body, "source", "bhavcopy")
    if source not in ("bhavcopy", "yahoo"):
        raise ApiError(400, "BadRequest", "'source' must be 'bhavcopy' or 'yahoo'")
    follow_days = int(body.get("follow_days", backtest.DEFAULT_FOLLOW_DAYS))
    if not (1 <= follow_days <= backtest.MAX_FOLLOW_DAYS):
        raise ApiError(
            400, "BadRequest",
            f"'follow_days' must be between 1 and {backtest.MAX_FOLLOW_DAYS}",
        )
    before_days = int(body.get("before_days", backtest.DEFAULT_BEFORE_DAYS))
    if not (0 <= before_days <= backtest.MAX_BEFORE_DAYS):
        raise ApiError(
            400, "BadRequest",
            f"'before_days' must be between 0 and {backtest.MAX_BEFORE_DAYS}",
        )

    service = current_app.config["BAND_STUDY_SERVICE"]
    data = current_app.config.get("BACKTEST_DATA")  # Yahoo: sole source for "yahoo", cross-check for "bhavcopy"
    bhav_data = current_app.config.get("BHAVCOPY_DATA")
    if source == "yahoo" and data is None:
        raise ApiError(
            503, "BandStudyUnavailable",
            "the yahoo-sourced band study needs a live market data source; it's disabled in manual/offline mode",
        )
    if source == "bhavcopy" and bhav_data is None:
        raise ApiError(503, "BandStudyUnavailable", "the bhavcopy data source is not configured")
    started = service.start(
        db_path=db_file(g.data.root),
        cache_path=universe_cache_path(g.data.root),
        data=data,
        start_date=start_date,
        end_date=end_date,
        band_low=band_low,
        band_high=band_high,
        direction=direction,
        source=source,
        bhav_data=bhav_data,
        follow_days=follow_days,
        before_days=before_days,
    )
    if not started:
        raise ApiError(409, "RunInProgress", "a band study is already running — check its status")
    return jsonify({"started": True, "status": service.snapshot()}), 202


@bp.get("/band-study/status")
@require_auth
def band_study_status():
    return jsonify(current_app.config["BAND_STUDY_SERVICE"].snapshot())


@bp.get("/band-study/runs")
@require_auth
def list_band_study_runs():
    return jsonify({"runs": _band_study_repo().list_runs()})


@bp.get("/band-study/runs/<int:run_id>")
@require_auth
def get_band_study_run(run_id: int):
    detail = _band_study_repo().get_run(run_id)
    if detail is None:
        raise ApiError(404, "NotFound", f"no band study run with id {run_id}")
    return jsonify(detail)


@bp.get("/band-study/runs/<int:run_id>/caps")
@require_auth
def band_study_run_caps(run_id: int):
    """Return {symbol: cap_bucket} for every symbol touched in this run.

    Fetched from Yahoo Finance (touched symbols only, never the whole exchange)
    and cached for 7 days.  Served as a separate endpoint so the main run
    detail loads instantly and cap badges appear a moment later.
    """
    from engine.market_cap import cap_buckets_for

    detail = _band_study_repo().get_run(run_id)
    if detail is None:
        raise ApiError(404, "NotFound", f"no band study run with id {run_id}")
    touches = detail.get("touches") or []
    syms = list({t["symbol"] for t in touches})
    data_root = current_app.config.get("DATA_ROOT", "data")
    cache_file = os.path.join(data_root, "market_cap_cache.json")
    caps = cap_buckets_for(syms, cache_file)
    return jsonify({"caps": caps})


@bp.get("/band-study/runs/<int:run_id>/week52")
@require_auth
def band_study_run_week52(run_id: int):
    """Where each touch sat in its own 52-week range, as of the touch date.

    Rebuilt from the bhavcopy cache rather than stored on the run, so runs
    scanned before this existed get the labels too, with no re-scan. Separate
    endpoint for the same reason as caps: the run detail should not wait on it.
    """
    from engine.week52 import classify

    detail = _band_study_repo().get_run(run_id)
    if detail is None:
        raise ApiError(404, "NotFound", f"no band study run with id {run_id}")
    touches = detail.get("touches") or []
    data_root = current_app.config.get("DATA_ROOT", "data")
    labels = classify(
        touches,
        os.path.join(data_root, "bhavcopy_cache"),
        os.path.join(data_root, "week52_cache.json"),
    )
    return jsonify({"week52": labels})


# --- research (a user's trading-idea notebook: idea -> year -> month) -------
def _research_repo():
    return g.data.research()


@bp.get("/research/ideas")
@require_auth
def list_research_ideas():
    """Every idea this user has, each with its checklist template and a
    light periods list — enough to render the whole sidebar tree in one call."""
    return jsonify({"ideas": _research_repo().list_ideas()})


@bp.post("/research/ideas")
@require_auth
def create_research_idea():
    body = json_body()
    idea = _research_repo().create_idea(req_str(body, "name"))
    return jsonify({"idea": idea}), 201


@bp.delete("/research/ideas/<int:idea_id>")
@require_auth
def delete_research_idea(idea_id: int):
    if not _research_repo().delete_idea(idea_id):
        raise ApiError(404, "NotFound", f"no research idea with id {idea_id}")
    return jsonify({"deleted": idea_id})


@bp.post("/research/ideas/<int:idea_id>/checklist")
@require_auth
def add_research_checklist_item(idea_id: int):
    body = json_body()
    item = _research_repo().add_checklist_item(idea_id, req_str(body, "text"))
    if item is None:
        raise ApiError(404, "NotFound", f"no research idea with id {idea_id}")
    return jsonify({"item": item}), 201


@bp.delete("/research/checklist/<int:item_id>")
@require_auth
def delete_research_checklist_item(item_id: int):
    if not _research_repo().delete_checklist_item(item_id):
        raise ApiError(404, "NotFound", f"no checklist item with id {item_id}")
    return jsonify({"deleted": item_id})


@bp.post("/research/ideas/<int:idea_id>/periods")
@require_auth
def create_research_period(idea_id: int):
    """Open (or re-open) one calendar month under an idea. Idempotent: asking
    for a month that already exists just returns it, rather than erroring —
    "create Jan 2026" twice should land you on the same month."""
    body = json_body()
    try:
        period = _research_repo().create_period(idea_id, req_int(body, "year"), req_int(body, "month"))
    except ValueError as e:
        raise ApiError(400, "BadRequest", str(e))
    if period is None:
        raise ApiError(404, "NotFound", f"no research idea with id {idea_id}")
    return jsonify({"period": period}), 201


@bp.get("/research/periods/<int:period_id>")
@require_auth
def get_research_period(period_id: int):
    period = _research_repo().get_period(period_id)
    if period is None:
        raise ApiError(404, "NotFound", f"no research period with id {period_id}")
    return jsonify({"period": period})


@bp.delete("/research/periods/<int:period_id>")
@require_auth
def delete_research_period(period_id: int):
    if not _research_repo().delete_period(period_id):
        raise ApiError(404, "NotFound", f"no research period with id {period_id}")
    return jsonify({"deleted": period_id})


@bp.put("/research/periods/<int:period_id>")
@require_auth
def update_research_period(period_id: int):
    """Partial update: only the fields present in the body are touched, so
    saving a note doesn't clobber the total count and vice versa."""
    body = json_body()
    notes = body.get("notes") if isinstance(body.get("notes"), str) else None
    total_count = body.get("total_count")
    if total_count is not None and (isinstance(total_count, bool) or not isinstance(total_count, int)):
        raise ApiError(400, "BadRequest", "'total_count' must be a whole number")
    if not _research_repo().update_period(period_id, notes=notes, total_count=total_count):
        raise ApiError(404, "NotFound", f"no research period with id {period_id}")
    return jsonify({"updated": period_id})


@bp.post("/research/periods/<int:period_id>/rows")
@require_auth
def add_research_row(period_id: int):
    body = json_body()
    pct = body.get("pct")
    if pct is not None and (isinstance(pct, bool) or not isinstance(pct, (int, float))):
        raise ApiError(400, "BadRequest", "'pct' must be a number")
    try:
        row = _research_repo().add_row(
            period_id,
            req_str(body, "symbol"),
            pct=float(pct) if pct is not None else None,
            remark=opt_str(body, "remark"),
            touch_date=opt_str(body, "touch_date") or None,
        )
    except ValueError as e:
        raise ApiError(400, "BadRequest", str(e))
    if row is None:
        raise ApiError(404, "NotFound", f"no research period with id {period_id}")
    return jsonify({"row": row}), 201


@bp.post("/research/periods/<int:period_id>/attach-run/<int:run_id>")
@require_auth
def attach_research_run(period_id: int, run_id: int):
    """Copies a past Backtest run's touches in as rows — the "not typing"
    path. Rows are copied, not referenced, so re-scanning or deleting the run
    later never breaks the research entry."""
    detail = _band_study_repo().get_run(run_id)
    if detail is None:
        raise ApiError(404, "NotFound", f"no band study run with id {run_id}")
    touches = detail.get("touches") or []
    added = _research_repo().bulk_add_rows(period_id, touches, source_run_id=run_id)
    if added is None:
        raise ApiError(404, "NotFound", f"no research period with id {period_id}")
    period = _research_repo().get_period(period_id)
    return jsonify({"added": added, "period": period}), 201


@bp.put("/research/rows/<int:row_id>")
@require_auth
def update_research_row(row_id: int):
    body = json_body()
    pct = body.get("pct")
    if pct is not None and (isinstance(pct, bool) or not isinstance(pct, (int, float))):
        raise ApiError(400, "BadRequest", "'pct' must be a number")
    remark = body.get("remark") if isinstance(body.get("remark"), str) else None
    if not _research_repo().update_row(row_id, pct=float(pct) if pct is not None else None, remark=remark):
        raise ApiError(404, "NotFound", f"no research row with id {row_id}")
    return jsonify({"updated": row_id})


@bp.delete("/research/rows/<int:row_id>")
@require_auth
def delete_research_row(row_id: int):
    if not _research_repo().delete_row(row_id):
        raise ApiError(404, "NotFound", f"no research row with id {row_id}")
    return jsonify({"deleted": row_id})


@bp.put("/research/periods/<int:period_id>/checklist/<int:item_id>")
@require_auth
def set_research_check(period_id: int, item_id: int):
    body = json_body()
    done = body.get("done")
    if not isinstance(done, bool):
        raise ApiError(400, "BadRequest", "'done' must be true or false")
    if not _research_repo().set_check(period_id, item_id, done):
        raise ApiError(404, "NotFound", f"no research period with id {period_id}")
    return jsonify({"period_id": period_id, "item_id": item_id, "done": done})


# --- trading-day calendar (the nav's month grid) -----------------------------
@bp.get("/calendar/month")
@require_auth
def calendar_month():
    """Every day of one calendar month, labelled trading / closed / future.

    Backed by the same bhavcopy cache every scan already fills — `fetch_day`
    caches an empty marker for a real non-trading day, not just a hit for a
    real one, so a month already touched by any scan renders instantly and a
    month that hasn't returns each day's true status, not "unknown", at the
    cost of one request per day the first time.
    """
    import calendar as _calendar

    from engine.bhavcopy import fetch_day

    # Query-string values, so always strings — req_int is for JSON body ints
    # and would reject these outright, hence the manual int() here.
    try:
        year = int(request.args["year"])
        month = int(request.args["month"])
    except (KeyError, ValueError):
        raise ApiError(400, "BadRequest", "year and month are required integers")
    if not (1 <= month <= 12) or not (2000 <= year <= 2100):
        raise ApiError(400, "BadRequest", "month must be 1-12, year a real year")

    cache_dir = bhavcopy_cache_dir(current_app.config.get("DATA_ROOT", "data"))
    today = date.today()
    _, days_in_month = _calendar.monthrange(year, month)

    days = []
    for day_num in range(1, days_in_month + 1):
        d = date(year, month, day_num)
        if d > today:
            status = "future"
        else:
            # A cached day is checked by file existence, not by asking
            # fetch_day to parse the whole CSV back into rows — a month
            # already scanned would otherwise re-parse ~2,000 rows per day
            # just to answer yes/no. Only a genuinely uncached date pays for
            # the real fetch (which also caches it, first time only).
            path = os.path.join(cache_dir, f"{d.isoformat()}.csv")
            if os.path.exists(path):
                status = "trading"
            elif os.path.exists(path + ".empty"):
                status = "closed"
            else:
                status = "trading" if fetch_day(cache_dir, d) else "closed"
        days.append({"date": d.isoformat(), "status": status})

    return jsonify({"year": year, "month": month, "days": days})


# --- single-symbol quote (used by the trade form's market-price prefill) ----
@bp.get("/prices/quote/<symbol>")
@require_auth
def quote(symbol: str):
    sym = symbol.strip().upper()
    source = current_app.config.get("PRICE_SOURCE")
    price = None
    if source:
        try:
            price = source.get_prices([sym]).get(sym)
        except Exception:
            price = None
    live = price is not None
    if price is not None:
        _price_store().set_price(sym, price)  # keep the shared table fresh
    else:
        price = _price_store().get_prices([sym]).get(sym)  # last known
    if price is None:
        raise ApiError(
            404, "NoQuote",
            f"no price available for '{sym}' — check the symbol, or set one manually",
        )
    return jsonify(
        {
            "symbol": sym,
            "name": symbols.name_of(sym),
            "price": price,
            "live": live,  # False -> last-known from the table, not a fresh fetch
            "market_open": market_is_open(),
        }
    )


# --- prices (manual stub until Phase 4 wires up live NSE data) --------------
@bp.get("/prices")
@require_auth
def get_prices():
    return jsonify({"prices": _price_store().get_all()})


@bp.post("/prices/refresh")
@require_auth
def refresh_prices():
    """Pull live prices right now instead of waiting for the background job.

    After refreshing we immediately re-check alerts, so a price that crossed a
    target fires without waiting for the next poll.
    """
    source = current_app.config.get("PRICE_SOURCE")
    fresh = refresh_live_prices(g.data.root, source)
    fired = _alert_store().check(_price_store().get_all())
    return jsonify(
        {
            "refreshed": fresh,
            "count": len(fresh),
            "market_open": market_is_open(),
            "triggered": [a.to_dict() for a in fired],
        }
    )


@bp.get("/prices/sources")
@require_auth
def price_sources():
    """Diagnostics: which live source is configured, and can this machine
    actually reach NSE? NSE blocks many IPs, so surfacing the real answer beats
    silently falling back and leaving you wondering why prices look delayed."""
    ok, msg = NsePythonPriceSource().healthcheck()
    return jsonify(
        {
            "mode": current_app.config.get("PRICE_SOURCE_MODE", "auto"),
            "market_open": market_is_open(),
            "nse": {"reachable": ok, "detail": msg},
        }
    )


@bp.get("/prices/<symbol>/history")
@require_auth
def price_history(symbol: str):
    """OHLC history for the price chart. ?period=1mo&interval=1d"""
    period = request.args.get("period", "1mo")
    interval = request.args.get("interval", "1d")
    # Whitelist: these strings go to an upstream API, so don't pass through
    # arbitrary user input.
    if period not in {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "ytd", "max"}:
        raise ApiError(400, "BadRequest", "unsupported period")
    if interval not in {"5m", "15m", "30m", "1h", "1d", "1wk", "1mo"}:
        raise ApiError(400, "BadRequest", "unsupported interval")

    source = current_app.config.get("PRICE_SOURCE")
    get_history = getattr(source, "get_history", None)
    if not get_history:
        raise ApiError(503, "NoHistorySource", "the configured price source cannot serve history")
    rows = get_history(symbol, period, interval)
    if not rows:
        raise ApiError(
            502,
            "HistoryUnavailable",
            f"could not fetch history for '{symbol.upper()}' — check the symbol, or the data provider may be rate-limiting",
        )
    return jsonify({"symbol": symbol.strip().upper(), "period": period, "interval": interval, "candles": rows})


@bp.put("/prices/<symbol>")
@require_auth
def set_price(symbol: str):
    body = json_body()
    price = req_num(body, "price")
    if price <= 0:
        raise ApiError(400, "BadRequest", "price must be positive")
    _price_store().set_price(symbol, price)
    # Setting a price is exactly when an alert might fire, so check immediately
    # rather than making the user wait for the next poll.
    fired = _alert_store().check(_price_store().get_all())
    return jsonify(
        {
            "symbol": symbol.strip().upper(),
            "price": round(price, 2),
            "triggered": [a.to_dict() for a in fired],
        }
    )
