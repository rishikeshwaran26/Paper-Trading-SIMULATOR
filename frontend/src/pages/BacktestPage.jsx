import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { Loading, ErrorBanner } from "../components/Status.jsx";
import Sparkline from "../components/Sparkline.jsx";
import { rupees, pct, when } from "../format.js";

// BACKTEST PAGE — does the "20% spike reverts" idea actually hold up?
//
// Two modes, both replaying PAST data (Yahoo Finance already has years of
// daily history, so there's nothing to wait for):
//
//   Single date  — one day, a wide threshold, a multi-week walk-forward
//                  (peak / first-red / round-trip). The original tool.
//   Band study   — a tight % band (e.g. 19-20%) over a whole DATE RANGE,
//                  looking only at what the very NEXT trading day did.
//                  This is "Phase 1" of the research: prove the idea on a
//                  small, cheap slice before widening the band or the window.
//
// Both run on a background thread with polled progress, since even a
// historical replay means real batched network calls across the exchange.

function todayMinusDays(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

export default function BacktestPage() {
  const [mode, setMode] = useState("band"); // "band" | "single"

  return (
    <div className="page">
      <h1>Backtest</h1>
      <p className="muted" style={{ marginTop: -12, marginBottom: 20 }}>
        Replays past market data and checks what actually happened — turning the mean-reversion
        idea into a real, measurable number instead of a belief.
      </p>

      <div className="seg" style={{ marginBottom: 4 }}>
        <button type="button" className={mode === "band" ? "on" : ""} onClick={() => setMode("band")}>
          Band study (Phase 1)
        </button>
        <button type="button" className={mode === "single" ? "on" : ""} onClick={() => setMode("single")}>
          Single date
        </button>
      </div>

      {mode === "band" ? <BandStudy /> : <SingleDateBacktest />}
    </div>
  );
}

// =============================================================================
// BAND STUDY — tight % band, date range, next-day-only. The touch is detected
// against the day's INTRADAY HIGH (or low for a down-band), not the close, so
// a stock that spikes 19.5% and fades to close +15% still counts — that's the
// whole point versus close-only detection.
// =============================================================================
function BandStudy() {
  const [startDate, setStartDate] = useState("2026-01-01");
  const [endDate, setEndDate] = useState("2026-01-31");
  const [direction, setDirection] = useState("up");
  const [bandLow, setBandLow] = useState(19);
  const [bandHigh, setBandHigh] = useState(20);
  const [source, setSource] = useState("bhavcopy");

  const [run, setRun] = useState(null);
  const [runs, setRuns] = useState([]);
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [sort, setSort] = useState({ key: "day2_pct_change", dir: "asc" }); // asc: biggest falls first
  const pollRef = useRef(null);

  async function loadHistory() {
    try {
      const d = await api.listBandStudyRuns();
      setRuns(d.runs ?? []);
      return d.runs ?? [];
    } catch {
      return [];
    }
  }

  async function loadRun(id) {
    setLoading(true);
    try {
      setRun(await api.getBandStudyRun(id));
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    (async () => {
      const list = await loadHistory();
      const latestDone = list.find((r) => r.status === "done");
      if (latestDone) await loadRun(latestDone.id);
      else setLoading(false);
    })();
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function startPolling() {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const st = await api.bandStudyStatus();
        setStatus(st);
        if (st.status === "done" || st.status === "error") {
          clearInterval(pollRef.current);
          await loadHistory();
          if (st.status === "done" && st.run_id) await loadRun(st.run_id);
        }
      } catch {
        clearInterval(pollRef.current);
      }
    }, 1500);
  }

  async function runStudy(e) {
    e.preventDefault();
    setError(null);
    try {
      const r = await api.runBandStudy({
        start_date: startDate,
        end_date: endDate,
        direction,
        band_low: Number(bandLow),
        band_high: Number(bandHigh),
        source,
      });
      setStatus(r.status);
      startPolling();
    } catch (e) {
      setError(e);
    }
  }

  const running = status?.status === "running";

  const touches = run?.touches ?? [];
  const sortedTouches = useMemo(() => {
    const { key, dir } = sort;
    const mul = dir === "asc" ? 1 : -1;
    return [...touches].sort((a, b) => {
      const av = a[key], bv = b[key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;  // no-day2 rows sink to the bottom regardless of direction
      if (bv == null) return -1;
      return (av - bv) * mul;
    });
  }, [touches, sort]);

  function toggleSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  }
  const arrow = (key) => (sort.key === key ? (sort.dir === "asc" ? " ↑" : " ↓") : "");

  return (
    <>
      <form className="card" onSubmit={runStudy}>
        <div className="card-title">Run a band study</div>
        <div className="row">
          <label>Start date
            <input type="date" value={startDate} max={todayMinusDays(1)} onChange={(e) => setStartDate(e.target.value)} />
          </label>
          <label>End date
            <input type="date" value={endDate} max={todayMinusDays(1)} onChange={(e) => setEndDate(e.target.value)} />
          </label>
          <label>Direction
            <select value={direction} onChange={(e) => setDirection(e.target.value)}>
              <option value="up">Spikes up (short thesis)</option>
              <option value="down">Drops down (bounce thesis)</option>
            </select>
          </label>
          <label>Band low (%)
            <input type="number" min="1" max="99" value={bandLow} onChange={(e) => setBandLow(e.target.value)} />
          </label>
          <label>Band high (%)
            <input type="number" min="1" max="100" value={bandHigh} onChange={(e) => setBandHigh(e.target.value)} />
          </label>
          <label>Data source
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="bhavcopy">NSE bhavcopy (fast, whole exchange)</option>
              <option value="yahoo">Yahoo Finance (slower, rate-limit risk)</option>
            </select>
          </label>
        </div>
        {source === "bhavcopy" && (
          <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>
            One request per trading day, straight from NSE's own daily file — immune to Yahoo's per-symbol
            rate limiting. Touches are still cross-checked against Yahoo's split/bonus-adjusted prices, so a
            mechanical price cut (not a real move) gets flagged, not miscounted.
          </p>
        )}
        <button className="primary" disabled={running}>
          {running ? "Running…" : "Run band study"}
        </button>

        {status && (running || status.status === "error") && (
          <div style={{ marginTop: 12 }}>
            {running ? (
              <>
                <div className="progress-track">
                  <div className="progress-fill" style={{ width: `${status.percent || 0}%` }} />
                </div>
                <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                  {status.message || "Working…"} ({status.percent || 0}%)
                </div>
              </>
            ) : (
              <div className="inline-error">Band study failed: {status.error}</div>
            )}
          </div>
        )}
      </form>

      {runs.length > 0 && (
        <div className="card" style={{ padding: "12px 16px" }}>
          <div className="card-title" style={{ marginBottom: 8 }}>Past runs</div>
          <div className="row" style={{ gap: 8 }}>
            {runs.map((r) => (
              <button
                key={r.id}
                className={`small ${run?.run?.id === r.id ? "primary" : ""}`}
                onClick={() => loadRun(r.id)}
                title={r.status}
              >
                {r.start_date} → {r.end_date} ({r.band_low}–{r.band_high}%, {r.source})
                {r.status === "done" ? ` · ${r.total_touches} touches` : ` (${r.status})`}
              </button>
            ))}
          </div>
        </div>
      )}

      {loading && <Loading label="Loading band study…" />}
      <ErrorBanner error={error} onRetry={() => run?.run?.id && loadRun(run.run.id)} />

      {run && run.run.status === "error" && (
        <div className="card"><div className="inline-error">This run failed: {run.run.error}</div></div>
      )}

      {run && run.run.status === "done" && touches.length === 0 && (
        <div className="card">
          <p className="muted" style={{ margin: 0 }}>
            No stock touched {run.run.band_low}–{run.run.band_high}% in this range — try a wider band or a longer range.
          </p>
        </div>
      )}

      {run && run.run.status === "done" && touches.length > 0 && (
        <BandStudyResult run={run.run} touches={sortedTouches} onSort={toggleSort} sortArrow={arrow} />
      )}
    </>
  );
}

