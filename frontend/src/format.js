// Small display helpers, kept out of components so formatting is consistent
// everywhere and easy to change in one place.

// Indian number grouping (lakh/crore) via the en-IN locale: 1000000 -> 10,00,000.
export const rupees = (n) =>
  "₹" + Number(n || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// P&L values get an explicit + sign so gains and losses read at a glance.
export const pnl = (n) => (Number(n) >= 0 ? "+" : "") + rupees(n);

export const pct = (n) => (Number(n) >= 0 ? "+" : "") + Number(n || 0).toFixed(2) + "%";

// Returns a CSS class name for coloring positive/negative numbers.
export const signClass = (n) => (Number(n) > 0 ? "pos" : Number(n) < 0 ? "neg" : "");

// ISO timestamp -> "2026-07-20 09:13"
export const when = (iso) => (iso ? iso.slice(0, 16).replace("T", " ") : "");

// Calculate how old a company was as of a given date (e.g. touch date):
// returns "2m old", "5y 2m old", "28y old", or "12d old".
export function companyAge(listingDateStr, asOfDateStr) {
  if (!listingDateStr) return null;
  const listDate = new Date(listingDateStr);
  const asOfDate = asOfDateStr ? new Date(asOfDateStr) : new Date();
  if (isNaN(listDate.getTime()) || isNaN(asOfDate.getTime())) return null;
  if (asOfDate < listDate) return "New listing";

  let years = asOfDate.getUTCFullYear() - listDate.getUTCFullYear();
  let months = asOfDate.getUTCMonth() - listDate.getUTCMonth();
  let days = asOfDate.getUTCDate() - listDate.getUTCDate();

  if (days < 0) {
    months -= 1;
  }
  if (months < 0) {
    years -= 1;
    months += 12;
  }

  if (years === 0 && months === 0) {
    const diffMs = asOfDate.getTime() - listDate.getTime();
    const diffDays = Math.max(0, Math.floor(diffMs / (1000 * 60 * 60 * 24)));
    return diffDays <= 1 ? "1d old" : `${diffDays}d old`;
  }
  if (years === 0) {
    return `${months}m old`;
  }
  if (years < 5 && months > 0) {
    return `${years}y ${months}m old`;
  }
  return `${years}y old`;
}

export function listingAgeTitle(listingDateStr, asOfDateStr) {
  if (!listingDateStr) return "";
  const age = companyAge(listingDateStr, asOfDateStr);
  return `Listed on ${listingDateStr}${age ? ` · ${age} as of touch date` : ""}`;
}

