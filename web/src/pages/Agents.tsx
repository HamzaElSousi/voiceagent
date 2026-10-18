import { Link } from "react-router-dom";
import { api, type Agent } from "../api";
import { useSession, useWs } from "../session";
import { Badge, Empty, ErrorNote, PageHeader, fmtDate, useLoad } from "../components/ui";

export default function Agents() {
  const { workspace } = useSession();
  const ws = useWs();
  const { data, error } = useLoad(() => api.get<Agent[]>(ws("/agents")), [workspace?.id]);
  return (
    <>
      <PageHeader title="Agents" subtitle="Each agent is a persona, a script and its booking and compliance rules."
        actions={<Link className="btn-primary" to="/agents/new">New agent</Link>} />
      <ErrorNote error={error} />
      {data && data.length === 0 && (
        <Empty title="No agents yet">Start from the outbound (sales) or inbound (receptionist) template.</Empty>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        {data?.map((a) => (
          <Link key={a.id} to={`/agents/${a.id}`} className="card block hover:border-brand-500">
            <div className="flex items-center justify-between">
              <span className="font-semibold">{a.name}</span>
              <Badge tone={a.mode === "inbound" ? "blue" : "green"}>{a.mode}</Badge>
            </div>
            <p className="mt-2 line-clamp-2 text-sm text-slate-500 dark:text-slate-400">{a.config?.script?.greeting}</p>
            <p className="mt-3 text-xs text-slate-400">
              {a.config?.booking?.provider === "calcom" ? "Books on Cal.com" : "Logs booking intent"} · updated {fmtDate(a.updated_at)}
              {!a.is_active && " · inactive"}
            </p>
          </Link>
        ))}
      </div>
    </>
  );
}
