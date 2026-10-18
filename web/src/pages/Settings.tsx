import { useState } from "react";
import { api, type Agent, type DncEntry, type KeyInfo, type PhoneNumber, type Workspace } from "../api";
import { useSession, useWs } from "../session";
import { Badge, ErrorNote, Field, PageHeader, fmtDate, useLoad } from "../components/ui";

export default function Settings() {
  return (
    <>
      <PageHeader title="Settings" subtitle="Provider keys, phone numbers, the do-not-call list and workspace settings." />
      <div className="space-y-6">
        <Keys />
        <Numbers />
        <Dnc />
        <WorkspaceSettings />
      </div>
    </>
  );
}

const KEY_HELP: Record<string, string> = {
  twilio_account_sid: "Twilio Console home page.",
  twilio_auth_token: "Twilio Console home page. Also used to verify Twilio's webhooks.",
  twilio_from_number: "A voice-capable number on your Twilio account, e.g. +12125550100.",
  deepgram_api_key: "Speech to text.",
  elevenlabs_api_key: "Text to speech.",
  anthropic_api_key: "Claude, the default LLM.",
  openrouter_api_key: "Optional: any other model via OpenRouter.",
  calcom_api_key: "Optional: book real slots during calls. Cal.com > Settings > Developer.",
};

