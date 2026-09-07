import { useState, useMemo } from "react";
import { useApi } from "../useApi.js";
import { api } from "../api.js";
import { Loading, ErrorBanner } from "../components/Status.jsx";
import { when } from "../format.js";

// Pattern Library page — catalog of discovered and saved spike patterns.
//
// Shows patterns captured from the Backtest report, with their historical
// win rates, sample sizes, and active filter criteria.

function formatFallRate(rate) {
  if (rate == null) return "—";
  const pctVal = Number(rate) * 100;
  return Number(pctVal.toFixed(2)).toString() + "%";
}

function compareNewest(a, b) {
  const timeA = a.created_at ? new Date(a.created_at).getTime() : 0;
  const timeB = b.created_at ? new Date(b.created_at).getTime() : 0;
  if (timeB !== timeA) return timeB - timeA;
  return (b.id || 0) - (a.id || 0);
}

function formatFilterSummary(filters) {
  if (!filters || typeof filters !== "object" || Object.keys(filters).length === 0) {
    return ["All cases (unfiltered)"];
  }

  const items = [];

  // Market cap filter
  if (Array.isArray(filters.cap) && filters.cap.length > 0 && !filters.cap.includes("all")) {
    items.push(`Cap: ${filters.cap.join(", ")}`);
  }

  // Day of week filter
  if (Array.isArray(filters.dow) && filters.dow.length > 0 && !filters.dow.includes("all")) {
    items.push(`Days: ${filters.dow.join(", ")}`);
  }

  // 52-week position filter
  if (Array.isArray(filters.w52) && filters.w52.length > 0 && !filters.w52.includes("all")) {
    items.push(`52W: ${filters.w52.join(", ")}`);
  }

  // Tradeable filter
  if (filters.tradeable && filters.tradeable !== "all") {
    items.push(`Tradeable: ${filters.tradeable}`);
  }

  // Outcome filter
  if (filters.outcome && filters.outcome !== "all") {
    items.push(`Outcome: ${filters.outcome}`);
  }

  // Gap filter
  if (filters.gap && typeof filters.gap === "object" && filters.gap.on) {
    items.push(`Gap: ${filters.gap.direction} ${filters.gap.pct}%`);
  }

  // Prior move filter
  if (filters.beforeMove && typeof filters.beforeMove === "object" && filters.beforeMove.on) {
    items.push(`Prior move: ${filters.beforeMove.direction} ${filters.beforeMove.pct}%`);
  }

  // High / Low condition
  if (filters.high && filters.high !== "all") {
    items.push(`High: ${filters.high}`);
  }
  if (filters.low && filters.low !== "all") {
    items.push(`Low: ${filters.low}`);
  }

  // Arbitrary other keys (e.g. from tests or custom filters)
  const knownKeys = new Set([
    "cap", "dow", "w52", "tradeable", "outcome", "gap", "beforeMove",
    "viewBefore", "viewAfter", "segregation", "low", "high"
  ]);

  for (const [k, v] of Object.entries(filters)) {
    if (knownKeys.has(k)) continue;
    if (v !== null && v !== undefined && typeof v !== "object") {
      items.push(`${k}: ${v}`);
    } else if (typeof v === "object" && v !== null && !Array.isArray(v)) {
      items.push(`${k}: ${JSON.stringify(v)}`);
    } else if (Array.isArray(v) && v.length > 0) {
      items.push(`${k}: ${v.join(", ")}`);
    }
  }

  if (items.length === 0) {
    return ["All cases (unfiltered)"];
  }

  return items;
}

