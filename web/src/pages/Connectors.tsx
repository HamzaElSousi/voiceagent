import { useState } from "react";
import { api, type Connector } from "../api";
import { useSession, useWs } from "../session";
import { Badge, Empty, ErrorNote, Field, Modal, PageHeader, fmtDate, useLoad } from "../components/ui";

const MCP_EXAMPLE = {
  url: "https://crm.example.com/mcp",
  auth_header: "Authorization",
  lead_tool: "",
  lead_args: {},
  items_path: "$",
  field_map: { external_id: "$.id", name: "$.name", phone: "$.phone", email: "$.email", consent_at: "$.consent_at" },
  outcome_tool: "",
  outcome_args: { lead_id: "{{lead.external_id}}", outcome: "{{outcome}}", note: "AI call: {{end_reason}}" },
};
const WEBHOOK_EXAMPLE = { leads_url: "https://crm.example.com/leads", outcome_url: "https://crm.example.com/outcome", items_path: "$", field_map: {} };

type TestResult = { ok: boolean; error?: string; info?: any; preview: any[] };

export default function Connectors() {
  const { workspace } = useSession();
  const ws = useWs();
  const [editing, setEditing] = useState<Connector | "new" | null>(null);
  const [test, setTest] = useState<{ id: string; result: TestResult | null } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const { data, error, reload, setError } = useLoad(() => api.get<Connector[]>(ws("/connectors")), [workspace?.id]);

  const runTest = async (c: Connector) => {
    setTest({ id: c.id, result: null });
    try {
      setTest({ id: c.id, result: await api.post<TestResult>(ws(`/connectors/${c.id}/test`)) });
    } catch (e: any) {
      setTest({ id: c.id, result: { ok: false, error: e.message, preview: [] } });
    }
  };
  const sync = async (c: Connector) => {
    try {
      const r = await api.post<any>(ws(`/connectors/${c.id}/sync`));
      setNotice(r.errors?.length ? `Sync failed: ${r.errors[0]}` : `${c.name}: ${r.created} new, ${r.updated} updated, ${r.without_consent} without consent, ${r.invalid_phone} invalid numbers.`);
      reload();
    } catch (e: any) {
      setError(e.message);
    }
  };

  return (
    <>
      <PageHeader title="Connectors" subtitle="Where leads come from and where call outcomes go. Any MCP server, a webhook pair, or CSV."
        actions={<button className="btn-primary" onClick={() => setEditing("new")}>Add connector</button>} />
      <ErrorNote error={error} />
      {notice && <p className="mb-4 rounded-lg bg-brand-50 px-3 py-2 text-sm text-brand-700 dark:bg-slate-800 dark:text-brand-100">{notice}</p>}
      {data && data.length === 0 && (
        <Empty title="No connectors">
          Point VoiceAgent at your CRM's MCP server, or at two small webhook endpoints. Integration guide: docs/CONNECTORS.md in the repo.
        </Empty>
      )}
      <div className="space-y-4">
        {data?.map((c) => (
          <div key={c.id} className="card">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-semibold">{c.name}</span>
                  <Badge tone="blue">{c.type.toUpperCase()}</Badge>
                  {!c.enabled && <Badge tone="gray">disabled</Badge>}
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  {c.config.url || c.config.leads_url || "CSV imports"} · last sync {fmtDate(c.last_sync_at)}
                </p>
                {c.last_error && <p className="mt-1 text-sm text-rose-600">{c.last_error}</p>}
              </div>
              <div className="flex gap-2">
                {c.type !== "csv" && <button className="btn-secondary" onClick={() => runTest(c)}>Test</button>}
                {c.type !== "csv" && <button className="btn-secondary" onClick={() => sync(c)}>Sync now</button>}
                <button className="btn-secondary" onClick={() => setEditing(c)}>Edit</button>
              </div>
            </div>
            {test?.id === c.id && <TestPanel result={test.result} />}
          </div>
        ))}
      </div>
      <ConnectorModal value={editing} onClose={() => setEditing(null)} onDone={reload} />
    </>
  );
}

