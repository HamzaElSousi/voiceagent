import { useState } from "react";
import { api, type Agent, type Campaign, type Connector } from "../api";
import { useSession, useWs } from "../session";
import { Badge, Empty, ErrorNote, Field, Modal, PageHeader, fmtMoney, useLoad } from "../components/ui";

const STATUS_TONE: Record<string, "green" | "amber" | "gray" | "blue"> = { running: "green", paused: "amber", done: "gray", draft: "blue" };

export default function Campaigns() {
  const { workspace } = useSession();
  const ws = useWs();
  const [open, setOpen] = useState(false);
  const { data, error, reload, setError } = useLoad(async () => {
    const [campaigns, agents, connectors] = await Promise.all([
      api.get<Campaign[]>(ws("/campaigns")),
      api.get<Agent[]>(ws("/agents")),
      api.get<Connector[]>(ws("/connectors")),
    ]);
    return { campaigns, agents, connectors };
  }, [workspace?.id]);

  const act = async (c: Campaign, action: "start" | "pause") => {
    try {
      await api.post(ws(`/campaigns/${c.id}/${action}`));
      reload();
    } catch (e: any) {
      setError(e.message);
    }
  };
  const agentName = (id: string) => data?.agents.find((a) => a.id === id)?.name ?? "deleted agent";

  return (
    <>
      <PageHeader title="Campaigns" subtitle="An outbound agent works through your consented leads, inside each lead's local calling hours."
        actions={<button className="btn-primary" onClick={() => setOpen(true)} disabled={!data?.agents.some((a) => a.mode === "outbound")}>New campaign</button>} />
      <ErrorNote error={error} />
      {data && data.campaigns.length === 0 && <Empty title="No campaigns yet">Create an outbound agent first, then start a campaign with a budget.</Empty>}
      <div className="space-y-4">
        {data?.campaigns.map((c) => (
          <div key={c.id} className="card">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-semibold">{c.name}</span>
                  <Badge tone={STATUS_TONE[c.status] ?? "gray"}>{c.status}</Badge>
                </div>
                <p className="mt-1 text-sm text-slate-500">{agentName(c.agent_id)} · up to {c.concurrency} at once · {c.max_attempts} tries per lead</p>
                {c.status_reason && <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">{c.status_reason}</p>}
              </div>
              <div className="flex gap-2">
                {c.status === "running" ? (
                  <button className="btn-secondary" onClick={() => act(c, "pause")}>Pause</button>
                ) : (
                  <button className="btn-primary" onClick={() => act(c, "start")}>{c.status === "draft" ? "Start" : "Resume"}</button>
                )}
              </div>
            </div>
            <div className="mt-4">
              <div className="mb-1 flex justify-between text-xs text-slate-500">
                <span>{fmtMoney(c.spent_usd)} of {fmtMoney(c.budget_usd)}</span>
                <span>{c.calls} calls</span>
              </div>
              <div className="h-2 rounded-full bg-slate-100 dark:bg-slate-800">
                <div className="h-2 rounded-full bg-brand-500" style={{ width: `${Math.min(100, (c.spent_usd / c.budget_usd) * 100)}%` }} />
              </div>
              <div className="mt-3 flex flex-wrap gap-2 text-xs">
                {Object.entries(c.outcomes).map(([k, v]) => <Badge key={k}>{k.replace(/_/g, " ")}: {v}</Badge>)}
              </div>
            </div>
          </div>
        ))}
      </div>
      {data && <NewCampaign open={open} onClose={() => setOpen(false)} agents={data.agents} connectors={data.connectors} onDone={reload} />}
    </>
  );
}

function NewCampaign({ open, onClose, agents, connectors, onDone }: { open: boolean; onClose: () => void; agents: Agent[]; connectors: Connector[]; onDone: () => void }) {
  const ws = useWs();
  const outbound = agents.filter((a) => a.mode === "outbound");
  const [form, setForm] = useState({ name: "", agent_id: "", connector_id: "", budget_usd: 25, concurrency: 1, max_attempts: 3, retry_delay_minutes: 240 });
  const [error, setError] = useState<string | null>(null);
  const go = async (start: boolean) => {
    try {
      const c = await api.post<Campaign>(ws("/campaigns"), {
        ...form, agent_id: form.agent_id || outbound[0]?.id, connector_id: form.connector_id || null,
      });
      if (start) await api.post(ws(`/campaigns/${c.id}/start`));
      onDone();
      onClose();
    } catch (e: any) {
      setError(e.message);
    }
  };
  const num = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: Number(e.target.value) });
  return (
    <Modal open={open} onClose={onClose} title="New campaign">
      <ErrorNote error={error} />
      <div className="space-y-4">
        <Field label="Name"><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="October callbacks" /></Field>
        <Field label="Agent">
          <select className="input" value={form.agent_id} onChange={(e) => setForm({ ...form, agent_id: e.target.value })}>
            {outbound.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </Field>
        <Field label="Leads from" hint="Leave on all leads, or limit the campaign to one connector.">
          <select className="input" value={form.connector_id} onChange={(e) => setForm({ ...form, connector_id: e.target.value })}>
            <option value="">All leads in this workspace</option>
            {connectors.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Budget (USD)"><input className="input" type="number" min={1} value={form.budget_usd} onChange={num("budget_usd")} /></Field>
          <Field label="Calls at once"><input className="input" type="number" min={1} max={20} value={form.concurrency} onChange={num("concurrency")} /></Field>
          <Field label="Tries per lead"><input className="input" type="number" min={1} max={10} value={form.max_attempts} onChange={num("max_attempts")} /></Field>
          <Field label="Retry after (minutes)"><input className="input" type="number" min={10} value={form.retry_delay_minutes} onChange={num("retry_delay_minutes")} /></Field>
        </div>
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <button className="btn-secondary" onClick={() => go(false)} disabled={!form.name}>Save as draft</button>
        <button className="btn-primary" onClick={() => go(true)} disabled={!form.name}>Create and start</button>
      </div>
    </Modal>
  );
}
