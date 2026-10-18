import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, type Agent, type AgentConfig } from "../api";
import { useWs } from "../session";
import { ErrorNote, Field, Modal, PageHeader } from "../components/ui";

// Read/write a dotted path in the config object, e.g. get(cfg, "booking.provider").
function get(obj: any, path: string): any {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}
function set(obj: any, path: string, value: any): any {
  const copy = structuredClone(obj);
  const keys = path.split(".");
  let o = copy;
  keys.slice(0, -1).forEach((k) => (o = o[k] ??= {}));
  o[keys[keys.length - 1]] = value;
  return copy;
}

export default function AgentEditor() {
  const { agentId } = useParams();
  const ws = useWs();
  const nav = useNavigate();
  const isNew = !agentId;
  const [name, setName] = useState("");
  const [cfg, setCfg] = useState<AgentConfig | null>(null);
  const [mode, setMode] = useState<"outbound" | "inbound">("outbound");
  const [raw, setRaw] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [testOpen, setTestOpen] = useState(false);

  useEffect(() => {
    if (isNew) {
      api.get<AgentConfig>(`/agent-template?mode=${mode}`).then((t) => {
        setCfg(t);
        setName(mode === "inbound" ? "Front desk" : "Outbound sales");
      });
    } else {
      api.get<Agent>(ws(`/agents/${agentId}`)).then((a) => {
        setName(a.name);
        setCfg(a.config);
        setMode(a.mode);
      }).catch((e) => setError(e.message));
    }
  }, [agentId, isNew ? mode : null]); // a loaded agent's mode must not trigger a refetch

  if (!cfg) return <ErrorNote error={error} />;

  const bind = (path: string) => ({
    value: get(cfg, path) ?? "",
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => setCfg(set(cfg, path, e.target.value)),
  });
  const bindNum = (path: string) => ({
    value: get(cfg, path) ?? "",
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => setCfg(set(cfg, path, e.target.value === "" ? null : Number(e.target.value))),
  });

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const config = raw !== null ? JSON.parse(raw) : cfg;
      if (isNew) {
        const a = await api.post<Agent>(ws("/agents"), { name, config });
        nav(`/agents/${a.id}`, { replace: true });
      } else {
        const a = await api.patch<Agent>(ws(`/agents/${agentId}`), { name, config });
        setCfg(a.config);
        setRaw(null);
      }
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e: any) {
      setError(e instanceof SyntaxError ? `Advanced JSON is not valid: ${e.message}` : e.message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!agentId || !window.confirm(`Delete agent "${name}"? Campaigns using it will stop.`)) return;
    await api.del(ws(`/agents/${agentId}`));
    nav("/agents");
  };

  const calcom = get(cfg, "booking.provider") === "calcom";
  return (
    <>
      <PageHeader
        title={isNew ? "New agent" : name}
        subtitle={mode === "inbound" ? "Answers calls to your number as a receptionist." : "Places calls to consented leads and books meetings."}
        actions={
          <>
            {!isNew && <button className="btn-secondary" onClick={() => setTestOpen(true)}>Call me now</button>}
            {!isNew && <button className="btn-secondary" onClick={remove}>Delete</button>}
            <button className="btn-primary" onClick={save} disabled={busy}>{saved ? "Saved" : busy ? "Saving…" : "Save"}</button>
          </>
        }
      />
      <ErrorNote error={error} />
      <div className="grid gap-6 lg:grid-cols-2">
        <section className="card space-y-4">
          <h2 className="font-semibold">Persona</h2>
          <Field label="Agent name (internal)"><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></Field>
          {isNew && (
            <Field label="Type">
              <select className="input" value={mode} onChange={(e) => setMode(e.target.value as any)}>
                <option value="outbound">Outbound: calls leads</option>
                <option value="inbound">Inbound: answers calls</option>
              </select>
            </Field>
          )}
          <Field label="Spoken name"><input className="input" {...bind("agent.name")} /></Field>
          <Field label="Persona and instructions" hint="Who the agent is and how it should sound. Keep it short.">
            <textarea className="input min-h-28" {...bind("agent.persona")} />
          </Field>
          <Field label="ElevenLabs voice ID"><input className="input font-mono" {...bind("agent.voice_id")} /></Field>
          <Field label="LLM model" hint="Haiku is the low-latency default.">
            <input className="input font-mono" {...bind("llm.model")} />
          </Field>
        </section>

        <section className="card space-y-4">
          <h2 className="font-semibold">Script</h2>
          <Field label="Greeting" hint="Supports {contact_name}."><textarea className="input" {...bind("script.greeting")} /></Field>
          <Field label={mode === "inbound" ? "What the business offers" : "Core pitch"}>
            <textarea className="input min-h-24" {...bind("script.value_prop")} />
          </Field>
          <Field label="What it books" hint='Spoken in the prompt, e.g. "a 15-minute demo".'><input className="input" {...bind("script.meeting")} /></Field>
        </section>

        <section className="card space-y-4">
          <h2 className="font-semibold">Booking</h2>
          <Field label="Calendar">
            <select className="input" {...bind("booking.provider")}>
              <option value="none">None: log the agreement, follow up manually</option>
              <option value="calcom">Cal.com: offer and book real slots during the call</option>
            </select>
          </Field>
          {calcom && (
            <>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Cal.com username"><input className="input" {...bind("booking.username")} placeholder="acme" /></Field>
                <Field label="Event type slug"><input className="input" {...bind("booking.event_type_slug")} placeholder="15min" /></Field>
              </div>
              <p className="hint">From your booking link: cal.com/<b>acme</b>/<b>15min</b>. Add the Cal.com API key in Settings.</p>
            </>
          )}
          <Field label="Default timezone" hint="Times are spoken in the contact's timezone when known.">
            <input className="input" {...bind("booking.timezone")} placeholder="America/New_York" />
          </Field>
        </section>

        <section className="card space-y-4">
          <h2 className="font-semibold">Compliance</h2>
          <Field label="AI disclosure (spoken first, cannot be interrupted)"><textarea className="input" {...bind("compliance.ai_disclosure")} /></Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Calling hours from"><input className="input" type="time" {...bind("compliance.calling_hours.start")} /></Field>
            <Field label="to"><input className="input" type="time" {...bind("compliance.calling_hours.end")} /></Field>
          </div>
          <p className="hint">Always applied in the callee's local time, and never outside 8am to 9pm.</p>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Max cost per call (USD)"><input className="input" type="number" step="0.05" {...bindNum("limits.per_call_usd")} /></Field>
            {mode === "outbound" && (
              <Field label="Voicemail">
                <select className="input" {...bind("voicemail.action")}>
                  <option value="leave_message">Leave a message</option>
                  <option value="hang_up">Hang up</option>
                </select>
              </Field>
            )}
          </div>
        </section>
      </div>

      <details className="card mt-6" onToggle={(e) => setRaw((e.target as HTMLDetailsElement).open ? JSON.stringify(cfg, null, 2) : null)}>
        <summary className="cursor-pointer font-semibold">Advanced: full configuration (JSON)</summary>
        <p className="hint mb-2">Objections, endpointing, opt-out phrases and every other setting. While open, Save uses this JSON.</p>
        {raw !== null && <textarea className="input min-h-96 font-mono text-xs" value={raw} onChange={(e) => setRaw(e.target.value)} spellCheck={false} />}
      </details>

      {agentId && <TestCall open={testOpen} onClose={() => setTestOpen(false)} agentId={agentId} />}
    </>
  );
}