export default function PatternLibraryPage() {
  const { data, loading, error, reload } = useApi(() => api.listPatterns(), []);
  const [sortBy, setSortBy] = useState("newest");

  const patterns = data?.patterns ?? [];

  const sortedPatterns = useMemo(() => {
    const list = [...patterns];
    if (sortBy === "fall_rate") {
      list.sort((a, b) => {
        const diff = (Number(b.fall_rate) || 0) - (Number(a.fall_rate) || 0);
        if (diff !== 0) return diff;
        return compareNewest(a, b);
      });
    } else if (sortBy === "cases") {
      list.sort((a, b) => {
        const diff = (Number(b.cases) || 0) - (Number(a.cases) || 0);
        if (diff !== 0) return diff;
        return compareNewest(a, b);
      });
    } else {
      list.sort(compareNewest);
    }
    return list;
  }, [patterns, sortBy]);

  return (
    <div className="page">
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          flexWrap: "wrap",
          gap: 12,
          marginBottom: 20,
        }}
      >
        <div>
          <h1 style={{ margin: "0 0 6px" }}>Pattern Library</h1>
          <p className="muted" style={{ margin: 0 }}>
            Saved spike patterns and backtested reversion rules.
          </p>
        </div>

        {!loading && !error && patterns.length > 0 && (
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
            <label
              htmlFor="pattern-sort"
              style={{
                margin: 0,
                fontSize: 13,
                color: "var(--muted)",
                fontWeight: 500,
                whiteSpace: "nowrap",
              }}
            >
              Sort:
            </label>
            <select
              id="pattern-sort"
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              style={{
                width: "auto",
                margin: 0,
                padding: "6px 12px",
                fontSize: 13,
                fontWeight: 500,
                cursor: "pointer",
                background: "var(--panel)",
                border: "1px solid var(--line)",
                borderRadius: "var(--radius-sm)",
                color: "var(--text)",
              }}
            >
              <option value="newest">Newest — default</option>
              <option value="fall_rate">Fall rate — highest</option>
              <option value="cases">Cases — highest</option>
            </select>
          </div>
        )}
      </div>

      {loading && <Loading label="Loading saved patterns…" />}
      <ErrorBanner error={error} onRetry={reload} />

      {!loading && !error && patterns.length === 0 && (
        <div className="card">
          <p className="muted" style={{ margin: 0 }}>
            No saved patterns yet.
          </p>
        </div>
      )}

      {!loading && !error && patterns.length > 0 && (
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))" }}>
          {sortedPatterns.map((p) => {
            const dateRange =
              p.test_start_date === p.test_end_date
                ? p.test_start_date
                : `${p.test_start_date} → ${p.test_end_date}`;
            const fallRatePct = formatFallRate(p.fall_rate);
            const createdStr = when(p.created_at);
            const filterSummary = formatFilterSummary(p.filters);

            return (
              <div
                key={p.id}
                className="card"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  justifyContent: "space-between",
                  margin: 0,
                }}
              >
                <div>
                  <div className="card-title" style={{ marginBottom: 10 }}>
                    <span style={{ fontSize: 16, fontWeight: 700 }}>{p.name}</span>
                    <span className="tag">{p.status}</span>
                  </div>

                  <div className="test-range" style={{ fontSize: 13, color: "var(--muted)", marginBottom: 12 }}>
                    <span>Test range: </span>
                    <strong style={{ color: "var(--text)" }}>{dateRange}</strong>
                  </div>

                  <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 14 }}>
                    <span className="fall-rate-value" style={{ fontSize: 26, fontWeight: 700, letterSpacing: "-0.02em" }}>
                      {fallRatePct}
                    </span>
                    <span className="muted" style={{ fontSize: 13 }}>
                      fall rate
                    </span>
                  </div>

                  <dl className="kv" style={{ marginBottom: 14 }}>
                    <div>
                      <dt>Cases</dt>
                      <dd><strong>{p.cases}</strong></dd>
                    </div>
                    <div>
                      <dt>Status</dt>
                      <dd>{p.status}</dd>
                    </div>
                    <div>
                      <dt>Fell</dt>
                      <dd>
                        <span className="tag sell">{p.fell_count}</span>
                      </dd>
                    </div>
                    <div>
                      <dt>Rose</dt>
                      <dd>
                        <span className="tag buy">{p.rose_count}</span>
                      </dd>
                    </div>
                  </dl>

                  <div style={{ borderTop: "1px solid var(--line-soft)", paddingTop: 10, marginTop: 10 }}>
                    <div className="muted" style={{ fontSize: 11, marginBottom: 6, fontWeight: 600, letterSpacing: "0.04em" }}>
                      FILTERS
                    </div>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                      {filterSummary.map((f, idx) => (
                        <span key={idx} className="chip" style={{ margin: 0, fontSize: 11, padding: "2px 8px" }}>
                          {f}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>

                <div
                  className="created-date"
                  style={{
                    borderTop: "1px solid var(--line-soft)",
                    paddingTop: 10,
                    marginTop: 14,
                    fontSize: 11.5,
                    color: "var(--muted)",
                  }}
                >
                  Created: {createdStr || "—"}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