function BandStudyResult({ run, touches, onSort, sortArrow }) {
  const flaggedCount = touches.filter((t) => t.corporate_action_suspected).length;
  return (
    <>
      {run.missing_count > 0 && (
        <div className="inline-error" style={{ marginBottom: 12 }}>
          <strong>{run.missing_count} symbol{run.missing_count === 1 ? "" : "s"} could not be fetched</strong> even
          after a retry, so this run's coverage is incomplete: {run.missing_symbols.join(", ")}.
        </div>
      )}

      {run.source === "bhavcopy" && flaggedCount > 0 && (
        <div className="alert-banner" style={{ marginBottom: 12 }}>
          <span className="bell">⚠</span>
          <span>
            <strong>{flaggedCount} touch{flaggedCount === 1 ? "" : "es"} flagged as a possible corporate action</strong> —
            NSE's raw price disagreed sharply with Yahoo's split/bonus-adjusted price for that date, which usually
            means a mechanical price cut (split/bonus), not a real market move. Marked with ⚠ below; still shown,
            not hidden, so you can judge for yourself.
          </span>
        </div>
      )}

      {/* Step 1: total count, up front */}
      <div className="summary-row">
        <Stat label="Total touches" value={run.total_touches} />
        <Stat label="With a day 2" value={run.with_day2} />
        <Stat label="Up on day 2" value={`${run.up_pct ?? 0}%`} cls="pos" />
        <Stat label="Down on day 2" value={`${run.down_pct ?? 0}%`} cls="neg" />
      </div>

      {/* Step 4: the report — up and down kept separate, never blended into one average */}
      <div className="card">
        <div className="card-title">Day-2 outcome, up vs down (kept separate on purpose)</div>
        <table className="table">
          <thead>
            <tr><th></th><th className="num">Count</th><th className="num">% of touches</th><th className="num">Avg day-2 move</th></tr>
          </thead>
          <tbody>
            <tr>
              <td><span className="tag buy">MOVED UP</span></td>
              <td className="num">{run.up_count}</td>
              <td className="num">{run.up_pct ?? 0}%</td>
              <td className="num pos">{run.avg_day2_pct_up != null ? pct(run.avg_day2_pct_up) : "—"}</td>
            </tr>
            <tr>
              <td><span className="tag sell">MOVED DOWN</span></td>
              <td className="num">{run.down_count}</td>
              <td className="num">{run.down_pct ?? 0}%</td>
              <td className="num neg">{run.avg_day2_pct_down != null ? pct(run.avg_day2_pct_down) : "—"}</td>
            </tr>
          </tbody>
        </table>
      </div>

      {/* Steps 2 & 3: every touch, shown separately, sortable by day-2 change */}
      <section className="card">
        <div className="card-title">
          {run.start_date} → {run.end_date} — every {run.direction === "up" ? "spike" : "drop"} that touched {run.band_low}–{run.band_high}%
        </div>
        <table className="table screener-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th className="num">Touch date</th>
              <th className="num">Touch %</th>
              <th className="num">Close %</th>
              <th className="num sortable" onClick={() => onSort("day2_pct_change")}>
                Day-2 change{sortArrow("day2_pct_change")}
              </th>
              <th className="num">Day-2 open→close</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {touches.map((t) => (
              <tr key={`${t.symbol}-${t.date}`} style={t.corporate_action_suspected ? { opacity: 0.55 } : undefined}>
                <td>
                  <strong>{t.symbol}</strong>
                  {t.corporate_action_suspected && (
                    <span className="tag warn" title="Bhavcopy vs Yahoo-adjusted price disagree sharply — likely a split/bonus, not a real move">
                      ⚠ possible split/bonus
                    </span>
                  )}
                  <div className="muted" style={{ fontSize: 12 }}>{t.name}</div>
                </td>
                <td className="num">{t.date}</td>
                <td className={`num ${t.direction === "up" ? "pos" : "neg"}`}>{pct(t.touch_pct)}</td>
                <td className="num">{pct(t.close_pct)}</td>
                <td className={`num ${t.day2_pct_change != null ? (t.day2_pct_change >= 0 ? "pos" : "neg") : ""}`}>
                  {t.day2_pct_change != null ? pct(t.day2_pct_change) : "—"}
                </td>
                <td className={`num ${t.day2_open_close_pct != null ? (t.day2_open_close_pct >= 0 ? "pos" : "neg") : ""}`}>
                  {t.day2_open_close_pct != null ? pct(t.day2_open_close_pct) : "—"}
                </td>
                <td>
                  {t.day2_pct_change == null ? (
                    <span className="chip muted">no day 2 yet</span>
                  ) : (
                    <span className={`tag ${t.day2_pct_change < 0 ? "sell" : "buy"}`}>
                      {t.day2_pct_change < 0 ? "fell" : "rose"}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}

// =============================================================================
// SINGLE DATE — one day, wide threshold, multi-week walk-forward (peak /
// first-red / round-trip). The original tool, kept as its own mode.
// =============================================================================
function SingleDateBacktest() {
  const [targetDate, setTargetDate] = useState(todayMinusDays(90));
  const [direction, setDirection] = useState("up");
  const [thresholdPct, setThresholdPct] = useState(20);
  const [windowDays, setWindowDays] = useState(30);

  const [run, setRun] = useState(null);
  const [runs, setRuns] = useState([]);
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const pollRef = useRef(null);

  async function loadHistory() {
    try {
      const d = await api.listBacktestRuns();
      setRuns(d.runs ?? []);
      return d.runs ?? [];
    } catch {
      return [];
    }
  }

  async function loadRun(id) {
    setLoading(true);
    try {
      setRun(await api.getBacktestRun(id));
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    (async () => {
      const list = await loadHistory();
      const latestDone = list.find((r) => r.status === "done");
      if (latestDone) await loadRun(latestDone.id);
      else setLoading(false);
    })();
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function startPolling() {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const st = await api.backtestStatus();
        setStatus(st);
        if (st.status === "done" || st.status === "error") {
          clearInterval(pollRef.current);
          await loadHistory();
          if (st.status === "done" && st.run_id) await loadRun(st.run_id);
        }
      } catch {
        clearInterval(pollRef.current);
      }
    }, 1500);
  }

  async function runBacktest(e) {
    e.preventDefault();
    setError(null);
    try {
      const r = await api.runBacktest({
        target_date: targetDate,
        direction,
        threshold_pct: Number(thresholdPct),
        window_days: Number(windowDays),
      });
      setStatus(r.status);
      startPolling();
    } catch (e) {
      setError(e);
    }
  }

  const running = status?.status === "running";

  return (
    <>
      <form className="card" onSubmit={runBacktest}>
        <div className="card-title">Run a backtest</div>
        <div className="row">
          <label>Date to replay
            <input type="date" value={targetDate} max={todayMinusDays(1)}
              onChange={(e) => setTargetDate(e.target.value)} />
          </label>
          <label>Direction
            <select value={direction} onChange={(e) => setDirection(e.target.value)}>
              <option value="up">Spikes up (short thesis)</option>
              <option value="down">Drops down (bounce thesis)</option>
            </select>
          </label>
          <label>Move threshold (%)
            <input type="number" min="1" max="100" value={thresholdPct}
              onChange={(e) => setThresholdPct(e.target.value)} />
          </label>
          <label>Tracking window (days)
            <input type="number" min="1" max="90" value={windowDays}
              onChange={(e) => setWindowDays(e.target.value)} />
          </label>
        </div>
        <button className="primary" disabled={running}>
          {running ? "Running…" : "Run backtest"}
        </button>

        {status && (running || status.status === "error") && (
          <div style={{ marginTop: 12 }}>
            {running ? (
              <>
                <div className="progress-track">
                  <div className="progress-fill" style={{ width: `${status.percent || 0}%` }} />
                </div>
                <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                  {status.message || "Working…"} ({status.percent || 0}%)
                </div>
              </>
            ) : (
              <div className="inline-error">Backtest failed: {status.error}</div>
            )}
          </div>
        )}
      </form>

      {runs.length > 0 && (
        <div className="card" style={{ padding: "12px 16px" }}>
          <div className="card-title" style={{ marginBottom: 8 }}>Past runs</div>
          <div className="row" style={{ gap: 8 }}>
            {runs.map((r) => (
              <button
                key={r.id}
                className={`small ${run?.run?.id === r.id ? "primary" : ""}`}
                onClick={() => loadRun(r.id)}
                title={r.status}
              >
                {r.target_date} {r.status === "done" ? `· ${r.reverted_pct ?? 0}%` : `(${r.status})`}
              </button>
            ))}
          </div>
        </div>
      )}

      {loading && <Loading label="Loading backtest…" />}
      <ErrorBanner error={error} onRetry={() => run?.run?.id && loadRun(run.run.id)} />

      {run && <BacktestResult detail={run} />}
    </>
  );
}

function BacktestResult({ detail }) {
  const { run, movers } = detail;
  if (run.status === "error") {
    return <div className="card"><div className="inline-error">This run failed: {run.error}</div></div>;
  }
  if (!movers || movers.length === 0) {
    return (
      <div className="card">
        <p className="muted" style={{ margin: 0 }}>
          No stocks moved {run.threshold_pct}%+ on {run.target_date} — try a different date or a lower threshold.
        </p>
      </div>
    );
  }

  const revertedPct = run.reverted_pct ?? 0;
  const avgDays = movers.filter((m) => m.reverted).map((m) => m.round_trip_offset_days);
  const avgDaysToRevert = avgDays.length ? (avgDays.reduce((a, b) => a + b, 0) / avgDays.length).toFixed(1) : "—";

  return (
    <>
      {run.missing_count > 0 && (
        <div className="inline-error" style={{ marginBottom: 12 }}>
          <strong>{run.missing_count} symbol{run.missing_count === 1 ? "" : "s"} could not be fetched</strong> even
          after a retry, so this run's coverage is incomplete: {run.missing_symbols.join(", ")}.
        </div>
      )}

      <div className="summary-row">
        <Stat label="Movers found" value={run.mover_count} />
        <Stat label="Reverted" value={`${revertedPct}%`} cls={revertedPct >= 50 ? "pos" : "neg"} />
        <Stat label="Avg days to revert" value={avgDaysToRevert} />
        <Stat label="Window" value={`${run.window_days}d`} />
      </div>

      <section className="card">
        <div className="card-title">
          {run.target_date} — {run.direction === "up" ? "gainers" : "losers"} of {run.threshold_pct}%+
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Symbol</th><th>Path</th><th className="num">Spike</th>
              <th className="num">Peak</th><th className="num">First red</th>
              <th className="num">Round trip</th><th className="num">RSI</th>
              <th className="num">Vol×</th><th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {movers.map((m) => (
              <tr key={m.symbol}>
                <td>
                  <strong>{m.symbol}</strong>
                  <div className="muted" style={{ fontSize: 12 }}>{m.name}</div>
                </td>
                <td><Sparkline values={m.spark} /></td>
                <td className={`num ${m.direction === "up" ? "pos" : "neg"}`}>{pct(m.spike_pct)}</td>
                <td className="num">
                  {rupees(m.peak_price)}
                  {m.peak_offset_days > 0 && <span className="muted"> (+{m.peak_offset_days}d)</span>}
                </td>
                <td className="num">{m.first_red_offset_days != null ? `${m.first_red_offset_days}d` : "—"}</td>
                <td className="num">{m.round_trip_offset_days != null ? `${m.round_trip_offset_days}d` : "—"}</td>
                <td className="num">{m.rsi != null ? m.rsi : "—"}</td>
                <td className="num">{m.vol_ratio ? `${m.vol_ratio}×` : "—"}</td>
                <td>
                  <span className={`tag ${m.reverted ? "buy" : "sell"}`}>
                    {m.reverted ? "reverted" : "held / kept going"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}

function Stat({ label, value, cls = "" }) {
  return (
    <div className={`stat ${cls}`}>
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${cls}`}>{value}</div>
    </div>
  );
}
