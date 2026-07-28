import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { Loading, ErrorBanner } from "../components/Status.jsx";
import { pct } from "../format.js";

// BACKTEST PAGE — does the "20% spike reverts" idea actually hold up?
//
// Both modes replay PAST data through the same band-touch engine (a tight %
// band, e.g. 19-20%, checked against the day's INTRADAY HIGH/LOW so a fade
// back before close still counts) and can track trading days both BEFORE and
// AFTER each touch:
//
//   Single date  — one specific day, one specific spike. Defaults to a wider
//                  before/after window (5 before, 10 after) since the whole
//                  point is looking closely at one event.
//   Band study   — a whole DATE RANGE, so many touches at once. Defaults to
//                  after-only (before_days=0) since the headline question is
//                  "what happens next", but the same before/after tracking
//                  and per-touch graph are available here too.
//
// Both run on a background thread with polled progress, since even a
// historical replay means real batched network calls across the exchange.

function todayMinusDays(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

// Resolves any sortable column's value for one touch. `offset:<n>` keys cover
// the per-day columns shown in the "all days" table view; the rest cover the
// fixed columns shown in the single-day view.
function sortValue(t, key, viewDay, findDay) {
  if (key === "symbol") return t.symbol;
  if (key === "date") return t.date;
  if (key === "touch_pct") return t.touch_pct;
  if (key === "close_pct") return t.close_pct;
  if (key === "_day_date") return findDay(t, viewDay)?.date ?? null;
  if (key === "_pct") return findDay(t, viewDay)?.pct_from_touch ?? null;
  if (key === "_open_close") return findDay(t, viewDay)?.open_close_pct ?? null;
  if (key.startsWith("offset:")) return findDay(t, Number(key.slice(7)))?.pct_from_touch ?? null;
  return null;
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

      {/* key={mode} remounts fresh on toggle — the two modes have different
          defaults (before/after window sizes) and shouldn't share state. */}
      <BandStudy key={mode} singleDate={mode === "single"} />
    </div>
  );
}