function Keys() {
  const { workspace } = useSession();
  const ws = useWs();
  const { data, error, reload, setError } = useLoad(() => api.get<KeyInfo[]>(ws("/keys")), [workspace?.id]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const save = async (name: string) => {
    try {
      await api.put(ws(`/keys/${name}`), { value: drafts[name] });
      setDrafts({ ...drafts, [name]: "" });
      reload();
    } catch (e: any) {
      setError(e.message);
    }
  };
  const remove = async (name: string) => {
    if (!window.confirm("Remove this key? Calls that need it will stop working.")) return;
    await api.del(ws(`/keys/${name}`));
    reload();
  };
  return (
    <section className="card">
      <h2 className="font-semibold">Provider keys</h2>
      <p className="hint mb-4">Bring your own accounts: you pay each provider directly. Keys are encrypted at rest and never shown again after saving.</p>
      <ErrorNote error={error} />
      <div className="divide-y divide-slate-100 dark:divide-slate-800">
        {data?.map((k) => (
          <div key={k.name} className="grid gap-2 py-3 sm:grid-cols-[1fr_1.4fr] sm:items-center">
            <div>
              <div className="flex items-center gap-2 text-sm font-medium">
                {k.label} {k.is_set ? <Badge tone="green">{k.masked}</Badge> : <Badge>not set</Badge>}
              </div>
              <div className="hint">{KEY_HELP[k.name]}</div>
            </div>
            <div className="flex gap-2">
              <input className="input font-mono" type={k.name === "twilio_from_number" ? "text" : "password"} autoComplete="off"
                placeholder={k.is_set ? "Replace…" : "Paste value"} value={drafts[k.name] ?? ""}
                onChange={(e) => setDrafts({ ...drafts, [k.name]: e.target.value })} />
              <button className="btn-primary" disabled={!drafts[k.name]} onClick={() => save(k.name)}>Save</button>
              {k.is_set && <button className="btn-secondary" onClick={() => remove(k.name)} aria-label={`Remove ${k.label}`}>✕</button>}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function Numbers() {
  const { workspace } = useSession();
  const ws = useWs();
  const { data, error, reload, setError } = useLoad(async () => ({
    numbers: await api.get<PhoneNumber[]>(ws("/numbers")),
    agents: (await api.get<Agent[]>(ws("/agents"))).filter((a) => a.mode === "inbound"),
  }), [workspace?.id]);
  const [form, setForm] = useState({ e164: "", inbound_agent_id: "", configure_twilio: true });
  const add = async () => {
    try {
      await api.post(ws("/numbers"), { ...form, inbound_agent_id: form.inbound_agent_id || null });
      setForm({ ...form, e164: "" });
      reload();
    } catch (e: any) {
      setError(e.message);
    }
  };
  const agentName = (id: string | null) => data?.agents.find((a) => a.id === id)?.name ?? "first inbound agent";
  return (
    <section className="card">
      <h2 className="font-semibold">Phone numbers</h2>
      <p className="hint mb-4">Calls to these Twilio numbers are answered by an inbound agent.</p>
      <ErrorNote error={error} />
      <ul className="mb-4 space-y-2">
        {data?.numbers.map((n) => (
          <li key={n.id} className="flex items-center justify-between rounded-lg border border-slate-200 px-3 py-2 text-sm dark:border-slate-800">
            <span><span className="font-mono">{n.e164}</span> <span className="text-slate-500">→ {agentName(n.inbound_agent_id)}</span></span>
            <button className="text-rose-600 hover:underline" onClick={async () => { await api.del(ws(`/numbers/${n.id}`)); reload(); }}>Remove</button>
          </li>
        ))}
      </ul>
      <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
        <Field label="Twilio number"><input className="input" value={form.e164} onChange={(e) => setForm({ ...form, e164: e.target.value })} placeholder="+12125550100" /></Field>
        <Field label="Answered by">
          <select className="input" value={form.inbound_agent_id} onChange={(e) => setForm({ ...form, inbound_agent_id: e.target.value })}>
            <option value="">First inbound agent</option>
            {data?.agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        </Field>
        <button className="btn-primary" disabled={!form.e164} onClick={add}>Add</button>
      </div>
      <label className="mt-3 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={form.configure_twilio} onChange={(e) => setForm({ ...form, configure_twilio: e.target.checked })} />
        Point the number's Twilio webhook at this server automatically
      </label>
    </section>
  );
}

function Dnc() {
  const { workspace } = useSession();
  const ws = useWs();
  const { data, error, reload, setError } = useLoad(() => api.get<DncEntry[]>(ws("/dnc")), [workspace?.id]);
  const [phone, setPhone] = useState("");
  const add = async () => {
    try {
      await api.post(ws("/dnc"), { phone });
      setPhone("");
      reload();
    } catch (e: any) {
      setError(e.message);
    }
  };
  const remove = async (p: string) => {
    if (!window.confirm(`Remove ${p}? This allows calling someone who may have asked not to be called.`)) return;
    await api.del(ws(`/dnc/${encodeURIComponent(p)}`));
    reload();
  };
  return (
    <section className="card">
      <h2 className="font-semibold">Do-not-call list</h2>
      <p className="hint mb-4">Checked before every call. Anyone who says "remove me" on a call is added automatically.</p>
      <ErrorNote error={error} />
      <div className="mb-4 flex gap-2">
        <input className="input max-w-xs" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+12125550100" />
        <button className="btn-primary" disabled={!phone} onClick={add}>Add number</button>
      </div>
      {data && data.length > 0 && (
        <div className="max-h-72 overflow-y-auto">
          <table className="w-full">
            <tbody>
              {data.map((d) => (
                <tr key={d.phone}>
                  <td className="td font-mono text-xs">{d.phone}</td>
                  <td className="td"><Badge tone={d.reason === "opt_out" ? "red" : "gray"}>{d.reason.replace("_", " ")}</Badge></td>
                  <td className="td hidden text-xs text-slate-500 sm:table-cell">{fmtDate(d.created_at)}</td>
                  <td className="td text-right"><button className="text-sm text-slate-500 hover:underline" onClick={() => remove(d.phone)}>Remove</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function WorkspaceSettings() {
  const { workspace, refresh } = useSession();
  const ws = useWs();
  const { data, error, setError } = useLoad(() => api.get<Workspace>(ws("")), [workspace?.id]);
  const [form, setForm] = useState<{ name?: string; retention_days?: number }>({});
  const save = async () => {
    try {
      await api.patch(ws(""), form);
      await refresh();
      setForm({});
    } catch (e: any) {
      setError(e.message);
    }
  };
  if (!data) return null;
  return (
    <section className="card space-y-4">
      <h2 className="font-semibold">Workspace</h2>
      <ErrorNote error={error} />
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Name"><input className="input" value={form.name ?? data.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
        <Field label="Keep calls for (days)" hint="Older calls and transcripts are deleted automatically.">
          <input className="input" type="number" min={1} value={form.retention_days ?? data.retention_days}
            onChange={(e) => setForm({ ...form, retention_days: Number(e.target.value) })} />
        </Field>
      </div>
      {data.inbound_webhook_url && (
        <Field label="Inbound webhook URL" hint={'Twilio number > Voice > "A call comes in" (HTTP POST), if you set numbers up by hand.'}>
          <input className="input font-mono text-xs" readOnly value={data.inbound_webhook_url} onFocus={(e) => e.target.select()} />
        </Field>
      )}
      <button className="btn-primary" disabled={!Object.keys(form).length} onClick={save}>Save</button>
    </section>
  );
}
