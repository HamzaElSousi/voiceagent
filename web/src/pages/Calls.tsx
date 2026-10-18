import { Link } from "react-router-dom";
import { api, type CallSummary } from "../api";
import { useSession, useWs } from "../session";
import { Badge, Empty, ErrorNote, OutcomeBadge, PageHeader, fmtDate, fmtMoney, useLoad } from "../components/ui";

export default function Calls() {
  const { workspace } = useSession();
  const ws = useWs();
  const { data, error } = useLoad(() => api.get<CallSummary[]>(ws("/calls?limit=200")), [workspace?.id]);
  return (
    <>
      <PageHeader title="Calls" subtitle="Every call with its outcome, cost, latency and transcript." />
      <ErrorNote error={error} />
      {data && data.length === 0 ? (
        <Empty title="No calls yet">Try "Call me now" on an agent to make your first one.</Empty>
      ) : (
        <div className="card overflow-x-auto p-0">
          <table className="w-full min-w-[680px]">
            <thead><tr><th className="th">Contact</th><th className="th">Direction</th><th className="th">Outcome</th><th className="th">Length</th><th className="th">Cost</th><th className="th">When</th></tr></thead>
            <tbody>
              {data?.map((c) => (
                <tr key={c.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/50">
                  <td className="td"><Link className="font-medium hover:underline" to={`/calls/${c.id}`}>{c.contact_name || "Unknown"}</Link><div className="font-mono text-xs text-slate-500">{c.contact_phone}</div></td>
                  <td className="td"><Badge>{c.direction}</Badge></td>
                  <td className="td"><OutcomeBadge outcome={c.outcome} />{c.booking_ref && <span className="ml-1 text-xs text-emerald-600">booked</span>}</td>
                  <td className="td text-sm">{Math.round(c.duration_seconds)}s</td>
                  <td className="td text-sm">{fmtMoney(c.cost_usd)}</td>
                  <td className="td text-xs text-slate-500">{fmtDate(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
