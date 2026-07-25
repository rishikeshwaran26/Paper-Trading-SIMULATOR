import { useState } from "react";
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine,
} from "recharts";
import { useApi } from "../useApi.js";
import { api } from "../api.js";
import { Loading, ErrorBanner } from "../components/Status.jsx";
import { rupees, pnl, signClass } from "../format.js";

const PERIODS = [
  { key: "5d", label: "5D" },
  { key: "1mo", label: "1M" },
  { key: "3mo", label: "3M" },
  { key: "6mo", label: "6M" },
  { key: "1y", label: "1Y" },
];

// Price history chart for one symbol.
//
// The genuinely useful bit for a trading journal is `avgPrice`: we draw your
// average entry as a dashed reference line across the chart, so you can see at
// a glance whether the position is above or below your cost — and what the
// price did BEFORE and AFTER you bought. A plain price chart tells you what the
// stock did; this one tells you something about your decision.
export default function PriceChart({ symbol, avgPrice }) {
  const [period, setPeriod] = useState("1mo");
  const { data, loading, error, reload } = useApi(
    () => api.priceHistory(symbol, period),
    [symbol, period]
  );

  const candles = data?.candles ?? [];
  const first = candles[0]?.close;
  const last = candles[candles.length - 1]?.close;
  const change = first != null && last != null ? last - first : null;
  const changePct = change != null && first ? (change / first) * 100 : null;

  // Colour the area by whether the period was up or down overall — the muted
  // green/red are the only colours in the app, reserved for money direction.
  const up = (change ?? 0) >= 0;
  const stroke = up ? "#157f3c" : "#c1121f";

  return (
    <section className="card chart-card">
      <div className="card-title">
        <span>
          {symbol} <span className="muted">price history</span>
        </span>
        <span className="seg small-seg">
          {PERIODS.map((p) => (
            <button
              key={p.key}
              type="button"
              className={period === p.key ? "on" : ""}
              onClick={() => setPeriod(p.key)}
            >
              {p.label}
            </button>
          ))}
        </span>
      </div>

      {loading && <Loading label="Fetching price history…" />}
      <ErrorBanner error={error} onRetry={reload} />

      {!loading && !error && candles.length > 0 && (
        <>
          <div className="chart-head">
            <span className="big">{rupees(last)}</span>
            {change != null && (
              <span className={signClass(change)}>
                {pnl(change)} ({changePct >= 0 ? "+" : ""}{changePct.toFixed(2)}%) over {period}
              </span>
            )}
            {avgPrice != null && (
              <span className="muted">· your avg {rupees(avgPrice)}</span>
            )}
          </div>

          <ResponsiveContainer width="100%" height={260}>
            <AreaChart data={candles} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
              <defs>
                <linearGradient id={`grad-${symbol}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={stroke} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={stroke} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#e4e4e7" />
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#6b7280" }} minTickGap={30} />
              <YAxis
                domain={["auto", "auto"]}
                tick={{ fontSize: 11, fill: "#6b7280" }}
                tickFormatter={(v) => v.toFixed(0)}
                width={55}
              />
              <Tooltip
                contentStyle={{ background: "#ffffff", border: "1px solid #d4d4d8", borderRadius: 8, boxShadow: "0 10px 30px rgba(0,0,0,.14)" }}
                formatter={(v, n) => [rupees(v), n]}
                labelStyle={{ color: "#6b7280" }}
              />
              {/* your entry price — the line that makes this chart personal */}
              {avgPrice != null && (
                <ReferenceLine
                  y={avgPrice}
                  stroke="#111111"
                  strokeDasharray="4 4"
                  label={{ value: "your avg", position: "insideTopLeft", fill: "#111111", fontSize: 11 }}
                />
              )}
              <Area
                type="monotone"
                dataKey="close"
                name="close"
                stroke={stroke}
                strokeWidth={2}
                fill={`url(#grad-${symbol})`}
                dot={false}
              />
            </AreaChart>
          </ResponsiveContainer>
          <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
            {candles.length} sessions · data may be delayed ~15 min
          </p>
        </>
      )}
    </section>
  );
}
