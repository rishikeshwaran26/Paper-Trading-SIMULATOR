import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams, Link } from "react-router-dom";
import { createChart, CandlestickSeries, HistogramSeries, createSeriesMarkers } from "lightweight-charts";
import { useApi } from "../useApi.js";
import { api } from "../api.js";
import { Loading, ErrorBanner } from "../components/Status.jsx";

// Each range picks its own candle size — a year of 5-minute candles would be
// unreadable, and a single day of daily candles is one bar.
const RANGES = [
  { key: "1D", period: "1d", interval: "5m" },
  { key: "5D", period: "5d", interval: "15m" },
  { key: "1M", period: "1mo", interval: "1d" },
  { key: "3M", period: "3mo", interval: "1d" },
  { key: "6M", period: "6mo", interval: "1d" },
  { key: "1Y", period: "1y", interval: "1d" },
  { key: "5Y", period: "5y", interval: "1d" },
];

// Read the shape off the data, never off the selected range: a range switch
// re-renders with the OLD candles still in hand for one pass, and formatting
// daily bars as intraday (or the reverse) produces NaN times that the chart
// rejects outright.
const isIntraday = (candles) => (candles[0]?.date ?? "").includes(" ");

// lightweight-charts wants a "YYYY-MM-DD" string for daily bars but a UNIX
// timestamp for intraday ones. The backend hands back "YYYY-MM-DD HH:MM" in the
// exchange's own clock, so it is parsed as UTC deliberately: that makes the axis
// print back the same wall-clock time the data was labelled with, instead of
// shifting it by the viewer's timezone.
function toChartTime(dateStr) {
  if (!dateStr.includes(" ")) return dateStr;
  return Date.parse(`${dateStr.replace(" ", "T")}:00Z`) / 1000;
}

// The chart's `Time` is a union — the string/number we handed it, or a
// {year,month,day} BusinessDay it may normalise a date string into. Everything
// funnels through here so the crosshair's time and our map keys can't disagree.
function timeKey(time) {
  if (time == null) return null;
  if (typeof time === "object") {
    const { year, month, day } = time;
    return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  }
  return String(time);
}