function TestPanel({ result }: { result: TestResult | null }) {
  if (!result) return <p className="mt-4 text-sm text-slate-500">Testing…</p>;
  if (!result.ok) return <p className="mt-4 text-sm text-rose-600">{result.error}</p>;
  return (
    <div className="mt-4 space-y-3 rounded-lg bg-slate-50 p-3 text-sm dark:bg-slate-800/60">
      <p className="text-emerald-600">Connected{result.info?.server?.name ? ` to ${result.info.server.name}` : ""}.</p>
      {result.info?.tools && (
        <div>
          <div className="mb-1 font-medium">Tools on this server</div>
          <ul className="space-y-0.5 text-xs">{result.info.tools.map((t: any) => <li key={t.name}><code>{t.name}</code> <span className="text-slate-500">{t.description}</span></li>)}</ul>
        </div>
      )}
      {result.preview.length > 0 && (
        <div>
          <div className="mb-1 font-medium">First leads, as mapped</div>
          {result.preview.map((p, i) => (
            <pre key={i} className="overflow-x-auto rounded bg-white p-2 text-xs dark:bg-slate-900">
              {p.mapped ? JSON.stringify(p.mapped, null, 2) : `${p.problem}. Fields available: ${p.raw_keys.join(", ")}`}
            </pre>
          ))}
        </div>
      )}
    </div>
  );
}

function ConnectorModal({ value, onClose, onDone }: { value: Connector | "new" | null; onClose: () => void; onDone: () => void }) {
  const ws = useWs();
  const existing = value && value !== "new" ? value : null;
  const [type, setType] = useState<"mcp" | "webhook" | "csv">("mcp");
  const [name, setName] = useState("");
  const [config, setConfig] = useState("");
  const [secret, setSecret] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  const key = existing?.id ?? (value === "new" ? `new-${type}` : null);
  if (!key && loadedFor) setLoadedFor(null); // closed: the next open starts fresh
  if (key && key !== loadedFor) {
    setLoadedFor(key);
    setName(existing?.name ?? "");
    setSecret("");
    setError(null);
    const t = existing?.type ?? type;
    if (existing) setType(existing.type);
    setConfig(JSON.stringify(existing?.config ?? (t === "mcp" ? MCP_EXAMPLE : t === "webhook" ? WEBHOOK_EXAMPLE : {}), null, 2));
  }

  const save = async () => {
    setError(null);
    try {
      const parsed = config.trim() ? JSON.parse(config) : {};
      const body: any = { name, config: parsed };
      if (secret) body.secret = secret;
      if (existing) await api.patch(ws(`/connectors/${existing.id}`), body);
      else await api.post(ws("/connectors"), { ...body, type });
      onDone();
      onClose();
    } catch (e: any) {
      setError(e instanceof SyntaxError ? `Configuration is not valid JSON: ${e.message}` : e.message);
    }
  };
  const remove = async () => {
    if (!existing || !window.confirm(`Delete ${existing.name}? Its leads stay, but stop syncing.`)) return;
    await api.del(ws(`/connectors/${existing.id}`));
    onDone();
    onClose();
  };

  return (
    <Modal open={value !== null} onClose={onClose} title={existing ? `Edit ${existing.name}` : "Add connector"}>
      <ErrorNote error={error} />
      <div className="space-y-4">
        {!existing && (
          <Field label="Type">
            <select className="input" value={type} onChange={(e) => setType(e.target.value as any)}>
              <option value="mcp">MCP server</option>
              <option value="webhook">Webhooks (GET leads, signed POST outcomes)</option>
              <option value="csv">CSV imports only</option>
            </select>
          </Field>
        )}
        <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="My CRM" /></Field>
        {(existing?.type ?? type) !== "csv" && (
          <>
            <Field label={(existing?.type ?? type) === "mcp" ? "API key or token" : "Shared secret"}
              hint={existing?.has_secret ? `Stored (${existing.secret_masked}). Leave empty to keep it.` : "Stored encrypted. Never shown again."}>
              <input className="input font-mono" type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="off" />
            </Field>
            <Field label="Configuration" hint="Pick tools after a first Test shows what the server offers. Field paths look like $.phone.">
              <textarea className="input min-h-64 font-mono text-xs" value={config} onChange={(e) => setConfig(e.target.value)} spellCheck={false} />
            </Field>
          </>
        )}
      </div>
      <div className="mt-5 flex justify-between">
        {existing ? <button className="btn-danger" onClick={remove}>Delete</button> : <span />}
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" onClick={save} disabled={!name}>Save</button>
        </div>
      </div>
    </Modal>
  );
}
