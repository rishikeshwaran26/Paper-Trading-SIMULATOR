import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import Layout from "./components/Layout.jsx";
import { Loading } from "./components/Status.jsx";
import DashboardPage from "./pages/DashboardPage.jsx";
import StrategyDetailPage from "./pages/StrategyDetailPage.jsx";
import JournalPage from "./pages/JournalPage.jsx";
import AnalyticsPage from "./pages/AnalyticsPage.jsx";
import WatchlistPage from "./pages/WatchlistPage.jsx";
import ScreenerPage from "./pages/ScreenerPage.jsx";
import BacktestPage from "./pages/BacktestPage.jsx";
import StockChartPage from "./pages/StockChartPage.jsx";
import LoginPage from "./pages/LoginPage.jsx";
import { api } from "./api.js";
import { getToken, clearToken } from "./auth.js";

// App owns the ONE piece of truly global state: who is logged in. Everything
// else is fetched per-page. Auth belongs here because it gates the entire route
// table — it's the only state genuinely shared by every screen.
export default function App() {
  const [user, setUser] = useState(null);
  const [checking, setChecking] = useState(true);

  // On boot, if we have a stored token, verify it's still valid before showing
  // the app. Otherwise every page would flash and then 401.
  useEffect(() => {
    if (!getToken()) {
      setChecking(false);
      return;
    }
    api
      .me()
      .then((d) => setUser(d.user))
      .catch(() => clearToken())
      .finally(() => setChecking(false));
  }, []);

  // api.js fires this event whenever any request comes back 401, so an expired
  // token anywhere in the app bounces us back to the login screen.
  useEffect(() => {
    const onUnauthorized = () => setUser(null);
    window.addEventListener("papertrading:unauthorized", onUnauthorized);
    return () => window.removeEventListener("papertrading:unauthorized", onUnauthorized);
  }, []);

  function logout() {
    clearToken();
    setUser(null);
  }

  if (checking) return <Loading label="Starting up…" />;
  if (!user) return <LoginPage onAuthed={setUser} />;

  return (
    <BrowserRouter>
      <Layout user={user} onLogout={logout}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/strategies/:name" element={<StrategyDetailPage />} />
          <Route path="/strategies/:name/journal" element={<JournalPage />} />
          <Route path="/strategies/:name/analytics" element={<AnalyticsPage />} />
          <Route path="/watchlist" element={<WatchlistPage />} />
          <Route path="/screener" element={<ScreenerPage />} />
          <Route path="/backtest" element={<BacktestPage />} />
          <Route path="/backtest/chart/:symbol" element={<StockChartPage />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  );
}
