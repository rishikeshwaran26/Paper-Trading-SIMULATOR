import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
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

// A "single %" run is stored as band_high=100 (the API's own ceiling, used as
// "no practical upper limit"). Rendered as "20%+" rather than "20-100%" so it
// reads the way it was actually configured.
function bandLabel(low, high) {
  return high >= 100 ? `${low}%+` : `${low}–${high}%`;
}

// Resolves any sortable column's value for one touch. `offset:<n>` keys cover
// the per-day columns shown in the "all days" table view; the rest cover the
// fixed columns shown in the single-day view.
// How far a day's high (or low) ran from the TOUCH day's close — the same
// baseline pct_from_touch uses, so the three columns read as one story: how far
// the trade could have gone in your favour, how far it went against you, and
// where it actually settled.
function dayExtremePct(t, day, which) {
  const price = day?.[which];
  if (price == null || !t.close) return null;
  return ((price - t.close) / t.close) * 100;
}

// In "all days" view no single day is selected, so `_day` is null. The touch
// itself carries the same `high`/`low`/`close` fields, so it stands in — the
// same convention the outcome filter uses there (it falls back to the touch
// day's own move). Without this a Low/High pick would silently empty the table.
const extremeDay = (t) => t._day ?? t;

function sortValue(t, key, viewDay, findDay, metric = "close") {
  // The per-day columns sort on whichever move is currently being scored, so
  // the ordering always matches the numbers actually on screen.
  const move = (d) => (d == null ? null : metric === "intraday" ? d.open_close_pct : d.pct_from_touch);
  if (key === "symbol") return t.symbol;
  if (key === "date") return t.date;
  if (key === "touch_pct") return t.touch_pct;
  if (key === "close_pct") return t.close_pct;
  if (key === "_day_date") return findDay(t, viewDay)?.date ?? null;
  if (key === "_pct") return move(findDay(t, viewDay)) ?? null;
  if (key === "_high") return dayExtremePct(t, viewDay === 0 ? t : findDay(t, viewDay), "high");
  if (key === "_low") return dayExtremePct(t, viewDay === 0 ? t : findDay(t, viewDay), "low");
  if (key.startsWith("offset:")) return move(findDay(t, Number(key.slice(7)))) ?? null;
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

// Did the stock close at (or within a whisker of) its own high?
//
// On a 19-20% touch that almost always means it was locked at the upper
// circuit: only buyers, no sellers, so there was nobody to sell to and the
// short could not have been opened at all. Counting those in a short-selling
// study measures trades nobody could have placed, which is why they are
// excluded by default rather than offered as a neutral filter.
function isCircuitLocked(t) {
  if (t.touch_pct == null || t.close_pct == null) return false;
  return Math.abs(t.touch_pct - t.close_pct) <= 0.15;
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
  // Defaults to excluding circuit-locked touches: they were untradeable, so
  // leaving them in overstates a strategy nobody could have run.
  tradeable: "exclude_locked", // "exclude_locked" | "all" | "only_locked"
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
  // View-only, same reason as `outcome`: where the viewed day's low and high
  // landed relative to the touch close is part of what happened AFTER the
  // touch, so these narrow the rows without ever touching the panel.
  //   "up"   — stayed above the touch close (low) / traded above it (high)
  //   "down" — dipped below the touch close (low) / never got above it (high)
  low: "all",           // "all" | "up" | "down"
  high: "all",          // "all" | "up" | "down"
  // Pre-trade: cap bucket is visible before placing the trade.
  // Multi-select: ["all"] means no filter; any specific buckets narrow to those.
  cap: ["all"],         // ["all"] | subset of ["large","mid","small","unknown"]
  // Pre-trade: which weekday the touch itself landed on. Multi-select, same
  // shape as cap: ["all"] means no filter. Values are JS getUTCDay() numbers
  // (1=Mon..5=Fri) as strings, since touch dates never fall on a weekend.
  dow: ["all"],
};

// t.date is "YYYY-MM-DD". Parsed as UTC midnight by the Date constructor, so
// getUTCDay() (not getDay()) is the one that matches the calendar date
// regardless of the browser's local timezone.
function touchDow(t) {
  return String(new Date(t.date).getUTCDay());
}

// Cohort stats at one day-offset, over whatever set of touches is passed in.
// Mirrors engine.backtest._summarize_one_day: a touch with no data that far out
// is excluded from `scored` rather than counted as a flat 0% — a missing day is
// not a zero move, and treating it as one would drag the averages toward
// nothing.
// What one intraday round trip costs in India, as a % of turnover: brokerage,
// STT, stamp duty, exchange fees and GST, rounded to a realistic all-in figure
// for a discount broker. Deducted from EV so the panel shows what a trade would
// actually have kept — a gross EV of +0.1% is a losing trade once this is paid.
const ROUND_TRIP_COST_PCT = 0.10;

// `metric` picks WHICH move is being scored:
//   "close"    — pct_from_touch: the touch day's close to this day's close.
//                Contains the overnight gap between the two sessions.
//   "intraday" — open_close_pct: this session's own open to its own close, the
//                only part an intraday trade (in by 9:15, out by 3:30) actually
//                captures. A stock can gap up overnight and still fall all day;
//                "close" scores that as a rise, "intraday" as the fall it was.
function summarizeOffset(touches, offset, direction = "up", metric = "close") {
  const moves = [];
  for (const t of touches) {
    const list = offset >= 0 ? t.follow_days : t.before_days;
    const d = (list ?? []).find((x) => x.offset === offset);
    if (!d) continue;
    const v = metric === "intraday" ? d.open_close_pct : d.pct_from_touch;
    if (v != null) moves.push(v); // no open price -> unscorable, never a flat 0%
  }
  const up = moves.filter((m) => m > 0);
  const down = moves.filter((m) => m < 0);
  const scored = moves.length;
  const r2 = (v) => Math.round(v * 100) / 100;
  const avg = (xs) => (xs.length ? r2(xs.reduce((a, b) => a + b, 0) / xs.length) : null);
  const share = (xs) => (scored ? Math.round((xs.length / scored) * 1000) / 10 : 0);

  // Expected value of taking the thesis trade, in % per trade — entered at the
  // touch close for "close", at that session's open for "intraday". An "up"
  // band study shorts the spike, so a negative move is a gain and EV is just
  // the mean negated; a "down" study is the bounce thesis (a long), so the
  // sign flips.
  //
  // This is the number a high down-% can hide: a cohort can fall 6 times out
  // of 10 and still lose money if the 4 that rise, rise further.
  const meanPct = scored ? moves.reduce((a, b) => a + b, 0) / scored : null;
  const evGross = meanPct == null ? null : (direction === "up" ? -meanPct : meanPct);
  const ev = evGross == null ? null : r2(evGross - ROUND_TRIP_COST_PCT);

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
    ev_gross: evGross == null ? null : r2(evGross),
  };
}