// =============================================================================
// BAND STUDY — tight % band, checked against the day's INTRADAY HIGH (or LOW
// for a down-band), not the close — a stock that spikes 19.5% and fades to
// close +15% still counts, which is the whole point versus close-only
// detection. Tracks `before_days` trading days leading up to each touch and
// `follow_days` after it; either can be viewed alone or all at once, and any
// touch can be graphed as a before/after bar chart, all without re-scanning.
// =============================================================================
function BandStudy({ singleDate }) {
  const [startDate, setStartDate] = useState(singleDate ? todayMinusDays(30) : "2026-01-01");
  const [endDate, setEndDate] = useState("2026-01-31");
  const [direction, setDirection] = useState("up");
  const [bandLow, setBandLow] = useState(19);
  const [bandHigh, setBandHigh] = useState(20);
  const [source, setSource] = useState("bhavcopy");
  const [followDays, setFollowDays] = useState(singleDate ? 10 : 5);
  const [beforeDays, setBeforeDays] = useState(singleDate ? 5 : 0);
  // Which day-offset the results table is showing. 0 means "all tracked days".
  const [viewDay, setViewDay] = useState(1);
  const [selectedTouch, setSelectedTouch] = useState(null);
  // "all" | "up" | "down" — filters the touches table to only rows that rose
  // (or fell) on the day being viewed (the touch day's own move when "All
  // days" is selected, since there's no single day to judge by there).
  const [outcomeFilter, setOutcomeFilter] = useState("all");

  const [run, setRun] = useState(null);
  const [runs, setRuns] = useState([]);
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // key: null means "normal" (the original, unsorted order the backend returned).
  const [sort, setSort] = useState({ key: null, dir: null });
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
      const d = await api.getBandStudyRun(id);
      setRun(d);
      setViewDay(1); // a freshly loaded run always starts on day 1
      setSelectedTouch(null);
      setSort({ key: null, dir: null }); // and on the table's normal (unsorted) order
      setOutcomeFilter("all");
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    (async () => {
      const list = await loadHistory();
      // Single date and band study share one run history — only resume a run
      // that matches this mode (single-day range vs multi-day range).
      const latestDone = list.find(
        (r) => r.status === "done" && (singleDate ? r.start_date === r.end_date : r.start_date !== r.end_date)
      );
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
        end_date: singleDate ? startDate : endDate,
        direction,
        band_low: Number(bandLow),
        band_high: Number(bandHigh),
        source,
        follow_days: Number(followDays),
        before_days: Number(beforeDays),
      });
      setStatus(r.status);
      startPolling();
    } catch (e) {
      setError(e);
    }
  }

  const running = status?.status === "running";

  const touches = run?.touches ?? [];
  const runFollowDays = run?.run?.follow_days ?? 1;
  const runBeforeDays = run?.run?.before_days ?? 0;

  // A day entry can live in either follow_days (offset > 0) or before_days
  // (offset < 0) — this is the one place that knows which list to search.
  function findDay(touch, offset) {
    const list = offset >= 0 ? touch.follow_days : touch.before_days;
    return (list ?? []).find((d) => d.offset === offset) ?? null;
  }

  // Attach the currently-viewed day's numbers to each touch, so sorting and
  // rendering both work off one shape regardless of which day is selected.
  // viewDay === 0 means "all days" — no single day is flattened in.
  const rows = useMemo(() => {
    return touches.map((t) => {
      const d = viewDay !== 0 ? findDay(t, viewDay) : null;
      // The value "up only"/"down only" judges by: the viewed day's move, or
      // the touch day's own move when "All days" is selected (no single day
      // to judge by there).
      const outcomeVal = viewDay !== 0 ? (d ? d.pct_from_touch : null) : t.close_pct;
      return { ...t, _day: d, _pct: d ? d.pct_from_touch : null, _outcome: outcomeVal };
    });
  }, [touches, viewDay]);

  const sortedTouches = useMemo(() => {
    const { key, dir } = sort;
    if (!key || !dir) return rows; // "normal": whatever order the backend returned
    const mul = dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = sortValue(a, key, viewDay, findDay);
      const bv = sortValue(b, key, viewDay, findDay);
      if (av == null && bv == null) return 0;
      if (av == null) return 1;  // rows with no data this far out (or this column) sink to the bottom
      if (bv == null) return -1;
      if (typeof av === "string" && typeof bv === "string") return av.localeCompare(bv) * mul;
      return (av - bv) * mul;
    });
  }, [rows, sort, viewDay]);

  const visibleTouches = useMemo(() => {
    if (outcomeFilter === "all") return sortedTouches;
    return sortedTouches.filter((t) =>
      outcomeFilter === "up" ? t._outcome > 0 : t._outcome != null && t._outcome < 0
    );
  }, [sortedTouches, outcomeFilter]);

  const upCount = rows.filter((t) => t._outcome > 0).length;
  const downCount = rows.filter((t) => t._outcome != null && t._outcome < 0).length;

  // Click cycle per heading: normal -> ascending -> descending -> normal again.
  function toggleSort(key) {
    setSort((s) => {
      if (s.key !== key) return { key, dir: "asc" };
      if (s.dir === "asc") return { key, dir: "desc" };
      return { key: null, dir: null };
    });
  }
  const arrow = (key) => (sort.key === key ? (sort.dir === "asc" ? " ↑" : " ↓") : "");

  return (
    <>
      <form className="card" onSubmit={runStudy}>
        <div className="card-title">{singleDate ? "Check a single date" : "Run a band study"}</div>
        <div className="row">
          {singleDate ? (
            <label>Date to check
              <input type="date" value={startDate} max={todayMinusDays(1)} onChange={(e) => setStartDate(e.target.value)} />
            </label>
          ) : (
            <>
              <label>Start date
                <input type="date" value={startDate} max={todayMinusDays(1)} onChange={(e) => setStartDate(e.target.value)} />
              </label>
              <label>End date
                <input type="date" value={endDate} max={todayMinusDays(1)} onChange={(e) => setEndDate(e.target.value)} />
              </label>
            </>
          )}
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
          <label>Before days
            <input type="number" min="0" max="30" value={beforeDays}
              onChange={(e) => setBeforeDays(e.target.value)} />
          </label>
          <label>Follow-up days
            <input type="number" min="1" max="30" value={followDays}
              onChange={(e) => setFollowDays(e.target.value)} />
          </label>
          <label>Data source
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="bhavcopy">NSE bhavcopy (fast, whole exchange)</option>
              <option value="yahoo">Yahoo Finance (slower, rate-limit risk)</option>
            </select>
          </label>
        </div>
        <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>
          Both windows are <strong>trading</strong> days, so weekends and holidays never consume one —
          before days look back from each touch, follow-up days run past the end date when they need to
          (a touch on 31 Jan is still followed into February). After the scan you can view any single day,
          or all of them, and click a stock to see its before/after move as a bar chart.
        </p>
        {source === "bhavcopy" && (
          <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>
            One request per trading day, straight from NSE's own daily file — immune to Yahoo's per-symbol
            rate limiting. Touches are still cross-checked against Yahoo's split/bonus-adjusted prices, so a
            mechanical price cut (not a real move) gets flagged, not miscounted.
          </p>
        )}
        <button className="primary" disabled={running}>
          {running ? "Running…" : singleDate ? "Check this date" : "Run band study"}
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
              <div className="inline-error">{singleDate ? "Check" : "Band study"} failed: {status.error}</div>
            )}
          </div>
        )}
      </form>

      {runs.length > 0 && (
        <div className="card" style={{ padding: "12px 16px" }}>
          <div className="card-title" style={{ marginBottom: 8 }}>Past runs</div>
          <div className="row" style={{ gap: 8 }}>
            {runs
              .filter((r) => (singleDate ? r.start_date === r.end_date : r.start_date !== r.end_date))
              .map((r) => (
                <button
                  key={r.id}
                  className={`small ${run?.run?.id === r.id ? "primary" : ""}`}
                  onClick={() => loadRun(r.id)}
                  title={r.status}
                >
                  {r.start_date === r.end_date ? r.start_date : `${r.start_date} → ${r.end_date}`}
                  {" "}({r.band_low}–{r.band_high}%, {r.source})
                  {r.status === "done" ? ` · ${r.total_touches} touches` : ` (${r.status})`}
                </button>
              ))}
          </div>
        </div>
      )}

      {loading && <Loading label="Loading…" />}
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
        <BandStudyResult
          run={run.run}
          touches={visibleTouches}
          totalCount={touches.length}
          onSort={toggleSort}
          sortArrow={arrow}
          viewDay={viewDay}
          onViewDay={setViewDay}
          followDays={runFollowDays}
          beforeDays={runBeforeDays}
          selectedTouch={selectedTouch}
          onSelectTouch={setSelectedTouch}
          findDay={findDay}
          outcomeFilter={outcomeFilter}
          onOutcomeFilter={setOutcomeFilter}
          upCount={upCount}
          downCount={downCount}
        />
      )}
    </>
  );
}

