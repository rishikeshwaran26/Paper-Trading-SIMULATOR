import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { Loading, ErrorBanner } from "../components/Status.jsx";
import Sparkline from "../components/Sparkline.jsx";
import { rupees, pct, when } from "../format.js";

// SCREENER PAGE — whole-market daily movers in a Tickertape-style table.
//
// A scan runs in a BACKGROUND THREAD on the server (~a minute over ~1,900
// stocks), so this page shows the last completed scan and, when you press
// "Scan now", polls progress until it finishes. Results are one flat, sortable
// table with a filter bar (price move, 52-week position, RSI, MACD) — each row
// expands to reveal the "why" (volume, 52-week context, news) and a one-click
// add-to-watchlist.

const FILTERS = {
  direction: [
    { key: "all", label: "All" },
    { key: "up", label: "Gainers" },
    { key: "down", label: "Losers" },
  ],
  move: [
    { key: "all", label: "Any move" },
    { key: "5", label: ">5%" },
    { key: "10", label: ">10%" },
    { key: "15", label: ">15%" },
  ],
  perf: [
    { key: "all", label: "52W: any" },
    { key: "high", label: "Near 52W high" },
    { key: "low", label: "Near 52W low" },
  ],
  rsi: [
    { key: "all", label: "RSI: any" },
    { key: "over", label: "Overbought >70" },
    { key: "under", label: "Oversold <30" },
  ],
  macd: [
    { key: "all", label: "MACD: any" },
    { key: "bull", label: "Bullish" },
    { key: "bear", label: "Bearish" },
  ],
};

const DEFAULT_FILTERS = { direction: "all", move: "all", perf: "all", rsi: "all", macd: "all" };

// Deterministic greyscale shade for a symbol's initial-letter avatar (we have
// no real logo source; a neutral initial keeps the monochrome look while still
// giving each row a slightly distinct chip). Lightness varies by hash so
// different symbols aren't identical, but it stays in a dark professional band.
function avatarColor(sym) {
  let h = 0;
  for (let i = 0; i < sym.length; i++) h = (h * 31 + sym.charCodeAt(i)) % 100;
  const lightness = 20 + (h % 22); // 20%–42%: dark greys, white initial stays legible
  return `hsl(0, 0%, ${lightness}%)`;
}

