import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, BASE, setUnauthorizedHandler, User } from "./api";
import { ThemeProvider } from "./theme";
import { Layout } from "./components/Layout";
import { Loading } from "./components/ui";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import SitePage from "./pages/Site";
import Wan from "./pages/Wan";
import S2S from "./pages/S2S";
import Devices from "./pages/Devices";
import DeviceDetail from "./pages/DeviceDetail";
import Clients from "./pages/Clients";
import ClientDetail from "./pages/ClientDetail";
import Events from "./pages/Events";
import Alerts from "./pages/Alerts";
import Topology from "./pages/Topology";
import Diagnostics from "./pages/Diagnostics";
import Users from "./pages/Users";
import MonitoringSites from "./pages/MonitoringSites";
import { ConnectionSetup, CreateOwner, SetupWizard } from "./pages/Setup";
import Operations from "./pages/Operations";
import Wireless from "./pages/Wireless";
import Search from "./pages/Search";
import Notifications from "./pages/Notifications";
import Maintenance from "./pages/Maintenance";
import History from "./pages/History";
import Account from "./pages/Account";
import Wallboard from "./pages/Wallboard";
import "./styles.css";

document.documentElement.dataset.theme = localStorage.getItem("noc-theme") || "dark";

const qc = new QueryClient({
  defaultOptions: {
    queries: { retry: (n, e) => !(e instanceof ApiError && (e.status === 401 || e.status === 404)) && n < 2, refetchOnWindowFocus: true, staleTime: 5000 },
  },
});

function App() {
  const client = useQueryClient();
  const setup = useQuery({ queryKey: ['setup-status'], queryFn: () => api<{ needs_owner: boolean; ui_setup: boolean }>('/setup/status'), retry: false });
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<User>("/auth/me"), retry: false, staleTime: 60000 });
  const connection = useQuery({ queryKey: ['setup-connection'], queryFn: () => api<any>('/setup/connection'), enabled: !!me.data?.is_owner && !!setup.data?.ui_setup, refetchInterval: 2000 });
  const [, force] = useState(0);
  useEffect(() => {
    setUnauthorizedHandler(() => {
      client.setQueryData(["me"], null);
      force((x) => x + 1);
    });
  }, [client]);
  useEffect(() => {
    if (connection.data?.complete) {
      client.invalidateQueries({ queryKey: ['sites'] });
      client.invalidateQueries({ queryKey: ['overview'] });
    }
  }, [connection.data?.complete, client]);
  if (me.isLoading || setup.isLoading) return <Loading />;
  if (setup.data?.needs_owner) return <CreateOwner onCreated={() => client.invalidateQueries()} />;
  if (!me.data) return <Login onLogin={() => me.refetch()} />;
  if (setup.data?.ui_setup && me.data.is_owner && connection.isLoading) return <Loading />;
  if (setup.data?.ui_setup && me.data.is_owner && connection.data && !connection.data.complete) return <SetupWizard onComplete={() => client.invalidateQueries()} />;
  return (
    <Layout user={me.data}>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/operations" element={<Operations />} />
        <Route path="/wallboard" element={<Wallboard />} />
        <Route path="/wireless" element={<Wireless />} />
        <Route path="/search" element={<Search />} />
        <Route path="/maintenance" element={<Maintenance user={me.data} />} />
        <Route path="/history" element={<Navigate to="/availability" replace />} />
        <Route path="/availability" element={<History view="availability" />} />
        <Route path="/changes" element={<History view="changes" />} />
        <Route path="/account" element={<Account />} />
        <Route path="/settings" element={<Navigate to="/account" replace />} />
        <Route path="/notifications" element={me.data.is_owner ? <Notifications /> : <div className="empty">Dashboard owner access required</div>} />
        <Route path="/site/:slug" element={<SitePage />} />
        <Route path="/wan" element={<Wan />} />
        <Route path="/s2s" element={<S2S />} />
        <Route path="/devices/:type" element={<Devices />} />
        <Route path="/device/:mac" element={<DeviceDetail />} />
        <Route path="/clients" element={<Clients />} />
        <Route path="/client/:site/:mac" element={<ClientDetail />} />
        <Route path="/events" element={<Events />} />
        <Route path="/alerts" element={<Alerts isAdmin={me.data.role === "admin"} isOwner={me.data.is_owner} user={me.data} />} />
        <Route path="/topology" element={<Topology />} />
        <Route path="/diagnostics" element={me.data.is_owner ? <Diagnostics isAdmin /> : <div className="empty">Dashboard owner access required</div>} />
        <Route path="/users" element={me.data.role === "admin" ? <Users user={me.data} /> : <div className="empty">Site administrator access required</div>} />
        <Route path="/monitoring" element={me.data.is_owner ? <MonitoringSites /> : <div className="empty">Dashboard owner access required</div>} />
        <Route path="/connection" element={me.data.is_owner ? <ConnectionSetup /> : <div className="empty">Dashboard owner access required</div>} />
        <Route path="*" element={<div className="empty">Page not found</div>} />
      </Routes>
    </Layout>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <RouterProvider router={createBrowserRouter([{ path: "*", element: <App /> }], { basename: BASE })} />
      </ThemeProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
