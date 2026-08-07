// The ONE place the frontend talks to the backend. Every component calls these
// functions instead of using fetch directly, so:
//   - the base URL lives in exactly one spot,
//   - the backend's error envelope { error: { type, message } } is unwrapped in
//     one place and turned into a real thrown Error, and
//   - a dead server (network failure) becomes a friendly message, not a crash.

import { getToken, clearToken } from "./auth.js";

const BASE = import.meta.env.VITE_API_URL || "http://127.0.0.1:5000";

// A typed error so UI code can show `err.message` and, if it wants, branch on
// `err.type` (e.g. treat "InsufficientFunds" specially).
export class ApiError extends Error {
  constructor(status, type, message) {
    super(message);
    this.status = status;
    this.type = type;
  }
}

async function request(path, options = {}) {
  let res;
  // Attach the bearer token to every request, in one place, so no individual
  // call site can forget it.
  const token = getToken();
  const headers = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.headers || {}),
  };
  try {
    res = await fetch(BASE + path, { ...options, headers });
  } catch {
    // fetch only rejects on network-level failure (server down, DNS, CORS block).
    throw new ApiError(0, "NetworkError", `Cannot reach the API at ${BASE}. Is the Flask server running?`);
  }

  // Parse the body if there is one (some responses could be empty).
  const text = await res.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      /* non-JSON response — leave body null */
    }
  }

  if (!res.ok) {
    const e = (body && body.error) || {};
    // A 401 means the token is missing/expired/invalid. Drop it and let the app
    // fall back to the login screen — handled here so every caller doesn't have
    // to check for it.
    if (res.status === 401) {
      clearToken();
      window.dispatchEvent(new Event("papertrading:unauthorized"));
    }
    throw new ApiError(res.status, e.type || "Error", e.message || res.statusText);
  }
  return body;
}

const enc = encodeURIComponent;

export const api = {
  // auth
  authStatus: () => request("/auth/status"),
  register: (username, password) =>
    request("/auth/register", { method: "POST", body: JSON.stringify({ username, password }) }),
  login: (username, password) =>
    request("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  me: () => request("/auth/me"),

  // alerts
  alerts: () => request("/alerts"),
  createAlert: (payload) => request("/alerts", { method: "POST", body: JSON.stringify(payload) }),
  dismissAlert: (id) => request(`/alerts/${enc(id)}/dismiss`, { method: "POST" }),
  deleteAlert: (id) => request(`/alerts/${enc(id)}`, { method: "DELETE" }),

  // snapshots / comparison
  snapshots: (strategies) =>
    request(`/snapshots${strategies?.length ? `?strategies=${enc(strategies.join(","))}` : ""}`),
  captureSnapshot: () => request("/snapshots/capture", { method: "POST" }),

  // strategies
  listStrategies: () => request("/strategies"),
  getStrategy: (name) => request(`/strategies/${enc(name)}`),
  createStrategy: (name, starting_cash) =>
    request("/strategies", { method: "POST", body: JSON.stringify({ name, starting_cash }) }),
  deleteStrategy: (name) => request(`/strategies/${enc(name)}`, { method: "DELETE" }),

  // trading
  buy: (name, payload) =>
    request(`/strategies/${enc(name)}/buy`, { method: "POST", body: JSON.stringify(payload) }),
  sell: (name, payload) =>
    request(`/strategies/${enc(name)}/sell`, { method: "POST", body: JSON.stringify(payload) }),
  short: (name, payload) =>
    request(`/strategies/${enc(name)}/short`, { method: "POST", body: JSON.stringify(payload) }),
  cover: (name, payload) =>
    request(`/strategies/${enc(name)}/cover`, { method: "POST", body: JSON.stringify(payload) }),

  // journal
  transactions: (name) => request(`/strategies/${enc(name)}/transactions`),
  review: (name, txnId, notes) =>
    request(`/strategies/${enc(name)}/transactions/${enc(txnId)}/review`, {
      method: "POST",
      body: JSON.stringify({ notes }),
    }),
  analytics: (name) => request(`/strategies/${enc(name)}/analytics`),

  // leaderboard + prices (prices are the Phase-1 manual stub)
  leaderboard: () => request("/leaderboard"),
  prices: () => request("/prices"),
  setPrice: (symbol, price) =>
    request(`/prices/${enc(symbol)}`, { method: "PUT", body: JSON.stringify({ price }) }),

  // symbols + quotes + named watchlists
  searchSymbols: (q) => request(`/symbols/search?q=${enc(q)}`),
  quote: (symbol) => request(`/prices/quote/${enc(symbol)}`),
  listWatchlists: () => request("/watchlists"),
  createWatchlist: (name) =>
    request("/watchlists", { method: "POST", body: JSON.stringify({ name }) }),
  deleteWatchlist: (id) => request(`/watchlists/${enc(id)}`, { method: "DELETE" }),
  addToWatchlist: (id, symbol) =>
    request(`/watchlists/${enc(id)}/symbols`, { method: "POST", body: JSON.stringify({ symbol }) }),
  removeFromWatchlist: (id, symbol) =>
    request(`/watchlists/${enc(id)}/symbols/${enc(symbol)}`, { method: "DELETE" }),

  // live prices (Phase 4)
  refreshPrices: () => request("/prices/refresh", { method: "POST" }),
  priceSources: () => request("/prices/sources"),
  priceHistory: (symbol, period = "1mo", interval = "1d") =>
    request(`/prices/${enc(symbol)}/history?period=${enc(period)}&interval=${enc(interval)}`),

  // screener (whole-market daily movers)
  screener: () => request("/screener"),
  scanScreener: () => request("/screener/scan", { method: "POST" }),
  screenerStatus: () => request("/screener/status"),

  // backtest (did a past spike actually revert?)
  runBacktest: (payload) => request("/backtest/run", { method: "POST", body: JSON.stringify(payload) }),
  backtestStatus: () => request("/backtest/status"),
  listBacktestRuns: () => request("/backtest/runs"),
  getBacktestRun: (id) => request(`/backtest/runs/${enc(id)}`),

  // band-touch study (Phase 1: tight band, date range, day-2-only)
  runBandStudy: (payload) => request("/band-study/run", { method: "POST", body: JSON.stringify(payload) }),
  bandStudyStatus: () => request("/band-study/status"),
  listBandStudyRuns: () => request("/band-study/runs"),
  getBandStudyRun: (id) => request(`/band-study/runs/${enc(id)}`),
  getBandStudyRunCaps: (id) => request(`/band-study/runs/${enc(id)}/caps`),
  getBandStudyRunWeek52: (id) => request(`/band-study/runs/${enc(id)}/week52`),

  // trading-day calendar (nav widget)
  calendarMonth: (year, month) => request(`/calendar/month?year=${year}&month=${month}`),
};