function BandStudyResult({
  run, touches, totalCount, onSort, sortArrow, viewDay, onViewDay, followDays, beforeDays,
  selectedTouch, onSelectTouch, findDay, outcomeFilter, onOutcomeFilter, upCount, downCount,
}) {
  const flaggedCount = touches.filter((t) => t.corporate_action_suspected).length;
  const perDay = run.per_day ?? [];
  const perBeforeDay = run.per_before_day ?? [];
  const dayOffsets = Array.from({ length: followDays }, (_, i) => i + 1);
  const beforeOffsets = Array.from({ length: beforeDays }, (_, i) => i - beforeDays); // -N ... -1
  const allOffsets = [...beforeOffsets, ...dayOffsets];
  const allPerDay = [...perBeforeDay, ...perDay];
  const selected = allPerDay.find((d) => d.offset === viewDay) ?? null;
  const showingAll = viewDay === 0;
  const dayLabel = (n) => (n > 0 ? `Day +${n}` : `Day ${n}`);

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

      {/* Day switcher — every tracked day is already in the data, so this
          changes the view instantly without re-scanning. */}
      <div className="card" style={{ padding: "12px 16px" }}>
        <div className="card-title" style={{ marginBottom: 8 }}>
          Showing {showingAll ? `all ${allOffsets.length} tracked days` : `${dayLabel(viewDay)} relative to the touch`}
        </div>
        <div className="seg" style={{ marginBottom: 0, flexWrap: "wrap" }}>
          {allOffsets.map((n) => (
            <button key={n} type="button" className={viewDay === n ? "on" : ""} onClick={() => onViewDay(n)}>
              {dayLabel(n)}
            </button>
          ))}
          <button type="button" className={showingAll ? "on" : ""} onClick={() => onViewDay(0)}>
            All {allOffsets.length}
          </button>
        </div>
      </div>

      {/* Headline counts, for whichever day is selected */}
      <div className="summary-row">
        <Stat label="Total touches" value={run.total_touches} />
        <Stat
          label={showingAll ? "Scored on day 1" : `Scored on ${dayLabel(viewDay)}`}
          value={showingAll ? (perDay[0]?.scored ?? 0) : (selected?.scored ?? 0)}
        />
        <Stat
          label={showingAll ? "Up by day 1" : `Up by ${dayLabel(viewDay)}`}
          value={`${(showingAll ? perDay[0]?.up_pct : selected?.up_pct) ?? 0}%`}
          cls="pos"
        />
        <Stat
          label={showingAll ? "Down by day 1" : `Down by ${dayLabel(viewDay)}`}
          value={`${(showingAll ? perDay[0]?.down_pct : selected?.down_pct) ?? 0}%`}
          cls="neg"
        />
      </div>

      {/* The per-touch graph — click any stock below to see it here. */}
      {selectedTouch && (
        <TouchBarChart touch={selectedTouch} beforeOffsets={beforeOffsets} dayOffsets={dayOffsets} findDay={findDay} onClose={() => onSelectTouch(null)} />
      )}

      {/* The report: every tracked day, up and down kept separate. Cumulative
          from the touch day's close, so "day +3" answers "where is it now, 3
          days on?" and "day -3" answers "where was it, 3 days before?" */}
      <div className="card">
        <div className="card-title">
          Cumulative move by each tracked day (up vs down kept separate)
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Day</th>
              <th className="num">Scored</th>
              <th className="num">Up</th>
              <th className="num">Avg up move</th>
              <th className="num">Down</th>
              <th className="num">Avg down move</th>
            </tr>
          </thead>
          <tbody>
            {allPerDay.map((d) => (
              <tr
                key={d.offset}
                className="clickable"
                onClick={() => onViewDay(d.offset)}
                style={!showingAll && d.offset === viewDay ? { background: "var(--accent-soft)" } : undefined}
              >
                <td><strong>{dayLabel(d.offset)}</strong></td>
                <td className="num">{d.scored}</td>
                <td className="num">{d.up_count} <span className="muted">({d.up_pct}%)</span></td>
                <td className="num pos">{d.avg_pct_up != null ? pct(d.avg_pct_up) : "—"}</td>
                <td className="num">{d.down_count} <span className="muted">({d.down_pct}%)</span></td>
                <td className="num neg">{d.avg_pct_down != null ? pct(d.avg_pct_down) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Every touch, shown separately */}
      <section className="card">
        <div className="card-title">
          {run.start_date === run.end_date ? run.start_date : `${run.start_date} → ${run.end_date}`} — every{" "}
          {run.direction === "up" ? "spike" : "drop"} that touched {run.band_low}–{run.band_high}%
        </div>
        <div className="seg" style={{ marginBottom: 12 }}>
          <button type="button" className={outcomeFilter === "all" ? "on" : ""} onClick={() => onOutcomeFilter("all")}>
            All {totalCount}
          </button>
          <button type="button" className={outcomeFilter === "up" ? "on" : ""} onClick={() => onOutcomeFilter("up")}>
            Up only ({upCount})
          </button>
          <button type="button" className={outcomeFilter === "down" ? "on" : ""} onClick={() => onOutcomeFilter("down")}>
            Down only ({downCount})
          </button>
        </div>
        {touches.length === 0 && (
          <p className="muted" style={{ margin: "0 0 12px" }}>
            No touches {outcomeFilter === "up" ? "rose" : "fell"} on {showingAll ? "the touch day" : dayLabel(viewDay)} — try the other filter.
          </p>
        )}
        <table className="table screener-table">
          <thead>
            <tr>
              <th className="sortable" onClick={() => onSort("symbol")}>Symbol{sortArrow("symbol")}</th>
              <th className="num sortable" onClick={() => onSort("date")}>Touch date{sortArrow("date")}</th>
              <th className="num sortable" onClick={() => onSort("touch_pct")}>Touch %{sortArrow("touch_pct")}</th>
              <th className="num sortable" onClick={() => onSort("close_pct")}>Close %{sortArrow("close_pct")}</th>
              {showingAll ? (
                allOffsets.map((n) => (
                  <th key={n} className="num sortable" onClick={() => onSort(`offset:${n}`)}>
                    D{n}{sortArrow(`offset:${n}`)}
                  </th>
                ))
              ) : (
                <>
                  <th className="num sortable" onClick={() => onSort("_day_date")}>Date{sortArrow("_day_date")}</th>
                  <th className="num sortable" onClick={() => onSort("_pct")}>
                    {dayLabel(viewDay)} change{sortArrow("_pct")}
                  </th>
                  <th className="num sortable" onClick={() => onSort("_open_close")}>
                    Open→close{sortArrow("_open_close")}
                  </th>
                  <th></th>
                </>
              )}
              <th>Graph</th>
            </tr>
          </thead>
          <tbody>
            {touches.map((t) => {
              const isSelected = selectedTouch && selectedTouch.symbol === t.symbol && selectedTouch.date === t.date;
              return (
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

                  {showingAll ? (
                    allOffsets.map((n) => {
                      const d = findDay(t, n);
                      return (
                        <td
                          key={n}
                          className={`num ${d ? (d.pct_from_touch >= 0 ? "pos" : "neg") : ""}`}
                          title={d ? `${d.date} · close ${d.close}` : "no data this far out"}
                        >
                          {d ? pct(d.pct_from_touch) : "—"}
                        </td>
                      );
                    })
                  ) : (
                    <>
                      <td className="num muted">{t._day?.date ?? "—"}</td>
                      <td className={`num ${t._pct != null ? (t._pct >= 0 ? "pos" : "neg") : ""}`}>
                        {t._pct != null ? pct(t._pct) : "—"}
                      </td>
                      <td className={`num ${t._day?.open_close_pct != null ? (t._day.open_close_pct >= 0 ? "pos" : "neg") : ""}`}>
                        {t._day?.open_close_pct != null ? pct(t._day.open_close_pct) : "—"}
                      </td>
                      <td>
                        {t._pct == null ? (
                          <span className="chip muted">no data</span>
                        ) : (
                          <span className={`tag ${t._pct < 0 ? "sell" : "buy"}`}>
                            {t._pct < 0 ? "fell" : "rose"}
                          </span>
                        )}
                      </td>
                    </>
                  )}
                  <td>
                    <button
                      type="button"
                      className={`small ${isSelected ? "primary" : ""}`}
                      onClick={() => onSelectTouch(isSelected ? null : t)}
                    >
                      {isSelected ? "Hide" : "Chart"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </>
  );
}

// A neat before/touch/after bar chart for one stock's move around its spike —
// self-contained inline SVG so it stays theme-aware with zero extra deps.
function TouchBarChart({ touch, beforeOffsets, dayOffsets, findDay, onClose }) {
  const points = useMemo(() => {
    const before = beforeOffsets.map((n) => {
      const d = findDay(touch, n);
      return { offset: n, label: `D${n}`, date: d?.date ?? null, pct: d ? d.pct_from_touch : null };
    });
    const touchPoint = { offset: 0, label: "Touch", date: touch.date, pct: touch.close_pct, isTouch: true };
    const after = dayOffsets.map((n) => {
      const d = findDay(touch, n);
      return { offset: n, label: `D+${n}`, date: d?.date ?? null, pct: d ? d.pct_from_touch : null };
    });
    return [...before, touchPoint, ...after];
  }, [touch, beforeOffsets, dayOffsets, findDay]);

  const values = points.map((p) => p.pct).filter((v) => v != null);
  const maxAbs = Math.max(1, ...values.map((v) => Math.abs(v)));
  const barW = 30, gap = 8, chartH = 170, padTop = 20, padBottom = 46;
  const width = points.length * (barW + gap) + gap;
  const height = chartH + padTop + padBottom;
  const zeroY = padTop + chartH / 2;
  const scale = (chartH / 2 - 10) / maxAbs;

  return (
    <div className="card">
      <div className="card-title">
        <span>
          {touch.symbol} <span className="muted" style={{ fontWeight: 400 }}>{touch.name}</span> — before/after the{" "}
          {touch.date} touch ({touch.touch_pct >= 0 ? "+" : ""}{touch.touch_pct}%)
        </span>
        <button type="button" className="small" onClick={onClose}>Close</button>
      </div>
      <div style={{ overflowX: "auto" }}>
        <svg viewBox={`0 0 ${width} ${height}`} width={width} height={height} style={{ display: "block" }}>
          <line x1={0} y1={zeroY} x2={width} y2={zeroY} stroke="var(--line)" strokeWidth="1" />
          {points.map((p, i) => {
            const x = gap + i * (barW + gap);
            if (p.pct == null) {
              return (
                <text key={i} x={x + barW / 2} y={zeroY + 4} textAnchor="middle" fontSize="10" fill="var(--muted)">—</text>
              );
            }
            const h = Math.max(Math.abs(p.pct) * scale, 1);
            const y = p.pct >= 0 ? zeroY - h : zeroY;
            const color = p.isTouch ? "var(--accent)" : p.pct >= 0 ? "var(--pos)" : "var(--neg)";
            return (
              <g key={i}>
                <rect x={x} y={y} width={barW} height={h} fill={color} rx="2" />
                <text
                  x={x + barW / 2}
                  y={p.pct >= 0 ? y - 4 : y + h + 12}
                  textAnchor="middle"
                  fontSize="10"
                  fontWeight={p.isTouch ? 700 : 400}
                  fill="var(--text)"
                >
                  {p.pct >= 0 ? "+" : ""}{p.pct.toFixed(1)}%
                </text>
              </g>
            );
          })}
          {points.map((p, i) => {
            const x = gap + i * (barW + gap);
            return (
              <g key={`label-${i}`}>
                <text x={x + barW / 2} y={height - 30} textAnchor="middle" fontSize="10" fontWeight={p.isTouch ? 700 : 400} fill="var(--muted)">
                  {p.label}
                </text>
                <text x={x + barW / 2} y={height - 16} textAnchor="middle" fontSize="9" fill="var(--muted)">
                  {p.date ?? ""}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <p className="muted" style={{ fontSize: 12, margin: "8px 0 0" }}>
        Before/after bars are cumulative % move from the touch day's close (offset 0). The touch bar itself
        (black) is that session's own open→close move — what the spike actually looked like.
      </p>
    </div>
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
