import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { useSession } from "../session";
import { ErrorNote, Field } from "../components/ui";

type Instance = { needs_setup: boolean; signups_open: boolean };

export default function AuthPage() {
  const { refresh } = useSession();
  const [instance, setInstance] = useState<Instance | null>(null);
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [form, setForm] = useState({ email: "", password: "", name: "", workspace_name: "", accept_terms: false });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get<Instance>("/instance").then((i) => {
      setInstance(i);
      if (i.needs_setup) setMode("signup");
    });
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") await api.post("/auth/login", { email: form.email, password: form.password });
      else await api.post("/auth/signup", { ...form, workspace_name: form.workspace_name || "My workspace" });
      await refresh();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center justify-center gap-2">
          <img src="/favicon.svg" alt="" className="h-9 w-9" />
          <span className="text-2xl font-semibold">VoiceAgent</span>
        </div>
        <form className="card space-y-4" onSubmit={submit}>
          <div>
            <h1 className="text-lg font-semibold">
              {instance?.needs_setup ? "Set up your instance" : mode === "login" ? "Sign in" : "Create an account"}
            </h1>
            {instance?.needs_setup && (
              <p className="hint">The first account becomes the instance admin. Sign-ups close after it unless ALLOW_SIGNUPS is on.</p>
            )}
          </div>
          <ErrorNote error={error} />
          {mode === "signup" && (
            <>
              <Field label="Your name"><input className="input" value={form.name} onChange={set("name")} autoComplete="name" /></Field>
              <Field label="Workspace name" hint="Usually your company or team.">
                <input className="input" value={form.workspace_name} onChange={set("workspace_name")} placeholder="Acme" />
              </Field>
            </>
          )}
          <Field label="Email"><input className="input" type="email" required value={form.email} onChange={set("email")} autoComplete="email" /></Field>
          <Field label="Password" hint={mode === "signup" ? "At least 10 characters." : undefined}>
            <input className="input" type="password" required minLength={mode === "signup" ? 10 : 1} value={form.password} onChange={set("password")}
              autoComplete={mode === "login" ? "current-password" : "new-password"} />
          </Field>
          {mode === "signup" && (
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" className="mt-1" checked={form.accept_terms} onChange={set("accept_terms")} required />
              <span>
                I am responsible for having consent to call the people I call and for following the calling laws that apply
                to me (for example the TCPA in the US and CRTC rules in Canada).
              </span>
            </label>
          )}
          <button className="btn-primary w-full" disabled={busy}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button>
          {!instance?.needs_setup && instance?.signups_open && (
            <p className="text-center text-sm">
              {mode === "login" ? "No account? " : "Have an account? "}
              <button type="button" className="text-brand-600 hover:underline" onClick={() => setMode(mode === "login" ? "signup" : "login")}>
                {mode === "login" ? "Create one" : "Sign in"}
              </button>
            </p>
          )}
        </form>
      </div>
    </div>
  );
}