function countActiveFilters(f, runBefore, runAfter) {
  let n = 0;
  if (f.beforeMove.on) n++;
  if (f.tradeable !== "exclude_locked") n++; // the default is itself a filter
  if (f.outcome !== "all") n++;
  if (f.low !== "all") n++;
  if (f.high !== "all") n++;
  if (!f.cap.includes("all")) n++;
  if (!f.dow.includes("all")) n++;
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
  // "range" — a tight band (e.g. 19-20%), the original mode.
  // "single" — one threshold, no upper cap: "touched at least 20%", counting
  // anything at or above it. Sent to the backend as band_low=threshold,
  // band_high=100 (the API's own max), which is "no practical ceiling" for
  // real single-day moves.
  const [bandMode, setBandMode] = useState("range");
  const [bandLow, setBandLow] = useState(19);
  const [bandHigh, setBandHigh] = useState(20);
  const [threshold, setThreshold] = useState(20);
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
  // "close" | "intraday" — which move every score on this page is measured on.
  // See summarizeOffset. Intraday is the honest one for a same-session trade.
  const [metric, setMetric] = useState("close");

  const [run, setRun] = useState(null);
  const [runs, setRuns] = useState([]);
  const [caps, setCaps] = useState({});   // symbol -> "large"|"mid"|"small"|"unknown"
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
    setCaps({});
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
      // Caps are fetched separately so the run loads instantly and badges arrive
      // a moment later (first call fetches from Yahoo, subsequent from cache).
      api.getBandStudyRunCaps(id).then((c) => setCaps(c.caps ?? {})).catch(() => {});
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
        band_low: bandMode === "single" ? Number(threshold) : Number(bandLow),
        band_high: bandMode === "single" ? 100 : Number(bandHigh),
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
    const dayMove = (d) => (d == null ? null : metric === "intraday" ? d.open_close_pct : d.pct_from_touch);
    return touches.map((t) => {
      const d = viewDay !== 0 ? findDay(t, viewDay) : null;
      // The value "up only"/"down only" judges by: the viewed day's move, or
      // the touch day's own move when "All days" is selected (no single day to
      // judge by there). Follows the metric so the filter agrees with the panel.
      const outcomeVal = viewDay !== 0 ? dayMove(d) : t.close_pct;
      return { ...t, _day: d, _pct: dayMove(d), _outcome: outcomeVal };
    });
  }, [touches, viewDay, metric]);

  const sortedTouches = useMemo(() => {
    const { key, dir } = sort;
    if (!key || !dir) return rows; // "normal": whatever order the backend returned
    const mul = dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = sortValue(a, key, viewDay, findDay, metric);
      const bv = sortValue(b, key, viewDay, findDay, metric);
      if (av == null && bv == null) return 0;
      if (av == null) return 1;  // rows with no data this far out (or this column) sink to the bottom
      if (bv == null) return -1;
      if (typeof av === "string" && typeof bv === "string") return av.localeCompare(bv) * mul;
      return (av - bv) * mul;
    });
  }, [rows, sort, viewDay, metric]);

  // How many tracked days the drawer is currently letting through. Capped by
  // what the run actually scanned — you can narrow the view, never widen it
  // past the data.
  const viewBefore = Math.min(filters.viewBefore ?? runBeforeDays, runBeforeDays);
  const viewAfter = Math.min(filters.viewAfter ?? runFollowDays, runFollowDays);

  // PRE-TRADE ONLY. Everything here was knowable before the trade would have
  // been placed, so it is legitimate to score a prediction on this set.
  const predictionTouches = useMemo(
    () => sortedTouches.filter((t) => {
      if (filters.tradeable === "exclude_locked" && isCircuitLocked(t)) return false;
      if (filters.tradeable === "only_locked" && !isCircuitLocked(t)) return false;
      if (!filters.cap.includes("all")) {
        const bucket = caps[t.symbol] ?? "unknown";
        if (!filters.cap.includes(bucket)) return false;
      }
      if (!filters.dow.includes("all") && !filters.dow.includes(touchDow(t))) return false;
      return passesBeforeMove(t, filters.beforeMove, viewBefore);
    }),
    [sortedTouches, filters.beforeMove, filters.tradeable, filters.cap, filters.dow, caps, viewBefore]
  );

  // + the outcome filter. Rows only — never the panel, because the outcome is
  // the thing being predicted; scoring a set selected BY that outcome would
  // always read 100% and mean nothing.
  const visibleTouches = useMemo(() => {
    return predictionTouches.filter((t) => {
      if (filters.outcome !== "all") {
        const ok = filters.outcome === "up" ? t._outcome > 0 : t._outcome != null && t._outcome < 0;
        if (!ok) return false;
      }
      // A day with no data has no low/high to judge, so it drops out of a
      // specific pick rather than silently counting as one side.
      for (const [key, which] of [[filters.low, "low"], [filters.high, "high"]]) {
        if (key === "all") continue;
        const v = dayExtremePct(t, extremeDay(t), which);
        if (v == null) return false;
        if (key === "up" ? v < 0 : v >= 0) return false;
      }
      return true;
    });
  }, [predictionTouches, filters.outcome, filters.low, filters.high]);

  // Plain-English list of whichever view-only filters are on, for the note that
  // explains why the row count and the panel count disagree.
  const viewOnlyFilters = useMemo(() => {
    const out = [];
    if (filters.outcome !== "all") out.push(filters.outcome === "up" ? "the risers" : "the fallers");
    if (filters.low !== "all") out.push(filters.low === "up" ? "days whose low stayed above the touch close" : "days whose low dipped below the touch close");
    if (filters.high !== "all") out.push(filters.high === "up" ? "days that traded above the touch close" : "days that never got above the touch close");
    return out;
  }, [filters.outcome, filters.low, filters.high]);

  // Only what the circuit-lock rule ITSELF removed — a touch that the
  // before-move filter would have dropped anyway is not counted here, so the
  // note never overstates the exclusion.
  const lockedExcluded = useMemo(() => {
    if (filters.tradeable !== "exclude_locked") return 0;
    return rows.filter((t) => isCircuitLocked(t) && passesBeforeMove(t, filters.beforeMove, viewBefore)).length;
  }, [rows, filters.tradeable, filters.beforeMove, viewBefore]);

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
        <div className="seg" style={{ marginBottom: 12 }}>
          <button type="button" className={bandMode === "range" ? "on" : ""} onClick={() => setBandMode("range")}>
            Band range
          </button>
          <button type="button" className={bandMode === "single" ? "on" : ""} onClick={() => setBandMode("single")}>
            Single %
          </button>
        </div>
        {bandMode === "single" && (
          <p className="muted" style={{ fontSize: 12, margin: "-6px 0 12px" }}>
            No upper limit — counts anything at or above this move, either direction.
          </p>
        )}
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
          {bandMode === "single" ? (
            <label>Touched at least (%)
              <input type="number" min="1" max="99" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
            </label>
          ) : (
            <>
              <label>Band low (%)
                <input type="number" min="1" max="99" value={bandLow} onChange={(e) => setBandLow(e.target.value)} />
              </label>
              <label>Band high (%)
                <input type="number" min="1" max="100" value={bandHigh} onChange={(e) => setBandHigh(e.target.value)} />
              </label>
            </>
          )}
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
                  {" "}({bandLabel(r.band_low, r.band_high)}, {r.source})
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
            No stock touched {bandLabel(run.run.band_low, run.run.band_high)} in this range — try a wider band or a longer range.
          </p>
        </div>
      )}

      {run && run.run.status === "done" && touches.length > 0 && (
        <BandStudyResult
          run={run.run}
          touches={visibleTouches}
          predictionTouches={predictionTouches}
          totalCount={touches.length}
          viewOnlyFilters={viewOnlyFilters}
          beforeMoveOn={filters.beforeMove.on}
          lockedExcluded={lockedExcluded}
          metric={metric}
          onMetric={setMetric}
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
          caps={caps}
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
          predictionRows={predictionTouches}
          caps={caps}
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
  run, touches, predictionTouches, totalCount, viewOnlyFilters, beforeMoveOn, lockedExcluded, metric, onMetric,
  onSort, sortArrow, viewDay, onViewDay, followDays, beforeDays,
  selectedTouch, onSelectTouch, findDay, onOpenFilters, activeFilterCount, caps,
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
    () => allOffsets.map((offset) => summarizeOffset(predictionTouches, offset, run.direction, metric)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [predictionTouches, followDays, beforeDays, run.direction, metric]
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

      {/* Which move every number on this page is scored on. Both are already in
          the data, so switching is instant. */}
      <div className="card" style={{ padding: "12px 16px" }}>
        <div className="card-title" style={{ marginBottom: 8 }}>Score each day on</div>
        <div className="seg" style={{ marginBottom: 8 }}>
          <button type="button" className={metric === "close" ? "on" : ""} onClick={() => onMetric("close")}>
            Close-to-close
          </button>
          <button type="button" className={metric === "intraday" ? "on" : ""} onClick={() => onMetric("intraday")}>
            Open→close (intraday)
          </button>
        </div>
        <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
          {metric === "close" ? (
            <>
              Measured from the touch day's close, so it includes the <strong>overnight gap</strong> into each
              session. Right for a position held overnight — but a same-session trade never gets that gap.
            </>
          ) : (
            <>
              Each session's own <strong>open to close</strong> — the only part an intraday trade (in after 9:15,
              out before 3:30) actually captures. A stock can gap up overnight and still fall all day: close-to-close
              scores that as a rise, this scores it as the fall it was.
            </>
          )}
        </p>
      </div>

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
          label="EV per trade (net)"
          value={panel?.ev != null ? pct(panel.ev) : "—"}
          cls={panel?.ev == null ? "" : panel.ev > 0 ? "pos" : "neg"}
        />
      </div>

      {lockedExcluded > 0 && (
        <p className="muted" style={{ fontSize: 12.5, margin: "-4px 0 14px" }}>
          <strong>{lockedExcluded}</strong> touch{lockedExcluded === 1 ? "" : "es"} left out as frozen
          (closed at its own high, so there were no sellers to short into). Change that under{" "}
          <strong>Filters &amp; sort → Circuit freeze</strong>.
        </p>
      )}

      {/* Every view-only filter lands here: each narrows the rows while the
          panel keeps scoring the full prediction set, and the reader has to be
          told, or the two counts look like a bug. */}
      {viewOnlyFilters.length > 0 && (
        <p className="muted" style={{ fontSize: 12.5, margin: "-4px 0 14px" }}>
          The rows below are filtered to {viewOnlyFilters.join(", ")} ({touches.length} of{" "}
          {predictionTouches.length}), but the panel above still scores all {predictionTouches.length} — these describe
          what happened after the touch, so filtering by them and then scoring would just report the choice back.
        </p>
      )}

      {/* The per-touch graph — click any stock below to see it here. */}
      {selectedTouch && (
        <TouchBarChart touch={selectedTouch} beforeOffsets={beforeOffsets} dayOffsets={dayOffsets} findDay={findDay} metric={metric} onClose={() => onSelectTouch(null)} />
      )}

      {/* The report: every tracked day, up and down kept separate. Cumulative
          from the touch day's close, so "day +3" answers "where is it now, 3
          days on?" and "day -3" answers "where was it, 3 days before?" */}
      <div className="card">
        <div className="card-title">
          {metric === "intraday"
            ? "Each day's own open→close move (up vs down kept separate)"
            : "Cumulative move by each tracked day (up vs down kept separate)"}
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
              <th className="num" title={`Expected % per trade, after ${ROUND_TRIP_COST_PCT}% round-trip costs`}>
                EV (net)
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
          <strong>EV (net)</strong> is what one trade averages after {ROUND_TRIP_COST_PCT}% round-trip costs
          (brokerage, STT, stamp duty, exchange fees). A high down-% still loses money when the minority that rise,
          rise further — this column is the one that catches that.
        </p>
      </div>

      {/* Every touch, shown separately */}
      <section className="card">
        <div className="card-title">
          <span>
            {run.start_date === run.end_date ? run.start_date : `${run.start_date} → ${run.end_date}`} — every{" "}
            {run.direction === "up" ? "spike" : "drop"} that touched {bandLabel(run.band_low, run.band_high)}
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
                  <th className="num sortable" onClick={() => onSort("_low")} title="Lowest the stock traded that day, measured from the touch day's close">
                    Low{sortArrow("_low")}
                  </th>
                  <th className="num sortable" onClick={() => onSort("_high")} title="Highest the stock traded that day, measured from the touch day's close">
                    High{sortArrow("_high")}
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
                    {isCircuitLocked(t) && (
                      <span className="tag warn" title="Closed at its own high — almost certainly frozen at the upper circuit, so it could not have been shorted">
                        frozen
                      </span>
                    )}
                    {caps[t.symbol] && caps[t.symbol] !== "unknown" && (
                      <span className={`tag cap-${caps[t.symbol]}`} title={`${caps[t.symbol]} cap`}>
                        {caps[t.symbol]}
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
                      const v = d == null ? null : metric === "intraday" ? d.open_close_pct : d.pct_from_touch;
                      return (
                        <td
                          key={n}
                          className={`num ${v == null ? "" : v >= 0 ? "pos" : "neg"}`}
                          title={d ? `${d.date} · open ${d.open ?? "—"} · close ${d.close}` : "no data this far out"}
                        >
                          {v != null ? pct(v) : "—"}
                        </td>
                      );
                    })
                  ) : (
                    <>
                      <td className="num muted">{t._day?.date ?? "—"}</td>
                      <td className={`num ${t._pct != null ? (t._pct >= 0 ? "pos" : "neg") : ""}`}>
                        {t._pct != null ? pct(t._pct) : "—"}
                      </td>
                      {[["low", "_low"], ["high", "_high"]].map(([which]) => {
                        const v = dayExtremePct(t, t._day, which);
                        return (
                          <td key={which} className={`num ${v == null ? "" : v >= 0 ? "pos" : "neg"}`}>
                            {v != null ? pct(v) : "—"}
                          </td>
                        );
                      })}
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
                  <td style={{ display: "flex", gap: 6 }}>
                    <button
                      type="button"
                      className={`small ${isSelected ? "primary" : ""}`}
                      onClick={() => onSelectTouch(isSelected ? null : t)}
                    >
                      {isSelected ? "Hide" : "Chart"}
                    </button>
                    <Link
                      to={`/backtest/chart/${encodeURIComponent(t.symbol)}?date=${encodeURIComponent(t.date)}`}
                      target="_blank"
                      className="small"
                      title="Open full candlestick chart in a new tab"
                    >
                      Candles ↗
                    </Link>
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
function TouchBarChart({ touch, beforeOffsets, dayOffsets, findDay, metric, onClose }) {
  const points = useMemo(() => {
    const move = (d) => (d == null ? null : metric === "intraday" ? d.open_close_pct : d.pct_from_touch);
    const bar = (n, label) => {
      const d = findDay(touch, n);
      return { offset: n, label, date: d?.date ?? null, pct: move(d) };
    };
    const before = beforeOffsets.map((n) => bar(n, `D${n}`));
    // The touch bar is that session's own open->close either way — it is the
    // spike itself, so "cumulative from the touch" would always be zero.
    const touchPoint = { offset: 0, label: "Touch", date: touch.date, pct: touch.close_pct, isTouch: true };
    const after = dayOffsets.map((n) => bar(n, `D+${n}`));
    return [...before, touchPoint, ...after];
  }, [touch, beforeOffsets, dayOffsets, findDay, metric]);

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
        {metric === "intraday"
          ? "Before/after bars are each session's own open→close move — what an intraday trade in that session would have captured."
          : "Before/after bars are cumulative % move from the touch day's close (offset 0)."}{" "}
        The touch bar itself (black) is that session's own open→close move — what the spike actually looked like.
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
  upCount, downCount, rows, predictionRows, caps, viewBefore, matchCount, predictionCount, totalCount,
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
    ["_low", "Low"],
    ["_high", "High"],
  ];

  // Day -1 needs day -2's close, so the furthest tracked day can never be
  // picked in single-day mode.
  const singleDayOffsets = Array.from({ length: Math.max(0, runBeforeDays - 1) }, (_, i) => -(i + 1));

  const lockedCount = useMemo(() => rows.filter(isCircuitLocked).length, [rows]);

  const cats = [
    ["window", "Window", filters.viewBefore != null || filters.viewAfter != null],
    ["before", "Before-move", bm.on],
    ["tradeable", "Circuit freeze", filters.tradeable !== "exclude_locked"],
    ["cap", "Market cap", !filters.cap.includes("all")],
    ["dow", "Day of week", !filters.dow.includes("all")],
    ["outcome", "Outcome", filters.outcome !== "all"],
    ["lowhigh", "Low / High", filters.low !== "all" || filters.high !== "all"],
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

            {tab === "tradeable" && (
              <>
                <Radio name="tradeable" value="exclude_locked" cur={filters.tradeable}
                  onPick={(v) => set({ tradeable: v })}
                  label="Not frozen only (tradeable)" note={`${rows.length - lockedCount}`} />
                <Radio name="tradeable" value="all" cur={filters.tradeable} onPick={(v) => set({ tradeable: v })}
                  label="All — frozen and not" note={`all ${rows.length}`} />
                <Radio name="tradeable" value="only_locked" cur={filters.tradeable} onPick={(v) => set({ tradeable: v })}
                  label="Frozen only" note={`${lockedCount}`} />
                <p className="drawer-note">
                  <strong>Frozen</strong> means it closed within 0.15 points of the same high (or low) it touched —
                  so it locked at the circuit for the rest of the session, with no sellers (or buyers) left to trade
                  into. <strong>{lockedCount} of {rows.length}</strong> touch{lockedCount === 1 ? "" : "es"} in this
                  run froze this way.
                </p>
                <p className="drawer-note">
                  Frozen touches are excluded by default because including them measures trades that were
                  impossible to place. Switch to "All" to see how much they were flattering the numbers.
                </p>
              </>
            )}

            {tab === "cap" && (() => {
              const CAP_OPTS = [
                { key: "large", label: "Large cap", note: "₹20,000 cr+" },
                { key: "mid",   label: "Mid cap",   note: "₹5,000–20,000 cr" },
                { key: "small", label: "Small cap",  note: "below ₹5,000 cr" },
                { key: "unknown", label: "Unknown",  note: "not yet fetched" },
              ];
              const countFor = (k) => rows.filter((t) => (caps[t.symbol] ?? "unknown") === k).length;
              const isAll = filters.cap.includes("all");
              const toggleCap = (k) => {
                if (k === "all") { set({ cap: ["all"] }); return; }
                const cur = filters.cap.filter((x) => x !== "all");
                const next = cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k];
                set({ cap: next.length ? next : ["all"] });
              };
              const capsLoaded = Object.keys(caps).length > 0;
              return (
                <>
                  {!capsLoaded && (
                    <p className="drawer-note" style={{ marginTop: 0 }}>
                      Fetching market cap data… badges will appear shortly.
                    </p>
                  )}
                  <div className="chip-row">
                    <button
                      type="button"
                      className={`chip${isAll ? " on" : ""}`}
                      onClick={() => toggleCap("all")}
                    >
                      All <span className="chip-note">{rows.length}</span>
                    </button>
                    {CAP_OPTS.map(({ key, label }) => (
                      <button
                        key={key}
                        type="button"
                        className={`chip${!isAll && filters.cap.includes(key) ? " on" : ""}`}
                        onClick={() => toggleCap(key)}
                      >
                        {label} <span className="chip-note">{countFor(key)}</span>
                      </button>
                    ))}
                  </div>
                  <p className="drawer-note">
                    Buckets use SEBI thresholds: large ≥ ₹20,000 cr, mid ₹5,000–20,000 cr, small below ₹5,000 cr.
                    Caps are fetched from Yahoo Finance for touched symbols only and cached for 7 days.
                    Multi-select — pick any combination.
                  </p>
                </>
              );
            })()}

            {tab === "dow" && (() => {
              const DOW_OPTS = [
                { key: "1", label: "Mon" },
                { key: "2", label: "Tue" },
                { key: "3", label: "Wed" },
                { key: "4", label: "Thu" },
                { key: "5", label: "Fri" },
              ];
              const countFor = (k) => rows.filter((t) => touchDow(t) === k).length;
              const isAll = filters.dow.includes("all");
              const toggleDow = (k) => {
                if (k === "all") { set({ dow: ["all"] }); return; }
                const cur = filters.dow.filter((x) => x !== "all");
                const next = cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k];
                set({ dow: next.length ? next : ["all"] });
              };
              return (
                <>
                  <div className="chip-row">
                    <button type="button" className={`chip${isAll ? " on" : ""}`} onClick={() => toggleDow("all")}>
                      All <span className="chip-note">{rows.length}</span>
                    </button>
                    {DOW_OPTS.map(({ key, label }) => (
                      <button
                        key={key}
                        type="button"
                        className={`chip${!isAll && filters.dow.includes(key) ? " on" : ""}`}
                        onClick={() => toggleDow(key)}
                      >
                        {label} <span className="chip-note">{countFor(key)}</span>
                      </button>
                    ))}
                  </div>
                  <p className="drawer-note">
                    Filters to touches whose spike happened on the picked weekday(s). Multi-select — pick any
                    combination.
                  </p>
                </>
              );
            })()}

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

            {tab === "lowhigh" && (() => {
              const countFor = (which, dir) =>
                predictionRows.filter((t) => {
                  const v = dayExtremePct(t, extremeDay(t), which);
                  return v != null && (dir === "up" ? v >= 0 : v < 0);
                }).length;
              const group = (which, label, upLabel, downLabel, upHint, downHint) => (
                <>
                  <div className="card-title" style={{ marginBottom: 8 }}>{label}</div>
                  <Radio name={which} value="all" cur={filters[which]} onPick={(v) => set({ [which]: v })}
                    label="Any" note={`${predictionRows.length}`} />
                  <Radio name={which} value="up" cur={filters[which]} onPick={(v) => set({ [which]: v })}
                    label={upLabel} note={`${countFor(which, "up")}`} />
                  <Radio name={which} value="down" cur={filters[which]} onPick={(v) => set({ [which]: v })}
                    label={downLabel} note={`${countFor(which, "down")}`} />
                  <p className="drawer-note">{upHint} {downHint}</p>
                </>
              );
              return (
                <>
                  {group("low", "Day's low", "Up — stayed above", "Down — dipped below",
                    "Up keeps days whose lowest trade never fell under the touch close.",
                    "Down keeps the ones that went under it at some point.")}
                  {group("high", "Day's high", "Up — traded above", "Down — never got above",
                    "Up keeps days that traded above the touch close at some point.",
                    "Down keeps the ones whose best price still sat under it.")}
                  <p className="drawer-note">
                    Both read against the <strong>touch day's close</strong> on the day currently selected
                    ({viewDay === 0 ? "the touch day's own range, since all days are shown" : dayLabel(viewDay)}), so
                    the counts move as you switch days. Days with no data are dropped by a specific pick rather than
                    counted as one side.
                  </p>
                  <p className="drawer-note">
                    <strong>Like Outcome, these only hide table rows — the panel above is untouched.</strong> Where the
                    low and high landed is part of what happened after the touch, so scoring a set chosen by it would
                    just report that choice back.
                  </p>
                </>
              );
            })()}

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
