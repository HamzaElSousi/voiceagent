import { Link } from "react-router-dom";
import { api, type Agent, type CallSummary, type Campaign, type Connector, type KeyInfo, type PhoneNumber } from "../api";
import { useSession, useWs } from "../session";
import { Badge, ErrorNote, OutcomeBadge, PageHeader, fmtDate, fmtMoney, useLoad } from "../components/ui";

const REQUIRED_KEYS = ["twilio_account_sid", "twilio_auth_token", "twilio_from_number", "deepgram_api_key", "elevenlabs_api_key"];

export default function Overview() {
  const { workspace } = useSession();
  const ws = useWs();
  const { data, error } = useLoad(async () => {
    const [keys, agents, numbers, connectors, campaigns, calls] = await Promise.all([
      api.get<KeyInfo[]>(ws("/keys")),
      api.get<Agent[]>(ws("/agents")),
      api.get<PhoneNumber[]>(ws("/numbers")),
      api.get<Connector[]>(ws("/connectors")),
      api.get<Campaign[]>(ws("/campaigns")),
      api.get<CallSummary[]>(ws("/calls?limit=8")),
    ]);
    return { keys, agents, numbers, connectors, campaigns, calls };
  }, [workspace?.id]);

  const set = new Set(data?.keys.filter((k) => k.is_set).map((k) => k.name));
  const llm = set.has("anthropic_api_key") || set.has("openrouter_api_key");
  const steps = data
    ? [
        { done: REQUIRED_KEYS.every((k) => set.has(k)) && llm, title: "Add your provider keys", body: "Twilio, Deepgram, ElevenLabs and an LLM key. You pay providers directly.", to: "/settings" },
        { done: data.agents.length > 0, title: "Create an agent", body: "Its persona, script, booking and compliance settings.", to: "/agents/new" },
        { done: data.agents.length > 0 && set.has("twilio_from_number"), title: "Test it on your phone", body: "Use \"Call me now\" on the agent page.", to: data.agents[0] ? `/agents/${data.agents[0].id}` : "/agents/new" },
        { done: set.has("calcom_api_key"), title: "Connect a calendar (optional)", body: "A Cal.com key lets the agent book real slots during the call.", to: "/settings" },
        { done: data.connectors.length > 0, title: "Connect your CRM", body: "Any MCP server, a webhook pair, or a CSV import.", to: "/connectors" },
        { done: data.numbers.length > 0, title: "Add an inbound number (optional)", body: "Route calls to your Twilio number to an inbound agent.", to: "/settings" },
      ]
    : [];
  const done = steps.filter((s) => s.done).length;
  const spent = data?.calls.reduce((a, c) => a + c.cost_usd, 0) ?? 0;
  const booked = data?.calls.filter((c) => c.outcome === "booked_meeting").length ?? 0;

  return (
    <>
      <PageHeader title={workspace?.name ?? "Overview"} subtitle="Your agents, campaigns and recent calls." />
      <ErrorNote error={error} />
      {data && done < steps.length && (
        <section className="card mb-6">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="font-semibold">Get set up</h2>
            <span className="text-sm text-slate-500">{done} of {steps.length} done</span>
          </div>
          <ol className="grid gap-3 sm:grid-cols-2">
            {steps.map((s) => (
              <li key={s.title}>
                <Link to={s.to} className="flex gap-3 rounded-lg border border-slate-200 p-3 hover:border-brand-500 dark:border-slate-800">
                  <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs ${s.done ? "bg-emerald-500 text-white" : "border border-slate-300 dark:border-slate-600"}`}>
                    {s.done ? "✓" : ""}
                  </span>
                  <span>
                    <span className="block text-sm font-medium">{s.title}</span>
                    <span className="block text-xs text-slate-500 dark:text-slate-400">{s.body}</span>
                  </span>
                </Link>
              </li>
            ))}
          </ol>
        </section>
      )}
      <div className="mb-6 grid gap-4 sm:grid-cols-3">
        <Stat label="Agents" value={data?.agents.length ?? "…"} />
        <Stat label="Running campaigns" value={data?.campaigns.filter((c) => c.status === "running").length ?? "…"} />
        <Stat label="Booked (recent calls)" value={data ? `${booked} · ${fmtMoney(spent)} spent` : "…"} />
      </div>
      <section className="card p-0">
        <div className="flex items-center justify-between px-5 py-4">
          <h2 className="font-semibold">Recent calls</h2>
          <Link to="/calls" className="text-sm text-brand-600 hover:underline">All calls</Link>
        </div>
        {data && data.calls.length === 0 ? (
          <p className="px-5 pb-5 text-sm text-slate-500">No calls yet.</p>
        ) : (
          <table className="w-full">
            <tbody>
              {data?.calls.map((c) => (
                <tr key={c.id}>
                  <td className="td"><Link className="font-medium hover:underline" to={`/calls/${c.id}`}>{c.contact_name || c.contact_phone}</Link></td>
                  <td className="td hidden sm:table-cell"><Badge>{c.direction}</Badge></td>
                  <td className="td"><OutcomeBadge outcome={c.outcome} /></td>
                  <td className="td hidden text-slate-500 sm:table-cell">{fmtDate(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="card">
      <div className="text-sm text-slate-500 dark:text-slate-400">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
    </div>
  );
}
