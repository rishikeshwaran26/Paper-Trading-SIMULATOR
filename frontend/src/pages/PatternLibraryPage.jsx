// Pattern Library page — catalog of discovered and saved spike patterns.
//
// Shows patterns captured from the Backtest report, with their historical
// win rates, sample sizes, and active filter criteria.

export default function PatternLibraryPage() {
  return (
    <div className="page">
      <h1>Pattern Library</h1>
      <p className="muted" style={{ marginTop: -12, marginBottom: 20 }}>
        Saved spike patterns and backtested reversion rules.
      </p>

      <div className="card">
        <p className="muted" style={{ margin: 0 }}>
          No saved patterns yet.
        </p>
      </div>
    </div>
  );
}
