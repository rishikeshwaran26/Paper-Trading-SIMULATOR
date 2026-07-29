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

// --- how the stock moved on its way INTO the touch --------------------------
//
// Both read off `close` on the stored before-days, so they measure ordinary
// forward-in-time price change. Deliberately NOT pct_from_prev_day: on
// before-days that field is computed walking BACKWARDS from the touch, so its
// sign reads inverted for this question.

// One before-day's own daily move — its close against the previous trading
// day's close, exactly what "the stock was 6% down on Jan 4" means.
//
// Day -k needs the close of day -(k+1), so the furthest tracked before-day can
// never have one. Returns null when it cannot be computed, and callers treat
// null as "cannot judge" rather than as 0%.
function beforeDayOwnMove(t, offset) {
  const days = t.before_days ?? [];
  const cur = days.find((d) => d.offset === offset);
  const prior = days.find((d) => d.offset === offset - 1);
  if (!cur || !prior || !prior.close) return null;
  return ((cur.close - prior.close) / prior.close) * 100;
}

// The whole run-up: close on the furthest visible before-day against the close
// on the day right before the touch.
function beforeWindowMove(t, span) {
  const days = t.before_days ?? [];
  const near = days.find((d) => d.offset === -1);
  const far = days.find((d) => d.offset === -Math.min(span, days.length));
  if (!near || !far || !far.close || near.offset === far.offset) return null;
  return ((near.close - far.close) / far.close) * 100;
}

// The move a beforeMove filter is testing, for one touch. null = unjudgeable.
function beforeMoveValue(t, bm, span) {
  return bm.mode === "single" ? beforeDayOwnMove(t, bm.offset) : beforeWindowMove(t, span);
}

function passesBeforeMove(t, bm, span) {
  if (!bm.on) return true;
  const v = beforeMoveValue(t, bm, span);
  if (v == null) return false; // never silently counted as a 0% move
  return bm.direction === "fell" ? v <= -bm.pct : v >= bm.pct;
}

// null on viewBefore/viewAfter means "however many the run tracked".
const DEFAULT_FILTERS = {
  // Pre-trade: known before the trade would have been placed, so these are
  // allowed to drive the prediction panel.
  viewBefore: null,
  viewAfter: null,
  beforeMove: {
    on: false,
    mode: "cumulative", // "cumulative" | "single"
    offset: -1,         // used when mode === "single"
    direction: "fell",  // "fell" | "rose"
    pct: 5,             // threshold, always a positive number
  },
  // View-only: this is the thing being PREDICTED, so it may never feed the
  // panel — filtering by it and then scoring it would always read 100%.
  outcome: "all",       // "all" | "up" | "down"
};

// Cohort stats at one day-offset, over whatever set of touches is passed in.
// Mirrors engine.backtest._summarize_one_day: a touch with no data that far out
// is excluded from `scored` rather than counted as a flat 0% — a missing day is
// not a zero move, and treating it as one would drag the averages toward
// nothing.
function summarizeOffset(touches, offset, direction = "up") {
  const moves = [];
  for (const t of touches) {
    const list = offset >= 0 ? t.follow_days : t.before_days;
    const d = (list ?? []).find((x) => x.offset === offset);
    if (d) moves.push(d.pct_from_touch);
  }
  const up = moves.filter((m) => m > 0);
  const down = moves.filter((m) => m < 0);
  const scored = moves.length;
  const r2 = (v) => Math.round(v * 100) / 100;
  const avg = (xs) => (xs.length ? r2(xs.reduce((a, b) => a + b, 0) / xs.length) : null);
  const share = (xs) => (scored ? Math.round((xs.length / scored) * 1000) / 10 : 0);

  // Expected value of taking the thesis trade at the touch close, in % per
  // trade. An "up" band study shorts the spike, so a negative move is a gain
  // and EV is just the mean negated; a "down" study is the bounce thesis (a
  // long), so the sign flips.
  //
  // This is the number a high down-% can hide: a cohort can fall 6 times out
  // of 10 and still lose money if the 4 that rise, rise further.
  const meanPct = scored ? moves.reduce((a, b) => a + b, 0) / scored : null;
  const ev = meanPct == null ? null : r2(direction === "up" ? -meanPct : meanPct);

  return {
    offset,
    scored,
    up_count: up.length,
    down_count: down.length,
    flat_count: moves.filter((m) => m === 0).length,
    up_pct: share(up),
    down_pct: share(down),
    avg_pct_up: avg(up),
    avg_pct_down: avg(down),
    ev,
  };
}

