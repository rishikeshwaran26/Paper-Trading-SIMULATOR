import { useEffect, useMemo, useState } from "react";
import { useApi } from "../useApi.js";
import { api } from "../api.js";
import { Loading, ErrorBanner } from "../components/Status.jsx";

const MONTH_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

// RESEARCH — a trading-idea notebook: Idea -> Year -> Month, not a trade
// journal (that's the per-strategy one under Strategies). Each month holds a
// hand-typed stock table (or one copied in from a past Backtest run, so a
// month you've already scanned never needs retyping), your own notes and
// total count, and the idea's reusable checklist ticked for that month.
export default function ResearchPage() {
  const { data, loading, error, reload } = useApi(() => api.listResearchIdeas(), []);
  const ideas = data?.ideas ?? [];
  const [selectedPeriodId, setSelectedPeriodId] = useState(null);
  // Cleared at the exact point something gets deleted (idea or month), not by
  // reconciling against the ideas list on every change — that reactive check
  // raced the reload triggered by CREATING a period: onChanged() and
  // onSelectPeriod() land in the same batched render, so `ideas` was still
  // the pre-creation snapshot when the "does it still exist" check ran, and
  // it wiped the selection the instant it was made.

  return (
    <div className="page">
      <h1>Research</h1>
      <p className="muted" style={{ marginTop: -12, marginBottom: 20 }}>
        Your own notebook for a trading idea — organized by year and month, so you can see whether it's actually
        getting better over time.
      </p>

      {loading && <Loading label="Loading research…" />}
      <ErrorBanner error={error} onRetry={reload} />

      {data && (
        <div className="research-layout">
          <IdeaTree
            ideas={ideas}
            selectedPeriodId={selectedPeriodId}
            onSelectPeriod={setSelectedPeriodId}
            onChanged={reload}
            onClearSelection={() => setSelectedPeriodId(null)}
          />
          <div className="research-panel">
            {selectedPeriodId == null ? (
              <div className="card">
                <p className="muted" style={{ margin: 0 }}>
                  {ideas.length === 0
                    ? "Create your first idea on the left to start a notebook for it."
                    : "Pick a month on the left, or create one under a year, to open its notes."}
                </p>
              </div>
            ) : (
              <PeriodPanel
                periodId={selectedPeriodId}
                onChanged={reload}
                onDeleted={() => setSelectedPeriodId(null)}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// --- sidebar: Idea -> Year -> Month tree -------------------------------------
function IdeaTree({ ideas, selectedPeriodId, onSelectPeriod, onChanged, onClearSelection }) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setSubmitting(true);
    try {
      await api.createResearchIdea(name.trim());
      setName("");
      setCreating(false);
      onChanged?.();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <aside className="research-tree">
      {ideas.map((idea) => (
        <IdeaNode
          key={idea.id}
          idea={idea}
          selectedPeriodId={selectedPeriodId}
          onSelectPeriod={onSelectPeriod}
          onChanged={onChanged}
          onClearSelection={onClearSelection}
        />
      ))}

      {creating ? (
        <form onSubmit={submit} style={{ marginTop: 8 }}>
          <input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. 20% Frozen"
            style={{ marginBottom: 6 }}
          />
          <div className="row" style={{ gap: 6 }}>
            <button className="primary small" disabled={!name.trim() || submitting} style={{ marginTop: 0 }}>
              {submitting ? "Creating…" : "Create"}
            </button>
            <button type="button" className="small" onClick={() => { setCreating(false); setName(""); }}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button type="button" className="small" style={{ marginTop: 8, width: "100%" }} onClick={() => setCreating(true)}>
          + New idea
        </button>
      )}
    </aside>
  );
}

function IdeaNode({ idea, selectedPeriodId, onSelectPeriod, onChanged, onClearSelection }) {
  const [open, setOpen] = useState(true);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [addingMonth, setAddingMonth] = useState(false);
  const [monthValue, setMonthValue] = useState("");

  // Group this idea's flat period list by year, newest year first.
  const byYear = useMemo(() => {
    const map = new Map();
    for (const p of idea.periods) {
      if (!map.has(p.year)) map.set(p.year, []);
      map.get(p.year).push(p);
    }
    for (const list of map.values()) list.sort((a, b) => a.month - b.month);
    return [...map.entries()].sort((a, b) => b[0] - a[0]);
  }, [idea.periods]);

  async function addMonth(e) {
    e.preventDefault();
    if (!monthValue) return;
    const [y, m] = monthValue.split("-").map(Number);
    const { period } = await api.createResearchPeriod(idea.id, y, m);
    setMonthValue("");
    setAddingMonth(false);
    onChanged?.();
    onSelectPeriod(period.id);
  }

  async function deleteIdea() {
    // Clear the selection FIRST if it's about to be deleted out from under
    // the panel — this idea's own periods list is right here, no need to
    // reconcile against the (about to be stale) parent list.
    if (idea.periods.some((p) => p.id === selectedPeriodId)) onClearSelection?.();
    await api.deleteResearchIdea(idea.id).catch(() => {});
    onChanged?.();
  }

  return (
    <div className="research-idea">
      <div className="research-idea-head" onClick={() => setOpen((o) => !o)}>
        <span className="research-tree-caret">{open ? "▾" : "▸"}</span>
        <span className="research-idea-name">{idea.name}</span>
      </div>

      {open && (
        <div className="research-idea-body">
          {byYear.length === 0 && <p className="muted research-empty-note">No months yet.</p>}
          {byYear.map(([year, periods]) => (
            <div key={year} className="research-year">
              <div className="research-year-label">{year}</div>
              {periods.map((p) => (
                <button
                  type="button"
                  key={p.id}
                  className={`research-month ${selectedPeriodId === p.id ? "on" : ""}`}
                  onClick={() => onSelectPeriod(p.id)}
                >
                  {MONTH_NAMES[p.month - 1]}
                  <span className="research-month-count">{p.row_count}</span>
                </button>
              ))}
            </div>
          ))}

          {addingMonth ? (
            <form onSubmit={addMonth} className="research-month-form">
              <input type="month" autoFocus value={monthValue} onChange={(e) => setMonthValue(e.target.value)} />
              <div className="row" style={{ gap: 6, marginTop: 6 }}>
                <button className="primary small" disabled={!monthValue} style={{ marginTop: 0 }}>Add</button>
                <button type="button" className="small" onClick={() => setAddingMonth(false)}>Cancel</button>
              </div>
            </form>
          ) : (
            <button type="button" className="link" style={{ fontSize: 12.5 }} onClick={() => setAddingMonth(true)}>
              + add a month
            </button>
          )}

          <div style={{ marginTop: 10 }}>
            {confirmDelete ? (
              <span>
                <span className="muted" style={{ fontSize: 12 }}>delete this idea and everything in it?</span>{" "}
                <button className="link" onClick={deleteIdea} style={{ color: "var(--neg)" }}>yes</button>{" "}
                <button className="link" onClick={() => setConfirmDelete(false)}>cancel</button>
              </span>
            ) : (
              <button type="button" className="link" style={{ fontSize: 12 }} onClick={() => setConfirmDelete(true)}>
                delete idea
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// --- main panel: one month's research ----------------------------------------
function PeriodPanel({ periodId, onChanged, onDeleted }) {
  const { data, loading, error, reload } = useApi(() => api.getResearchPeriod(periodId), [periodId]);
  const period = data?.period;
  const [confirmDelete, setConfirmDelete] = useState(false);

  if (loading && !period) return <Loading label="Loading month…" />;
  if (error) return <ErrorBanner error={error} onRetry={reload} />;
  if (!period) return null;

  async function refresh() {
    await reload();
    onChanged?.(); // row_count in the sidebar tree can change
  }

  async function deleteMonth() {
    onDeleted?.(); // clear the selection FIRST — this panel is about to be gone
    await api.deleteResearchPeriod(period.id).catch(() => {});
    onChanged?.();
  }

  return (
    <>
      <div className="card">
        <div className="card-title">
          <span>{MONTH_NAMES[period.month - 1]} {period.year}</span>
          {confirmDelete ? (
            <span>
              <span className="muted" style={{ fontSize: 13, marginRight: 6 }}>delete this month?</span>
              <button className="link" onClick={deleteMonth} style={{ color: "var(--neg)" }}>yes, delete</button>
              <button className="link" onClick={() => setConfirmDelete(false)} style={{ marginLeft: 8 }}>cancel</button>
            </span>
          ) : (
            <button className="link" onClick={() => setConfirmDelete(true)}>delete this month</button>
          )}
        </div>
        <NotesAndCount period={period} onChanged={refresh} />
      </div>

      <Checklist period={period} onChanged={refresh} />
      <AttachRun periodId={period.id} onChanged={refresh} />
      <RowsTable period={period} onChanged={refresh} />
    </>
  );
}

// Notes + total count, saved on blur (not per keystroke) — same "type, then it
// saves quietly" feel as the trade journal's review notes.
function NotesAndCount({ period, onChanged }) {
  const [notes, setNotes] = useState(period.notes);
  const [totalCount, setTotalCount] = useState(period.total_count ?? "");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setNotes(period.notes);
    setTotalCount(period.total_count ?? "");
  }, [period.id]); // eslint-disable-line react-hooks/exhaustive-deps

  async function save() {
    setSaving(true);
    try {
      await api.updateResearchPeriod(period.id, {
        notes,
        total_count: totalCount === "" ? null : Number(totalCount),
      });
      onChanged?.();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="row" style={{ alignItems: "start" }}>
      <label style={{ flex: 2 }}>
        Notes / remarks
        <textarea
          rows={3}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          onBlur={save}
          placeholder="How the idea did this month, what you'd change…"
        />
      </label>
      <label style={{ flex: 1 }}>
        Total count {saving && <span className="muted">saving…</span>}
        <input
          type="number"
          min="0"
          value={totalCount}
          onChange={(e) => setTotalCount(e.target.value)}
          onBlur={save}
          placeholder="e.g. 36"
        />
      </label>
    </div>
  );
}

function Checklist({ period, onChanged }) {
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState("");

  async function toggle(itemId, done) {
    await api.setResearchCheck(period.id, itemId, done);
    onChanged?.();
  }

  async function addItem(e) {
    e.preventDefault();
    if (!text.trim()) return;
    await api.addResearchChecklistItem(period.idea_id, text.trim());
    setText("");
    setAdding(false);
    onChanged?.();
  }

  async function removeItem(itemId) {
    await api.deleteResearchChecklistItem(itemId).catch(() => {});
    onChanged?.();
  }

  return (
    <div className="card">
      <div className="card-title">Checklist</div>
      {period.checklist.length === 0 && !adding && (
        <p className="muted" style={{ margin: "0 0 8px" }}>
          No checklist yet — items you add apply to every month under this idea.
        </p>
      )}
      {period.checklist.map((item) => (
        <label key={item.id} className="research-check-row">
          <input type="checkbox" checked={item.done} onChange={(e) => toggle(item.id, e.target.checked)} />
          <span className={item.done ? "muted" : ""} style={item.done ? { textDecoration: "line-through" } : undefined}>
            {item.text}
          </span>
          <button type="button" className="link" style={{ marginLeft: "auto", fontSize: 11.5 }} onClick={() => removeItem(item.id)}>
            remove
          </button>
        </label>
      ))}
      {adding ? (
        <form onSubmit={addItem} className="row" style={{ marginTop: 8 }}>
          <input autoFocus value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. Checked circuit-freeze count" />
          <button className="primary small" disabled={!text.trim()} style={{ marginTop: 0 }}>Add</button>
        </form>
      ) : (
        <button type="button" className="link" style={{ fontSize: 12.5, marginTop: 4 }} onClick={() => setAdding(true)}>
          + add checklist item
        </button>
      )}
    </div>
  );
}

// Pull a past Backtest run's touches straight in as rows — the "not typing"
// path. Runs list is fetched once and reused; attaching copies rows in, it
// never keeps a live link, so a later re-scan can't silently change this
// month's numbers underneath you.
function AttachRun({ periodId, onChanged }) {
  const { data } = useApi(() => api.listBandStudyRuns(), []);
  const runs = data?.runs ?? [];
  const [runId, setRunId] = useState("");
  const [attaching, setAttaching] = useState(false);
  const [result, setResult] = useState(null);

  async function attach() {
    if (!runId) return;
    setAttaching(true);
    setResult(null);
    try {
      const d = await api.attachResearchRun(periodId, runId);
      setResult(`Added ${d.added} row${d.added === 1 ? "" : "s"} from that run.`);
      onChanged?.();
    } finally {
      setAttaching(false);
    }
  }

  return (
    <div className="card">
      <div className="card-title">Attach a Backtest run</div>
      <p className="muted" style={{ margin: "0 0 10px", fontSize: 13 }}>
        Pull a scan's stock list straight in, instead of retyping it — you can still edit or remove rows after.
      </p>
      <div className="row" style={{ alignItems: "end" }}>
        <label style={{ flex: 1 }}>
          Run
          <select value={runId} onChange={(e) => setRunId(e.target.value)}>
            <option value="">Choose a run…</option>
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                {r.start_date === r.end_date ? r.start_date : `${r.start_date} → ${r.end_date}`} ({r.total_touches} touches)
              </option>
            ))}
          </select>
        </label>
        <button className="primary" disabled={!runId || attaching} onClick={attach}>
          {attaching ? "Attaching…" : "Attach"}
        </button>
      </div>
      {result && <p className="muted" style={{ marginTop: 8, marginBottom: 0 }}>{result}</p>}
    </div>
  );
}

function RowsTable({ period, onChanged }) {
  return (
    <div className="card">
      <div className="card-title">Stocks</div>
      {period.rows.length === 0 ? (
        <p className="muted">No stocks yet — add one below, or attach a run above.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Symbol</th><th className="num">%</th><th>Remark</th><th>Source</th><th></th>
            </tr>
          </thead>
          <tbody>
            {period.rows.map((row) => (
              <RowLine key={row.id} row={row} onChanged={onChanged} />
            ))}
          </tbody>
        </table>
      )}
      <AddRowForm periodId={period.id} onChanged={onChanged} />
    </div>
  );
}

function RowLine({ row, onChanged }) {
  const [pctVal, setPctVal] = useState(row.pct ?? "");
  const [remark, setRemark] = useState(row.remark);

  async function saveField(patch) {
    await api.updateResearchRow(row.id, patch);
    onChanged?.();
  }

  async function remove() {
    await api.deleteResearchRow(row.id).catch(() => {});
    onChanged?.();
  }

  return (
    <tr>
      <td><strong>{row.symbol}</strong>{row.touch_date && <div className="muted" style={{ fontSize: 11.5 }}>{row.touch_date}</div>}</td>
      <td className="num">
        <input
          type="number"
          step="0.01"
          value={pctVal}
          onChange={(e) => setPctVal(e.target.value)}
          onBlur={() => saveField({ pct: pctVal === "" ? null : Number(pctVal) })}
          style={{ width: 80, textAlign: "right" }}
        />
      </td>
      <td>
        <input
          value={remark}
          onChange={(e) => setRemark(e.target.value)}
          onBlur={() => saveField({ remark })}
          placeholder="remark…"
        />
      </td>
      <td className="muted" style={{ fontSize: 12 }}>{row.source_run_id ? `run #${row.source_run_id}` : "typed"}</td>
      <td><button type="button" className="link" onClick={remove}>remove</button></td>
    </tr>
  );
}

function AddRowForm({ periodId, onChanged }) {
  const [symbol, setSymbol] = useState("");
  const [pctVal, setPctVal] = useState("");
  const [remark, setRemark] = useState("");
  const [adding, setAdding] = useState(false);

  async function submit(e) {
    e.preventDefault();
    if (!symbol.trim()) return;
    setAdding(true);
    try {
      await api.addResearchRow(periodId, {
        symbol: symbol.trim(),
        pct: pctVal === "" ? null : Number(pctVal),
        remark,
      });
      setSymbol(""); setPctVal(""); setRemark("");
      onChanged?.();
    } finally {
      setAdding(false);
    }
  }

  return (
    <form onSubmit={submit} className="row" style={{ marginTop: 10, alignItems: "end" }}>
      <label style={{ flex: 1 }}>
        Symbol
        <input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} placeholder="e.g. OMNI" />
      </label>
      <label style={{ width: 90 }}>
        %
        <input type="number" step="0.01" value={pctVal} onChange={(e) => setPctVal(e.target.value)} />
      </label>
      <label style={{ flex: 2 }}>
        Remark
        <input value={remark} onChange={(e) => setRemark(e.target.value)} placeholder="optional" />
      </label>
      <button className="primary" disabled={!symbol.trim() || adding} style={{ marginTop: 0 }}>
        {adding ? "Adding…" : "Add row"}
      </button>
    </form>
  );
}