function TestCall({ open, onClose, agentId }: { open: boolean; onClose: () => void; agentId: string }) {
  const ws = useWs();
  const [phone, setPhone] = useState("");
  const [state, setState] = useState<{ busy?: boolean; ok?: string; error?: string }>({});
  const go = async () => {
    setState({ busy: true });
    try {
      const r = await api.post<{ call_id: string }>(ws(`/agents/${agentId}/test-call`), { phone });
      setState({ ok: `Calling ${phone}… The call will appear under Calls when it ends (${r.call_id.slice(0, 8)}).` });
    } catch (e: any) {
      setState({ error: e.message });
    }
  };
  return (
    <Modal open={open} onClose={onClose} title="Call me now">
      <p className="mb-3 text-sm text-slate-500">The agent calls this number right away, using your saved settings. For testing on your own phone only.</p>
      <ErrorNote error={state.error ?? null} />
      {state.ok && <p className="mb-3 text-sm text-emerald-600">{state.ok}</p>}
      <Field label="Your phone number"><input className="input" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+1 212 555 0100" /></Field>
      <div className="mt-4 flex justify-end gap-2">
        <button className="btn-secondary" onClick={onClose}>Close</button>
        <button className="btn-primary" onClick={go} disabled={state.busy || !phone}>Call</button>
      </div>
    </Modal>
  );
}
