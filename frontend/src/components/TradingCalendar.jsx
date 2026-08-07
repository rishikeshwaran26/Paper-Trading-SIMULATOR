import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const WEEKDAY_LETTERS = ["S", "M", "T", "W", "T", "F", "S"];

// Trading-day calendar for the nav — circled days are confirmed NSE trading
// days, everything else (weekend, holiday, or not-yet-happened) sits plain
// and light so the circle means one thing: the market was actually open.
//
// Backed by the bhavcopy cache every scan already fills, so a month already
// touched anywhere in the app renders instantly; a month that hasn't costs
// one request per not-yet-cached day, the first time only.
export default function TradingCalendar() {
  const today = new Date();
  // year and month as ONE number (Jan year 0 = 0), not two separate pieces of
  // state — two setters updated together from stale closure values only
  // apply the last of several rapid clicks (React batches them against the
  // same pre-click snapshot), so double-clicking "next" silently moved one
  // month instead of two. A single functional update carries correctly no
  // matter how many clicks land before a re-render.
  const [monthIndex, setMonthIndex] = useState(today.getFullYear() * 12 + today.getMonth());
  const year = Math.floor(monthIndex / 12);
  const month = (monthIndex % 12) + 1; // 1-12

  const [days, setDays] = useState(null);
  const [loading, setLoading] = useState(true);
  // Keyed by "YYYY-M" — a month already fetched this session never asks the
  // server again, so paging back and forth is instant after the first visit.
  const cache = useRef({});

  useEffect(() => {
    const key = `${year}-${month}`;
    if (cache.current[key]) {
      setDays(cache.current[key]);
      setLoading(false);
      return;
    }
    let alive = true;
    setLoading(true);
    api
      .calendarMonth(year, month)
      .then((d) => {
        if (!alive) return;
        cache.current[key] = d.days;
        setDays(d.days);
      })
      .catch(() => alive && setDays(null))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [year, month]);

  const shift = (delta) => setMonthIndex((i) => i + delta);

  // Leading blanks so day 1 lands under its real weekday column.
  const firstWeekday = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
  const cells = days ? [...Array(firstWeekday).fill(null), ...days] : [];

  return (
    <div className="trading-calendar">
      <div className="trading-calendar-head">
        <button type="button" onClick={() => shift(-1)} aria-label="Previous month">‹</button>
        <span>{MONTH_NAMES[month - 1]} {year}</span>
        <button type="button" onClick={() => shift(1)} aria-label="Next month">›</button>
      </div>
      <div className="trading-calendar-grid">
        {WEEKDAY_LETTERS.map((l, i) => (
          <span key={i} className="trading-calendar-dow">{l}</span>
        ))}
        {loading && !days
          ? <span className="trading-calendar-loading">loading…</span>
          : cells.map((d, i) =>
              d == null ? (
                <span key={i} />
              ) : (
                <span
                  key={d.date}
                  className={`trading-calendar-day ${d.status === "trading" ? "on" : ""}`}
                  title={d.status === "trading" ? `${d.date} — NSE trading day`
                       : d.status === "future" ? `${d.date} — hasn't happened yet`
                       : `${d.date} — weekend or holiday`}
                >
                  {Number(d.date.slice(-2))}
                </span>
              )
            )}
      </div>
    </div>
  );
}
