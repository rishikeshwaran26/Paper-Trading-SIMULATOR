import { useParams, Link } from "react-router-dom";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell, ReferenceLine,
} from "recharts";
import { useApi } from "../useApi.js";
import { api } from "../api.js";
import { Loading, ErrorBanner } from "../components/Status.jsx";
import { rupees, pnl, signClass } from "../format.js";

// The insight layer: turns the journal from a log into an answer to
// "what kind of trader am I?".
export default function AnalyticsPage() {
  const { name } = useParams();
  const { data, loading, error, reload } = useApi(() => api.analytics(name), [name]);

  if (loading) return <Loading label="Crunching your trades…" />;
  if (error) return <ErrorBanner error={error} onRetry={reload} />;

  const wl = data.winners_vs_losers;
  const byConf = data.by_confidence ?? [];
  const byTag = data.by_tag ?? [];
  const bySide = data.by_side ?? { long: { closed_trades: 0 }, short: { closed_trades: 0 } };

  if (wl.closed_trades === 0) {
    return (
      <div className="page">
        <div className="crumb"><Link className="link" to={`/strategies/${encodeURIComponent(name)}`}>← {name}</Link></div>
        <h1>Journal analytics</h1>
        <p className="muted">
          No closed trades yet. These insights compare trades you've actually sold —
          buy something and sell it, then come back.
        </p>
      </div>
    );
  }

  const confChart = byConf.map((r) => ({
    name: r.label.replace("confidence ", "conf "),
    avg_pnl: r.avg_pnl,
  }));

  return (
    <div className="page">
      <div className="crumb"><Link className="link" to={`/strategies/${encodeURIComponent(name)}`}>← {name}</Link></div>
      <h1>Journal analytics <span className="muted">— {name}</span></h1>

      {/* --- headline: winners vs losers --- */}
      <div className="summary-row">
        <Stat label="Closed trades" value={wl.closed_trades} />
        <Stat label="Win rate" value={`${(wl.win_rate * 100).toFixed(0)}%`} />
        <Stat label="Avg win" value={pnl(wl.winners.avg_pnl)} cls="pos" />
        <Stat label="Avg loss" value={pnl(wl.losers.avg_pnl)} cls="neg" />
        <Stat
          label="Payoff ratio"
          value={wl.payoff_ratio ?? "—"}
          cls={wl.payoff_ratio >= 1 ? "pos" : "neg"}
        />
      </div>

      <section className="card">
        <div className="card-title">Holding duration — winners vs losers</div>
        <table className="table">
          <thead>
            <tr>
              <th></th><th className="num">Trades</th><th className="num">Total P&L</th>
              <th className="num">Avg P&L</th><th className="num">Avg held (days)</th><th className="num">Avg confidence</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><span className="tag buy">WINNERS</span></td>
              <td className="num">{wl.winners.count}</td>
              <td className="num pos">{pnl(wl.winners.total_pnl)}</td>
              <td className="num pos">{pnl(wl.winners.avg_pnl)}</td>
              <td className="num">{wl.winners.avg_holding_days}</td>
              <td className="num">{wl.winners.avg_confidence ?? "—"}</td>
            </tr>
            <tr>
              <td><span className="tag sell">LOSERS</span></td>
              <td className="num">{wl.losers.count}</td>
              <td className="num neg">{pnl(wl.losers.total_pnl)}</td>
              <td className="num neg">{pnl(wl.losers.avg_pnl)}</td>
              <td className="num">{wl.losers.avg_holding_days}</td>
              <td className="num">{wl.losers.avg_confidence ?? "—"}</td>
            </tr>
          </tbody>
        </table>

        {/* The single most useful sentence on the page. */}
        <p className={`insight ${wl.holding_gap_days > 0 ? "warn" : "good"}`}>
          {wl.holding_gap_days > 0 ? (
            <>
              You hold losers <strong>{wl.holding_gap_days} days longer</strong> than winners on
              average. That's the classic disposition effect — cutting gains early while hoping
              losses come back.
            </>
          ) : wl.holding_gap_days < 0 ? (
            <>
              You hold winners <strong>{Math.abs(wl.holding_gap_days)} days longer</strong> than
              losers — you're letting profits run and cutting losses. That's the habit you want.
            </>
          ) : (
            <>You hold winners and losers for about the same time.</>
          )}
        </p>
      </section>

      {/* --- long vs short: is the shorting actually working, on its own? --- */}
      {(bySide.long.closed_trades > 0 || bySide.short.closed_trades > 0) && (
        <section className="card">
          <div className="card-title">Long vs short performance</div>
          <p className="muted" style={{ marginTop: 0 }}>
            Betting on a rise and betting on a fall are different skills — this
            keeps them from hiding inside one blended number.
          </p>
          <table className="table">
            <thead>
              <tr>
                <th></th><th className="num">Trades</th><th className="num">Win rate</th>
                <th className="num">Total P&L</th><th className="num">Avg P&L</th><th className="num">Avg held (days)</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><span className="tag buy">LONG</span></td>
                <td className="num">{bySide.long.closed_trades}</td>
                <td className="num">{(bySide.long.win_rate * 100).toFixed(0)}%</td>
                <td className={`num ${signClass(bySide.long.total_pnl)}`}>{pnl(bySide.long.total_pnl)}</td>
                <td className={`num ${signClass(bySide.long.avg_pnl)}`}>{pnl(bySide.long.avg_pnl)}</td>
                <td className="num">{bySide.long.avg_holding_days}</td>
              </tr>
              <tr>
                <td><span className="tag short">SHORT</span></td>
                <td className="num">{bySide.short.closed_trades}</td>
                <td className="num">{(bySide.short.win_rate * 100).toFixed(0)}%</td>
                <td className={`num ${signClass(bySide.short.total_pnl)}`}>{pnl(bySide.short.total_pnl)}</td>
                <td className={`num ${signClass(bySide.short.avg_pnl)}`}>{pnl(bySide.short.avg_pnl)}</td>
                <td className="num">{bySide.short.avg_holding_days}</td>
              </tr>
            </tbody>
          </table>
        </section>
      )}

      {/* --- by confidence --- */}
      <section className="card">
        <div className="card-title">Average P&L by confidence level</div>
        <p className="muted" style={{ marginTop: 0 }}>
          Does your self-rated conviction actually predict results? If the bars don't rise
          left-to-right, your confidence isn't tracking reality.
        </p>
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={confChart} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
            <XAxis dataKey="name" tick={{ fontSize: 12, fill: "#6b7280" }} />
            <YAxis tick={{ fontSize: 12, fill: "#6b7280" }} />
            <Tooltip
              formatter={(v) => [rupees(v), "avg P&L"]}
              contentStyle={{ background: "#ffffff", border: "1px solid #d4d4d8", borderRadius: 8, boxShadow: "0 10px 30px rgba(0,0,0,.14)" }}
            />
            <ReferenceLine y={0} stroke="#9ca3af" />
            <Bar dataKey="avg_pnl" radius={[4, 4, 0, 0]}>
              {confChart.map((d, i) => (
                <Cell key={i} fill={d.avg_pnl >= 0 ? "#157f3c" : "#c1121f"} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
        <StatsTable rows={byConf} firstCol="Confidence" />
      </section>

      {/* --- by tag --- */}
      <section className="card">
        <div className="card-title">Win rate by tag</div>
        <p className="muted" style={{ marginTop: 0 }}>
          Which of your setups actually works? Compare “technical breakout” against
          “earnings play” on equal footing.
        </p>
        <StatsTable rows={byTag} firstCol="Tag" />
      </section>
    </div>
  );
}

function StatsTable({ rows, firstCol }) {
  if (!rows.length) return <p className="muted">Nothing to show yet.</p>;
  return (
    <table className="table">
      <thead>
        <tr>
          <th>{firstCol}</th><th className="num">Trades</th><th className="num">Win rate</th>
          <th className="num">Total P&L</th><th className="num">Avg P&L</th><th className="num">Avg held (days)</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.label}>
            <td>{r.label}</td>
            <td className="num">{r.closed_trades}</td>
            <td className="num">{(r.win_rate * 100).toFixed(0)}%</td>
            <td className={`num ${signClass(r.total_pnl)}`}>{pnl(r.total_pnl)}</td>
            <td className={`num ${signClass(r.avg_pnl)}`}>{pnl(r.avg_pnl)}</td>
            <td className="num">{r.avg_holding_days}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Stat({ label, value, cls = "" }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${cls}`}>{value}</div>
    </div>
  );
}