function countActiveFilters(f, runBefore, runAfter) {
  let n = 0;
  if (f.beforeMove.on) n++;
  if (f.outcome !== "all") n++;
  if (f.viewBefore != null && f.viewBefore < runBefore) n++;
  if (f.viewAfter != null && f.viewAfter < runAfter) n++;
  return n;
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
  // Everything the drawer controls, in one object so "Clear all" is a single
  // reset and the active-filter count is easy to derive. All of it narrows what
  // is DISPLAYED from the scan already in memory — none of it re-scans.
  //   outcome     — rows that rose / fell on the day being viewed (the touch
  //                 day's own move when "All days" is selected).
  //   trend       — was the stock climbing or sliding into the touch.
  //   type        — how much of the intraday spike survived to the close.
  //   viewBefore  — how many of the tracked before-days to show (null = all).
  //   viewAfter   — same for follow-days.
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [drawerOpen, setDrawerOpen] = useState(false);

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
      // Filters deliberately SURVIVE a run change, clamped to what the new run
      // tracked. That turns the Past runs list into out-of-sample testing: set a
      // rule on January, click February, and the same rule lands on data it was
      // never tuned on. A rule that only works on the month it was found in is
      // curve-fitting, and this is the cheapest way to catch that.
      const nb = d.run?.before_days ?? 0;
      const nf = d.run?.follow_days ?? 1;
      setFilters((f) => ({
        ...f,
        viewBefore: f.viewBefore == null ? null : Math.min(f.viewBefore, nb),
        viewAfter: f.viewAfter == null ? null : Math.max(1, Math.min(f.viewAfter, nf)),
        beforeMove: { ...f.beforeMove, offset: Math.max(f.beforeMove.offset, -nb || -1) },
      }));
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

  // How many tracked days the drawer is currently letting through. Capped by
  // what the run actually scanned — you can narrow the view, never widen it
  // past the data.
  const viewBefore = Math.min(filters.viewBefore ?? runBeforeDays, runBeforeDays);
  const viewAfter = Math.min(filters.viewAfter ?? runFollowDays, runFollowDays);

  // PRE-TRADE ONLY. Everything here was knowable before the trade would have
  // been placed, so it is legitimate to score a prediction on this set.
  const predictionTouches = useMemo(
    () => sortedTouches.filter((t) => passesBeforeMove(t, filters.beforeMove, viewBefore)),
    [sortedTouches, filters.beforeMove, viewBefore]
  );

  // + the outcome filter. Rows only — never the panel, because the outcome is
  // the thing being predicted; scoring a set selected BY that outcome would
  // always read 100% and mean nothing.
  const visibleTouches = useMemo(() => {
    if (filters.outcome === "all") return predictionTouches;
    return predictionTouches.filter((t) =>
      filters.outcome === "up" ? t._outcome > 0 : t._outcome != null && t._outcome < 0
    );
  }, [predictionTouches, filters.outcome]);

  const upCount = predictionTouches.filter((t) => t._outcome > 0).length;
  const downCount = predictionTouches.filter((t) => t._outcome != null && t._outcome < 0).length;
  const activeFilterCount = countActiveFilters(filters, runBeforeDays, runFollowDays);

  // Narrowing the window can strand the selected day outside it — snap back to
  // day +1, which every run has.
  useEffect(() => {
    if (viewDay > viewAfter || viewDay < -viewBefore) setViewDay(1);
  }, [viewAfter, viewBefore, viewDay]);

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
          predictionTouches={predictionTouches}
          totalCount={touches.length}
          outcomeFilter={filters.outcome}
          beforeMoveOn={filters.beforeMove.on}
          onSort={toggleSort}
          sortArrow={arrow}
          viewDay={viewDay}
          onViewDay={setViewDay}
          followDays={viewAfter}
          beforeDays={viewBefore}
          selectedTouch={selectedTouch}
          onSelectTouch={setSelectedTouch}
          findDay={findDay}
          onOpenFilters={() => setDrawerOpen(true)}
          activeFilterCount={activeFilterCount}
        />
      )}

      {drawerOpen && (
        <FilterDrawer
          filters={filters}
          onChange={setFilters}
          onClose={() => setDrawerOpen(false)}
          runBeforeDays={runBeforeDays}
          runFollowDays={runFollowDays}
          upCount={upCount}
          downCount={downCount}
          rows={rows}
          viewBefore={viewBefore}
          matchCount={visibleTouches.length}
          predictionCount={predictionTouches.length}
          totalCount={touches.length}
          sort={sort}
          onSort={toggleSort}
          onClearSort={() => setSort({ key: null, dir: null })}
          viewDay={viewDay}
        />
      )}
    </>
  );
}

