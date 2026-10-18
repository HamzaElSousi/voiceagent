import { useNavigate, useParams } from "react-router-dom";
import { api, type CallDetail as Detail } from "../api";
import { useWs } from "../session";
import { ErrorNote, OutcomeBadge, PageHeader, fmtDate, fmtMoney, useLoad } from "../components/ui";

export default function CallDetail() {
  const { callId } = useParams();
  const ws = useWs();
  const nav = useNavigate();
  const { data: c, error } = useLoad(() => api.get<Detail>(ws(`/calls/${callId}`)), [callId]);
  const remove = async () => {
    if (!window.confirm("Delete this call and its transcript? This cannot be undone.")) return;
    await api.del(ws(`/calls/${callId}`));
    nav("/calls");
  };
  if (!c) return <ErrorNote error={error} />;
  return (
    <>
      <PageHeader title={c.contact_name || c.contact_phone || "Call"} subtitle={`${c.direction} call · ${fmtDate(c.created_at)}`}
        actions={<button className="btn-secondary" onClick={remove}>Delete call</button>} />
      <div className="mb-6 grid gap-4 sm:grid-cols-4">
        <Info label="Outcome"><OutcomeBadge outcome={c.outcome} /></Info>
        <Info label="Length">{Math.round(c.duration_seconds)}s</Info>
        <Info label="Cost">{fmtMoney(c.cost_usd)}</Info>
        <Info label="Response time (p50)">{c.latency_p50_ms ? `${Math.round(c.latency_p50_ms)} ms` : "n/a"}</Info>
      </div>
      {c.booking_ref && (
        <div className="card mb-6 border-emerald-200 dark:border-emerald-900">
          <span className="font-medium text-emerald-700 dark:text-emerald-300">Meeting booked</span>
          <span className="ml-2 text-sm text-slate-500">{c.booked_slot && fmtDate(c.booked_slot)} · ref {c.booking_ref}</span>
        </div>
      )}
      <p className="mb-2 text-sm text-slate-500">{c.end_reason}</p>
      {c.error && <ErrorNote error={c.error} />}
      <section className="card space-y-3">
        <h2 className="font-semibold">Transcript</h2>
        {c.transcript.length === 0 && <p className="text-sm text-slate-500">No conversation (the call did not connect).</p>}
        {c.transcript.map((t, i) => (
          <div key={i} className={`flex ${t.speaker === "agent" ? "justify-start" : "justify-end"}`}>
            <div className={`max-w-[80%] rounded-2xl px-3.5 py-2 text-sm ${t.speaker === "agent" ? "bg-slate-100 dark:bg-slate-800" : "bg-brand-500 text-white"}`}>
              {t.text}
            </div>
          </div>
        ))}
      </section>
    </>
  );
}

function Info({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="card">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-1 font-semibold">{children}</div>
    </div>
  );
}