// A real candlestick chart for one stock — the TradingView-style view a bar
// chart can't give you: full OHLC, volume, a marker on the touch day, and a
// legend that follows the crosshair. Click any candle to pin its numbers.
export default function StockChartPage() {
  const { symbol } = useParams();
  const [params] = useSearchParams();
  const touchDate = params.get("date"); // set when opened from a backtest touch row
  const [range, setRange] = useState(RANGES.find((r) => r.key === "1Y"));
  const [hovered, setHovered] = useState(null);
  const [pinned, setPinned] = useState(null);
  const containerRef = useRef(null);

  const { data, loading, error, reload } = useApi(
    () => api.priceHistory(symbol, range.period, range.interval),
    [symbol, range.period, range.interval]
  );
  const candles = data?.candles ?? [];

  // Percent change is against the PREVIOUS candle's close, which is what a
  // trading terminal shows — so it has to be precomputed against the series,
  // not derived from one candle in isolation.
  const byTime = useMemo(() => {
    const map = new Map();
    candles.forEach((c, i) => {
      const prevClose = i > 0 ? candles[i - 1].close : null;
      map.set(timeKey(toChartTime(c.date)), {
        ...c,
        changePct: prevClose ? ((c.close - prevClose) / prevClose) * 100 : null,
      });
    });
    return map;
  }, [candles]);

  // lightweight-charts owns its own canvas — it is not a React-rendered tree,
  // so this is imperative by design, torn down and rebuilt when the data changes.
  useEffect(() => {
    if (!containerRef.current || candles.length === 0) return;
    setHovered(null);
    setPinned(null);

    const chart = createChart(containerRef.current, {
      layout: { background: { color: "#ffffff" }, textColor: "#0a0a0a" },
      grid: { vertLines: { color: "#efeff1" }, horzLines: { color: "#efeff1" } },
      rightPriceScale: { borderColor: "#e4e4e7" },
      timeScale: { borderColor: "#e4e4e7", timeVisible: isIntraday(candles) },
      crosshair: { mode: 0 }, // free crosshair, like TradingView
      autoSize: true,
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#157f3c",
      downColor: "#c1121f",
      borderVisible: false,
      wickUpColor: "#157f3c",
      wickDownColor: "#c1121f",
    });
    candleSeries.setData(
      candles.map((c) => ({
        time: toChartTime(c.date),
        open: c.open, high: c.high, low: c.low, close: c.close,
      }))
    );

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volumeSeries.setData(
      candles.map((c) => ({
        time: toChartTime(c.date),
        value: c.volume,
        color: c.close >= c.open ? "rgba(21,127,60,.5)" : "rgba(193,18,31,.5)",
      }))
    );

    if (touchDate && !isIntraday(candles) && candles.some((c) => c.date === touchDate)) {
      createSeriesMarkers(candleSeries, [
        { time: touchDate, position: "aboveBar", color: "#111111", shape: "arrowDown", text: "Touch" },
      ]);
    }

    chart.subscribeCrosshairMove((param) => {
      setHovered(byTime.get(timeKey(param.time)) ?? null);
    });
    chart.subscribeClick((param) => {
      const hit = byTime.get(timeKey(param.time));
      if (hit) setPinned(hit);
    });

    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [candles, byTime, touchDate]);

  // The crosshair wins while you're on the chart; the pinned candle is what
  // stays behind once the pointer leaves, so a clicked bar keeps its numbers.
  const shown = hovered ?? pinned;
  const first = candles[0]?.close;
  const last = candles[candles.length - 1]?.close;
  const rangePct = first ? ((last - first) / first) * 100 : null;

  return (
    <section className="card chart-card">
      <div className="card-title">
        <span>
          {symbol} <span className="muted">candlestick chart</span>
          {touchDate && <span className="muted"> · touch on {touchDate}</span>}
        </span>
        <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span className="seg small-seg">
            {RANGES.map((r) => (
              <button key={r.key} type="button" className={range.key === r.key ? "on" : ""} onClick={() => setRange(r)}>
                {r.key}
              </button>
            ))}
          </span>
          <Link to="/backtest" className="small">Close</Link>
        </span>
      </div>

      {loading && <Loading label="Fetching price history…" />}
      <ErrorBanner error={error} onRetry={reload} />

      {!loading && !error && candles.length === 0 && (
        <p className="muted">No price history available for {symbol} over {range.key}.</p>
      )}

      {!loading && !error && candles.length > 0 && (
        <>
          <div className="chart-legend">
            {shown ? (
              <>
                <strong>{shown.date}</strong>
                <span>O <b>{shown.open}</b></span>
                <span>H <b>{shown.high}</b></span>
                <span>L <b>{shown.low}</b></span>
                <span>C <b>{shown.close}</b></span>
                {shown.changePct != null && (
                  <span className={shown.changePct >= 0 ? "pos" : "neg"}>
                    {shown.changePct >= 0 ? "+" : ""}{shown.changePct.toFixed(2)}%
                  </span>
                )}
                <span className="muted">Vol {shown.volume.toLocaleString("en-IN")}</span>
                {pinned && !hovered && (
                  <button type="button" className="small" onClick={() => setPinned(null)}>unpin</button>
                )}
              </>
            ) : (
              <span className="muted">
                {candles.length} candles over {range.key} ·{" "}
                {rangePct != null && (
                  <span className={rangePct >= 0 ? "pos" : "neg"}>
                    {rangePct >= 0 ? "+" : ""}{rangePct.toFixed(2)}%
                  </span>
                )}{" "}
                — hover a candle for its OHLC, click one to pin it.
              </span>
            )}
          </div>
          <div ref={containerRef} style={{ width: "100%", height: 480 }} />
        </>
      )}
    </section>
  );
}
