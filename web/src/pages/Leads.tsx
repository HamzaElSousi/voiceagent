import { useState } from "react";
import { api, type Lead } from "../api";
import { useSession, useWs } from "../session";
import { Badge, Empty, ErrorNote, Field, Modal, PageHeader, fmtDate, useLoad } from "../components/ui";

const BLOCKED: Record<string, string> = {
  no_consent: "No call consent",
  dnc: "Do not call",
  invalid_phone: "Invalid number",
  outside_hours: "Outside their hours",
  max_attempts: "Max attempts",
};

export default function Leads() {
  const { workspace } = useSession();
  const ws = useWs();
  const [q, setQ] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [consentFor, setConsentFor] = useState<Lead | null>(null);
  const { data, error, reload } = useLoad(
    () => api.get<{ total: number; items: Lead[] }>(ws(`/leads?limit=100${q ? `&q=${encodeURIComponent(q)}` : ""}`)),
    [workspace?.id, q],
  );

  return (
    <>
      <PageHeader
        title="Leads"
        subtitle="Synced from your connectors or imported. Only leads with recorded call consent are ever dialed."
        actions={<button className="btn-primary" onClick={() => setImportOpen(true)}>Import CSV</button>}
      />
      <ErrorNote error={error} />
      <input className="input mb-4 max-w-sm" placeholder="Search name, email or phone" value={q} onChange={(e) => setQ(e.target.value)} />
      {data && data.items.length === 0 ? (
        <Empty title="No leads">Connect a CRM under Connectors, or import a CSV with a phone column and consent columns.</Empty>
      ) : (
        <div className="card overflow-x-auto p-0">
          <table className="w-full min-w-[640px]">
            <thead><tr><th className="th">Lead</th><th className="th">Phone</th><th className="th">Consent</th><th className="th">Status</th><th className="th">Last call</th><th className="th" /></tr></thead>
            <tbody>
              {data?.items.map((l) => (
                <tr key={l.id}>
                  <td className="td"><div className="font-medium">{l.name || "Unnamed"}</div><div className="text-xs text-slate-500">{l.email || l.company}</div></td>
                  <td className="td font-mono text-xs">{l.phone}</td>
                  <td className="td">
                    {l.has_consent ? <Badge tone="green" >Consent</Badge> : <Badge tone="red">None</Badge>}
                  </td>
                  <td className="td">
                    <div className="flex flex-wrap gap-1">
                      <Badge>{l.status}</Badge>
                      {l.blocked_reason && <Badge tone="amber">{BLOCKED[l.blocked_reason] ?? l.blocked_reason}</Badge>}
                    </div>
                  </td>
                  <td className="td text-xs text-slate-500">{fmtDate(l.last_called_at)} {l.attempts > 0 && `· ${l.attempts} tries`}</td>
                  <td className="td text-right">
                    {!l.has_consent && <button className="text-sm text-brand-600 hover:underline" onClick={() => setConsentFor(l)}>Record consent</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {data && data.total > data.items.length && <p className="px-3 py-2 text-xs text-slate-500">Showing {data.items.length} of {data.total}. Search to narrow down.</p>}
        </div>
      )}
      <ImportModal open={importOpen} onClose={() => setImportOpen(false)} onDone={reload} />
      <ConsentModal lead={consentFor} onClose={() => setConsentFor(null)} onDone={reload} />
    </>
  );
}

function ImportModal({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: () => void }) {
  const ws = useWs();
  const [csv, setCsv] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const onFile = async (f: File | undefined) => f && setCsv(await f.text());
  const go = async () => {
    setError(null);
    try {
      const r = await api.post<any>(ws("/leads/import"), { csv });
      setResult(`${r.created} added, ${r.updated} updated, ${r.without_consent} without consent, ${r.invalid_phone + r.skipped_without_phone} skipped.`);
      onDone();
    } catch (e: any) {
      setError(e.message);
    }
  };
  return (
    <Modal open={open} onClose={onClose} title="Import leads from CSV">
      <ErrorNote error={error} />
      {result && <p className="mb-3 text-sm text-emerald-600">{result}</p>}
      <p className="mb-3 text-sm text-slate-500">
        Columns: <code>name, phone, email, company, timezone, consent_at, consent_evidence</code>. Only <code>phone</code> is required, but
        rows without <code>consent_at</code> will not be called.
      </p>
      <input type="file" accept=".csv,text/csv" className="mb-3 block text-sm" onChange={(e) => onFile(e.target.files?.[0])} />
      <textarea className="input min-h-40 font-mono text-xs" value={csv} onChange={(e) => setCsv(e.target.value)} placeholder="name,phone,consent_at&#10;Ada,+12125550101,2026-10-01" />
      <div className="mt-4 flex justify-end gap-2">
        <button className="btn-secondary" onClick={onClose}>Close</button>
        <button className="btn-primary" onClick={go} disabled={!csv.trim()}>Import</button>
      </div>
    </Modal>
  );
}

function ConsentModal({ lead, onClose, onDone }: { lead: Lead | null; onClose: () => void; onDone: () => void }) {
  const ws = useWs();
  const [evidence, setEvidence] = useState("");
  const [error, setError] = useState<string | null>(null);
  const go = async () => {
    try {
      await api.post(ws(`/leads/${lead!.id}/consent`), { evidence });
      setEvidence("");
      onDone();
      onClose();
    } catch (e: any) {
      setError(e.message);
    }
  };
  return (
    <Modal open={!!lead} onClose={onClose} title={`Record call consent for ${lead?.name || lead?.phone}`}>
      <ErrorNote error={error} />
      <p className="mb-3 text-sm text-slate-500">Only when this person actually agreed to be called. Your note and name are kept with the lead.</p>
      <Field label="How and when they agreed" hint="e.g. Asked for a callback on our contact form, Oct 18 2026.">
        <textarea className="input" value={evidence} onChange={(e) => setEvidence(e.target.value)} />
      </Field>
      <div className="mt-4 flex justify-end gap-2">
        <button className="btn-secondary" onClick={onClose}>Cancel</button>
        <button className="btn-primary" onClick={go} disabled={evidence.trim().length < 10}>Record consent</button>
      </div>
    </Modal>
  );
}
