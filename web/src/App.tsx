import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { SessionProvider, useSession } from "./session";
import AuthPage from "./pages/Auth";
import Overview from "./pages/Overview";
import Agents from "./pages/Agents";
import AgentEditor from "./pages/AgentEditor";
import Leads from "./pages/Leads";
import Campaigns from "./pages/Campaigns";
import Calls from "./pages/Calls";
import CallDetail from "./pages/CallDetail";
import Connectors from "./pages/Connectors";
import Settings from "./pages/Settings";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/agents", label: "Agents" },
  { to: "/leads", label: "Leads" },
  { to: "/campaigns", label: "Campaigns" },
  { to: "/calls", label: "Calls" },
  { to: "/connectors", label: "Connectors" },
  { to: "/settings", label: "Settings" },
];

function Shell() {
  const { me, workspace, setWorkspaceId, logout } = useSession();
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="border-b border-slate-200 bg-white md:w-60 md:shrink-0 md:border-b-0 md:border-r dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-center gap-2 px-5 py-4">
          <img src="/favicon.svg" alt="" className="h-7 w-7" />
          <span className="text-lg font-semibold">VoiceAgent</span>
        </div>
        {me && me.workspaces.length > 1 && (
          <div className="px-4 pb-3">
            <select className="input" value={workspace?.id} onChange={(e) => setWorkspaceId(e.target.value)} aria-label="Workspace">
              {me.workspaces.map((w) => (
                <option key={w.id} value={w.id}>{w.name}</option>
              ))}
            </select>
          </div>
        )}
        <nav className="flex gap-1 overflow-x-auto px-3 pb-3 md:flex-col md:pb-0">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `whitespace-nowrap rounded-lg px-3 py-2 text-sm font-medium ${
                  isActive ? "bg-brand-50 text-brand-700 dark:bg-slate-800 dark:text-white" : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
                }`
              }
            >
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="hidden px-5 py-4 text-xs text-slate-500 md:block">
          <div className="truncate">{me?.user.email}</div>
          <button className="mt-1 text-brand-600 hover:underline" onClick={logout}>Sign out</button>
        </div>
      </aside>
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 md:px-8 md:py-8">
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/agents/new" element={<AgentEditor />} />
          <Route path="/agents/:agentId" element={<AgentEditor />} />
          <Route path="/leads" element={<Leads />} />
          <Route path="/campaigns" element={<Campaigns />} />
          <Route path="/calls" element={<Calls />} />
          <Route path="/calls/:callId" element={<CallDetail />} />
          <Route path="/connectors" element={<Connectors />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function Gate() {
  const { me, loading } = useSession();
  if (loading) return <div className="p-10 text-center text-sm text-slate-500">Loading…</div>;
  if (!me) return <AuthPage />;
  if (!me.workspaces.length) return <div className="p-10 text-center">You are not a member of any workspace.</div>;
  return <Shell />;
}

export default function App() {
  return (
    <SessionProvider>
      <Gate />
    </SessionProvider>
  );
}