function BandStudyResult({
  run, touches, predictionTouches, totalCount, outcomeFilter, beforeMoveOn, onSort, sortArrow,
  viewDay, onViewDay, followDays, beforeDays,
  selectedTouch, onSelectTouch, findDay, onOpenFilters, activeFilterCount,
}) {
  const flaggedCount = touches.filter((t) => t.corporate_action_suspected).length;
  const dayOffsets = Array.from({ length: followDays }, (_, i) => i + 1);
  const beforeOffsets = Array.from({ length: beforeDays }, (_, i) => i - beforeDays); // -N ... -1
  const allOffsets = [...beforeOffsets, ...dayOffsets];

  // Scored on predictionTouches, NOT on the rows below: those have had the
  // outcome filter applied, and scoring a set picked by its outcome would just
  // report that outcome back at 100%. Recomputed rather than read off the run
  // because the pre-trade filters are the whole point — "of the stocks that
  // were sliding into the touch, how many fell afterwards?" is a different
  // question from "of all touches...".
  const allPerDay = useMemo(
    () => allOffsets.map((offset) => summarizeOffset(predictionTouches, offset, run.direction)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [predictionTouches, followDays, beforeDays, run.direction]
  );
  const selected = allPerDay.find((d) => d.offset === viewDay) ?? null;
  const showingAll = viewDay === 0;
  // "All days" has no single day to score, so the panel falls back to day +1.
  const panel = showingAll ? allPerDay.find((d) => d.offset === 1) ?? null : selected;
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

      {/* THE PREDICTION PANEL. Always scored on predictionTouches, so it stays
          an honest read no matter what the outcome filter is doing below. */}
      <div className="summary-row">
        <Stat
          label={predictionTouches.length === totalCount ? "Total touches" : "Touches (filtered)"}
          value={predictionTouches.length}
        />
        <Stat
          label={showingAll ? "Scored on day +1" : `Scored on ${dayLabel(viewDay)}`}
          value={panel?.scored ?? 0}
        />
        <Stat
          label={showingAll ? "Up by day +1" : `Up by ${dayLabel(viewDay)}`}
          value={`${panel?.up_pct ?? 0}%`}
          cls="pos"
        />
        <Stat
          label={showingAll ? "Down by day +1" : `Down by ${dayLabel(viewDay)}`}
          value={`${panel?.down_pct ?? 0}%`}
          cls="neg"
        />
        <Stat
          label="Avg down / up"
          value={
            <>
              <span className="neg">{panel?.avg_pct_down != null ? pct(panel.avg_pct_down) : "—"}</span>
              <span className="muted"> / </span>
              <span className="pos">{panel?.avg_pct_up != null ? pct(panel.avg_pct_up) : "—"}</span>
            </>
          }
        />
        <Stat
          label="EV per trade"
          value={panel?.ev != null ? pct(panel.ev) : "—"}
          cls={panel?.ev == null ? "" : panel.ev > 0 ? "pos" : "neg"}
        />
      </div>

      {outcomeFilter !== "all" && (
        <p className="muted" style={{ fontSize: 12.5, margin: "-4px 0 14px" }}>
          The rows below are filtered to the {outcomeFilter === "up" ? "risers" : "fallers"} ({touches.length} of{" "}
          {predictionTouches.length}), but the panel above still scores all {predictionTouches.length} — filtering by
          the outcome and then scoring it would just read 100% and predict nothing.
        </p>
      )}

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
              <th className="num" title="Expected % per trade: what the whole cohort averages, in the thesis trade's favour">
                EV
              </th>
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
                <td className={`num ${d.ev == null ? "" : d.ev > 0 ? "pos" : "neg"}`}>
                  {d.ev != null ? pct(d.ev) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted" style={{ fontSize: 12.5, margin: "10px 0 0" }}>
          <strong>EV</strong> is what one trade averages, taken at the touch day's close. A high down-% still loses
          money when the minority that rise, rise further — this column is the one that catches that.
        </p>
      </div>

      {/* Every touch, shown separately */}
      <section className="card">
        <div className="card-title">
          <span>
            {run.start_date === run.end_date ? run.start_date : `${run.start_date} → ${run.end_date}`} — every{" "}
            {run.direction === "up" ? "spike" : "drop"} that touched {run.band_low}–{run.band_high}%
          </span>
          <button type="button" className="small" onClick={onOpenFilters}>
            Filters &amp; sort
            {activeFilterCount > 0 && <span className="filter-badge">{activeFilterCount}</span>}
          </button>
        </div>
        <p className="muted" style={{ fontSize: 12.5, margin: "0 0 12px" }}>
          Showing <strong>{touches.length}</strong> of {totalCount} touches
          {activeFilterCount > 0 ? ` (${activeFilterCount} filter${activeFilterCount === 1 ? "" : "s"} on)` : ""}.
        </p>
        {touches.length === 0 && (
          <p className="muted" style={{ margin: "0 0 12px" }}>
            {beforeDays === 0 && beforeMoveOn ? (
              <>
                The <strong>Before-move</strong> filter is on, but this run tracked no days before each touch, so
                there is nothing for it to measure. Clear the filter, or re-scan this range with a before-days
                window.
              </>
            ) : (
              <>No touches match these filters — open <strong>Filters &amp; sort</strong> and widen or clear them.</>
            )}
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

// =============================================================================
// FILTER DRAWER — narrows what's DISPLAYED from the scan already in memory.
// Nothing here re-scans, so every change is instant; the trade-off is that you
// can only ever narrow the tracked window, never widen it past what the run
// actually fetched.
// =============================================================================
function FilterDrawer({
  filters, onChange, onClose, runBeforeDays, runFollowDays,
  upCount, downCount, rows, viewBefore, matchCount, predictionCount, totalCount,
  sort, onSort, onClearSort, viewDay,
}) {
  const [tab, setTab] = useState("window");
  const set = (patch) => onChange({ ...filters, ...patch });
  const setBM = (patch) => onChange({ ...filters, beforeMove: { ...filters.beforeMove, ...patch } });
  const bm = filters.beforeMove;

  // Live preview of what the current before-move settings would keep, plus how
  // many touches simply cannot be judged (no close on the day it needs).
  const bmCounts = useMemo(() => {
    let fell = 0, rose = 0, unknown = 0;
    for (const t of rows) {
      const v = beforeMoveValue(t, bm, viewBefore);
      if (v == null) unknown += 1;
      else if (v <= -bm.pct) fell += 1;
      else if (v >= bm.pct) rose += 1;
    }
    return { fell, rose, unknown };
  }, [rows, bm.mode, bm.offset, bm.pct, viewBefore]);

  const dayLabel = (n) => (n > 0 ? `Day +${n}` : `Day ${n}`);
  const sortCols = [
    ["symbol", "Symbol"],
    ["date", "Touch date"],
    ["touch_pct", "Touch %"],
    ["close_pct", "Close %"],
    ["_pct", `${viewDay === 0 ? "Viewed day" : dayLabel(viewDay)} change`],
    ["_open_close", "Open→close"],
  ];

  // Day -1 needs day -2's close, so the furthest tracked day can never be
  // picked in single-day mode.
  const singleDayOffsets = Array.from({ length: Math.max(0, runBeforeDays - 1) }, (_, i) => -(i + 1));

  const cats = [
    ["window", "Window", filters.viewBefore != null || filters.viewAfter != null],
    ["before", "Before-move", bm.on],
    ["outcome", "Outcome", filters.outcome !== "all"],
    ["sort", "Sort by", !!sort.key],
  ];

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h2>Filters &amp; sort</h2>
          <button type="button" className="drawer-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="drawer-body">
          <div className="drawer-rail">
            {cats.map(([id, label, on]) => (
              <button key={id} type="button" className={tab === id ? "on" : ""} onClick={() => setTab(id)}>
                {label}
                {on && <span className="rail-count"> •</span>}
              </button>
            ))}
          </div>

          <div className="drawer-pane">
            {tab === "window" && (
              <>
                <label>
                  Before days to show
                  <input
                    type="number" min="0" max={runBeforeDays} value={filters.viewBefore ?? runBeforeDays}
                    onChange={(e) => set({ viewBefore: Math.min(Number(e.target.value), runBeforeDays) })}
                    disabled={runBeforeDays === 0}
                  />
                </label>
                <label style={{ marginTop: 14 }}>
                  Follow-up days to show
                  <input
                    type="number" min="1" max={runFollowDays} value={filters.viewAfter ?? runFollowDays}
                    onChange={(e) => set({ viewAfter: Math.max(1, Math.min(Number(e.target.value), runFollowDays)) })}
                  />
                </label>
                <p className="drawer-note">
                  This run tracked <strong>{runBeforeDays}</strong> day{runBeforeDays === 1 ? "" : "s"} before and{" "}
                  <strong>{runFollowDays}</strong> after each touch. Narrowing here changes the day buttons, the
                  summary table, the per-day columns and the bar chart together — instantly, with no re-scan. To go
                  wider than the numbers above you have to run the scan again.
                </p>
              </>
            )}

            {tab === "before" && (
              <>
                {runBeforeDays === 0 && (
                  <div className="inline-error" style={{ marginTop: 0, marginBottom: 12 }}>
                    This run tracked no days before each touch, so there is nothing to measure. Re-scan the range
                    with a before-days window to use this filter.
                  </div>
                )}
                <Radio name="bmon" value={false} cur={bm.on} onPick={() => setBM({ on: false })}
                  label="None" note={`all ${rows.length}`} />
                <Radio name="bmon" value={true} cur={bm.on} onPick={() => setBM({ on: true })}
                  label="Filter by the move before the touch"
                  note={`${bm.direction === "fell" ? bmCounts.fell : bmCounts.rose} match`} />

                <div style={{ opacity: bm.on ? 1 : 0.45, pointerEvents: bm.on ? "auto" : "none", marginTop: 14 }}>
                  <div className="seg" style={{ marginBottom: 12 }}>
                    <button type="button" className={bm.mode === "cumulative" ? "on" : ""}
                      onClick={() => setBM({ mode: "cumulative" })}>
                      Whole window
                    </button>
                    <button type="button" className={bm.mode === "single" ? "on" : ""}
                      onClick={() => setBM({ mode: "single" })} disabled={singleDayOffsets.length === 0}>
                      One day
                    </button>
                  </div>

                  <div className="row">
                    {bm.mode === "single" && (
                      <label>
                        Which day
                        <select value={bm.offset} onChange={(e) => setBM({ offset: Number(e.target.value) })}>
                          {singleDayOffsets.map((o) => (
                            <option key={o} value={o}>{dayLabel(o)}</option>
                          ))}
                        </select>
                      </label>
                    )}
                    <label>
                      Direction
                      <select value={bm.direction} onChange={(e) => setBM({ direction: e.target.value })}>
                        <option value="fell">Fell at least</option>
                        <option value="rose">Rose at least</option>
                      </select>
                    </label>
                    <label>
                      Threshold %
                      <input type="number" min="0" step="0.5" value={bm.pct}
                        onChange={(e) => setBM({ pct: Math.max(0, Number(e.target.value)) })} />
                    </label>
                  </div>
                </div>

                <p className="drawer-note">
                  {bm.mode === "cumulative" ? (
                    <>Measures the whole run-in: the close {viewBefore} trading day{viewBefore === 1 ? "" : "s"} before
                    the touch against the close on the day right before it.</>
                  ) : (
                    <>Measures that one session's own change — its close against the previous trading day's close,
                    which is what "it was 6% down on Jan 4" normally means.</>
                  )}{" "}
                  Right now <strong>{bmCounts.fell}</strong> touch{bmCounts.fell === 1 ? "" : "es"} fell ≥ {bm.pct}%
                  and <strong>{bmCounts.rose}</strong> rose ≥ {bm.pct}%.
                  {bmCounts.unknown > 0 && (
                    <> {bmCounts.unknown} cannot be measured this way (no close on the day it needs) and are excluded
                    while this filter is on — never counted as a 0% move.</>
                  )}
                </p>
                <p className="drawer-note">
                  Check both directions at the same threshold before trusting a result. If “fell” and “rose” give
                  roughly the same down-%, the filter is not carrying any signal — the number moved because the
                  sample got smaller, not because the rule found something.
                </p>
              </>
            )}

            {tab === "outcome" && (
              <>
                <Radio name="outcome" value="all" cur={filters.outcome} onPick={(v) => set({ outcome: v })}
                  label="None" note={`all ${predictionCount}`} />
                <Radio name="outcome" value="up" cur={filters.outcome} onPick={(v) => set({ outcome: v })}
                  label="Rose only" note={`${upCount}`} />
                <Radio name="outcome" value="down" cur={filters.outcome} onPick={(v) => set({ outcome: v })}
                  label="Fell only" note={`${downCount}`} />
                <p className="drawer-note">
                  Judged on the day currently selected ({viewDay === 0 ? "the touch day's own move, since all days are shown" : dayLabel(viewDay)}),
                  so these counts change as you switch days.
                </p>
                <p className="drawer-note">
                  <strong>This one only hides table rows — it never touches the panel above.</strong> The panel is
                  measuring how often the prediction came true, and picking the rows by that same outcome would just
                  report it back as 100%. Use this to read through the fallers, not to score them.
                </p>
              </>
            )}

            {tab === "sort" && (
              <>
                <Radio name="sortcol" value="" cur={sort.key ?? ""} onPick={() => onClearSort()}
                  label="Normal (scan order)" />
                {sortCols.map(([key, label]) => (
                  // onClick lives on the input, not the label: a label wrapping
                  // an input forwards clicks to it, so a handler on both would
                  // advance the sort cycle twice per click. onClick (not
                  // onChange) so re-picking the current column still cycles it.
                  <label key={key} className="opt">
                    <input type="radio" name="sortcol" readOnly checked={sort.key === key}
                      onClick={() => onSort(key)} />
                    <span>
                      {label}
                      {sort.key === key && (
                        <span className="opt-note"> — {sort.dir === "asc" ? "ascending ↑" : "descending ↓"}</span>
                      )}
                    </span>
                  </label>
                ))}
                <p className="drawer-note">
                  Same as clicking a column heading in the table: pick once for ascending, again for descending, a
                  third time to go back to the scan's own order.
                </p>
              </>
            )}
          </div>
        </div>

        <div className="drawer-foot">
          <button type="button" className="ghost" onClick={() => onChange(DEFAULT_FILTERS)}>
            Clear all
          </button>
          <button type="button" className="primary" onClick={onClose}>
            Show {matchCount} of {totalCount}
            {filters.outcome !== "all" && ` · scoring ${predictionCount}`}
          </button>
        </div>
      </div>
    </div>
  );
}

function Radio({ name, value, cur, onPick, label, note }) {
  return (
    <label className="opt">
      <input type="radio" name={name} checked={cur === value} onChange={() => onPick(value)} />
      <span>
        {label}
        {note != null && <span className="opt-note"> — {note}</span>}
      </span>
    </label>
  );
}