export default function ScreenerPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [scan, setScan] = useState(null);
  const [watchlists, setWatchlists] = useState([]);
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [sort, setSort] = useState({ key: "pct_change", dir: "desc" });
  const pollRef = useRef(null);

  async function load() {
    setLoading(true);
    try {
      const d = await api.screener();
      setData(d);
      setScan(d.status?.status === "running" ? d.status : null);
      if (d.status?.status === "running") startPolling();
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    api.listWatchlists().then((d) => setWatchlists(d.watchlists ?? [])).catch(() => {});
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function startPolling() {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const st = await api.screenerStatus();
        setScan(st);
        if (st.status === "done" || st.status === "error") {
          clearInterval(pollRef.current);
          if (st.status === "done") setData(await api.screener());
        }
      } catch {
        clearInterval(pollRef.current);
      }
    }, 2000);
  }

  async function runScan() {
    setError(null);
    try {
      const r = await api.scanScreener();
      setScan(r.status);
      startPolling();
    } catch (e) {
      setError(e);
    }
  }

  const scanning = scan?.status === "running";
  const latest = data?.latest;
  const allMovers = latest?.movers ?? [];

  const rows = useMemo(() => {
    let out = allMovers.filter((m) => {
      if (filters.direction !== "all" && m.direction !== filters.direction) return false;
      if (filters.move !== "all" && Math.abs(m.pct_change) < Number(filters.move)) return false;
      if (filters.perf === "high" && !m.near_high) return false;
      if (filters.perf === "low" && !m.near_low) return false;
      if (filters.rsi === "over" && !(m.rsi >= 70)) return false;
      if (filters.rsi === "under" && !(m.rsi <= 30 && m.rsi != null)) return false;
      if (filters.macd === "bull" && m.macd_bullish !== true) return false;
      if (filters.macd === "bear" && m.macd_bullish !== false) return false;
      return true;
    });
    const { key, dir } = sort;
    const mul = dir === "asc" ? 1 : -1;
    out = [...out].sort((a, b) => {
      const av = a[key] ?? -Infinity, bv = b[key] ?? -Infinity;
      if (key === "pct_change") return (Math.abs(bv) - Math.abs(av)) * (dir === "asc" ? -1 : 1);
      return (av - bv) * mul;
    });
    return out;
  }, [allMovers, filters, sort]);

  function toggleSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "desc" ? "asc" : "desc" } : { key, dir: "desc" }));
  }
  const sortArrow = (key) => (sort.key === key ? (sort.dir === "desc" ? " ↓" : " ↑") : "");
  const filtersActive = Object.entries(filters).some(([k, v]) => v !== DEFAULT_FILTERS[k]);

  return (
    <div className="page">
      <h1>
        Intraday stocks screener{" "}
        <button className="link" onClick={runScan} disabled={scanning} title="Run a fresh scan" style={{ fontSize: 18 }}>
          ↻
        </button>{" "}
        {data && (
          <span className="source-chip">
            <span className={`live-dot ${data.market_open ? "" : "closed"}`} />
            {data.market_open ? "market open" : "market closed"}
          </span>
        )}
      </h1>

      {/* filter bar */}
      <div className="screener-filters">
        {Object.entries(FILTERS).map(([group, opts]) => (
          <select
            key={group}
            value={filters[group]}
            onChange={(e) => setFilters((f) => ({ ...f, [group]: e.target.value }))}
            className={filters[group] !== "all" ? "filter-on" : ""}
          >
            {opts.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
          </select>
        ))}
        {filtersActive && (
          <button className="link" onClick={() => setFilters(DEFAULT_FILTERS)}>Clear all</button>
        )}
        <span className="muted" style={{ fontSize: 13, marginLeft: "auto" }}>
          {latest && <>{rows.length} of {allMovers.length} movers</>}
        </span>
      </div>

      {/* scan control + progress */}
      <div className="card" style={{ padding: "12px 16px" }}>
        <div className="row" style={{ alignItems: "center", justifyContent: "space-between" }}>
          <div className="muted" style={{ fontSize: 13 }}>
            Scans the whole NSE for stocks up or down 5–20%+ on the day.
            {latest && (
              <> Last scan: <strong>{latest.run.mover_count}</strong> movers from{" "}
                <strong>{latest.run.universe_count}</strong> stocks · {when(latest.run.finished_at)} ·{" "}
                {latest.run.source === "download" ? "live NSE list" :
                 latest.run.source === "cache" ? "cached NSE list" : "built-in list"}</>
            )}
          </div>
          <button className="primary" onClick={runScan} disabled={scanning} style={{ marginTop: 0 }}>
            {scanning ? "Scanning…" : "Scan now"}
          </button>
        </div>
        {scan && (scanning || scan.status === "error") && (
          <div style={{ marginTop: 12 }}>
            {scanning ? (
              <>
                <div className="progress-track">
                  <div className="progress-fill" style={{ width: `${scan.percent || 0}%` }} />
                </div>
                <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                  {scan.message || "Working…"} ({scan.percent || 0}%)
                </div>
              </>
            ) : (
              <div className="inline-error">Scan failed: {scan.error}</div>
            )}
          </div>
        )}
      </div>

      {loading && <Loading label="Loading screener…" />}
      <ErrorBanner error={error} onRetry={load} />

      {data && !latest && !scanning && (
        <p className="muted">No scan yet — press <strong>Scan now</strong> to find today's movers. It takes about a minute.</p>
      )}

      {latest && (
        <div className="card" style={{ padding: 0, overflowX: "auto" }}>
          <table className="table screener-table">
            <thead>
              <tr>
                <th>Company</th>
                <th>1M trend</th>
                <th className="num sortable" onClick={() => toggleSort("price")}>Market price{sortArrow("price")}</th>
                <th className="num sortable" onClick={() => toggleSort("pct_change")}>1D change{sortArrow("pct_change")}</th>
                <th className="num sortable" onClick={() => toggleSort("volume")}>1D volume{sortArrow("volume")}</th>
                <th className="num sortable" onClick={() => toggleSort("vol_diff_1w_pct")}>1W vol diff{sortArrow("vol_diff_1w_pct")}</th>
                <th className="num sortable" onClick={() => toggleSort("rsi")}>RSI{sortArrow("rsi")}</th>
                <th>MACD</th>
                <th>52W range</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => <MoverRow key={m.symbol} m={m} watchlists={watchlists} />)}
              {rows.length === 0 && (
                <tr><td colSpan={9} className="muted" style={{ textAlign: "center", padding: 20 }}>No movers match these filters.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function MoverRow({ m, watchlists }) {
  const [open, setOpen] = useState(false);
  const [added, setAdded] = useState(null);
  const changeColor = m.direction === "up" ? "var(--pos)" : "var(--neg)";

  async function addTo(listId, listName) {
    try {
      await api.addToWatchlist(Number(listId), m.symbol);
      setAdded(`Added to ${listName}`);
    } catch {
      setAdded("Could not add");
    }
  }

  return (
    <>
      <tr className="clickable" onClick={() => setOpen((o) => !o)}>
        <td>
          <div className="company-cell">
            <span className="logo-avatar" style={{ background: avatarColor(m.symbol) }}>{m.symbol[0]}</span>
            <div>
              <div><strong>{m.symbol}</strong></div>
              <div className="muted company-name">{m.name}</div>
              {m.results_recent && <span className="results-tag">Results recently</span>}
            </div>
          </div>
        </td>
        <td><Sparkline values={m.spark} /></td>
        <td className="num">{rupees(m.price)}</td>
        <td className="num" style={{ color: changeColor }}>
          <div>{m.pct_change >= 0 ? "+" : ""}{(m.price - m.prev_close).toFixed(2)}</div>
          <div style={{ fontSize: 12 }}>{pct(m.pct_change)}</div>
        </td>
        <td className="num">{m.volume?.toLocaleString("en-IN")}</td>
        <td className="num" style={{ color: m.vol_diff_1w_pct >= 0 ? "var(--pos)" : "var(--neg)" }}>
          {m.vol_diff_1w_pct == null ? "—" : `${m.vol_diff_1w_pct >= 0 ? "+" : ""}${m.vol_diff_1w_pct.toFixed(0)}%`}
        </td>
        <td className="num">{m.rsi == null ? "—" : <RsiBadge rsi={m.rsi} />}</td>
        <td>{m.macd_bullish == null ? "—" :
          <span className={`tag ${m.macd_bullish ? "buy" : "sell"}`}>{m.macd_bullish ? "Bullish" : "Bearish"}</span>}</td>
        <td><Week52Bar m={m} /></td>
      </tr>
      {open && (
        <tr className="detail-row">
          <td colSpan={9} style={{ background: "var(--panel-2)" }}>
            <div style={{ padding: "8px 4px" }}>
              {m.reasons?.length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  {m.reasons.map((r, i) => <span key={i} className="chip">{r}</span>)}
                </div>
              )}
              <div className="muted" style={{ fontSize: 13, marginBottom: 8 }}>
                Volume {m.volume?.toLocaleString("en-IN")} vs 30-day avg {m.avg_volume?.toLocaleString("en-IN")} ({m.vol_ratio}×)
                {m.week52_high != null && <> · 52W high {rupees(m.week52_high)}</>}
                {m.week52_low != null && <> · 52W low {rupees(m.week52_low)}</>}
                {m.results_date && <> · last results {m.results_date}</>}
              </div>
              {m.news?.length > 0 ? (
                <div style={{ marginBottom: 8 }}>
                  <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>Recent news</div>
                  <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                    {m.news.map((n, i) => (
                      <li key={i}>
                        {n.link ? <a className="link" href={n.link} target="_blank" rel="noopener noreferrer">{n.title}</a> : n.title}
                        {n.publisher && <span className="muted"> — {n.publisher}</span>}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : <div className="muted" style={{ fontSize: 13, marginBottom: 8 }}>No recent headlines found.</div>}
              <div className="row" style={{ alignItems: "center", gap: 8 }}>
                {watchlists.length > 0 ? (
                  <select defaultValue="" style={{ width: "auto" }}
                    onChange={(e) => { const wl = watchlists.find((w) => String(w.id) === e.target.value); if (wl) addTo(wl.id, wl.name); }}>
                    <option value="" disabled>Add to watchlist…</option>
                    {watchlists.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                  </select>
                ) : <span className="muted" style={{ fontSize: 13 }}>Create a watchlist first to save movers.</span>}
                {added && <span className="inline-ok" style={{ margin: 0 }}>{added}</span>}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function RsiBadge({ rsi }) {
  const cls = rsi >= 70 ? "neg" : rsi <= 30 ? "pos" : "";
  return <span className={cls}>{rsi.toFixed(0)}</span>;
}

// The L——●——H range bar: a marker positioned by where today's price sits in the
// 52-week span (0 = at the low, 100 = at the high).
function Week52Bar({ m }) {
  if (m.week52_pct == null) return <span className="muted">—</span>;
  const p = Math.max(0, Math.min(100, m.week52_pct));
  return (
    <div className="w52" title={`${m.week52_pct}% of 52-week range`}>
      <span className="w52-l">L</span>
      <span className="w52-track"><span className="w52-marker" style={{ left: `${p}%` }} /></span>
      <span className="w52-h">H</span>
    </div>
  );
}
