import { useEffect, useState, type ReactNode } from "react";

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

const tones: Record<string, string> = {
  green: "bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-900",
  red: "bg-rose-50 text-rose-700 ring-rose-200 dark:bg-rose-950 dark:text-rose-300 dark:ring-rose-900",
  amber: "bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-900",
  blue: "bg-brand-50 text-brand-700 ring-brand-100 dark:bg-slate-800 dark:text-brand-100 dark:ring-slate-700",
  gray: "bg-slate-100 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700",
};

export function Badge({ tone = "gray", children }: { tone?: keyof typeof tones; children: ReactNode }) {
  return <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tones[tone]}`}>{children}</span>;
}

const outcomeTone: Record<string, keyof typeof tones> = {
  booked_meeting: "green", callback_requested: "blue", not_interested: "gray", voicemail: "amber",
  no_answer: "amber", opted_out: "red", hung_up: "gray", budget_exceeded: "red", error: "red",
};

export function OutcomeBadge({ outcome }: { outcome: string | null }) {
  if (!outcome) return <Badge tone="blue">in progress</Badge>;
  return <Badge tone={outcomeTone[outcome] ?? "gray"}>{outcome.replace(/_/g, " ")}</Badge>;
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-300">{error}</div>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="card flex flex-col items-center py-12 text-center">
      <p className="font-medium">{title}</p>
      {children && <div className="mt-2 max-w-md text-sm text-slate-500 dark:text-slate-400">{children}</div>}
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="hint block">{hint}</span>}
    </label>
  );
}

export function Modal({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    if (open) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-slate-900/40 p-4 pt-16" onClick={onClose}>
      <div className="card w-full max-w-lg" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={title}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button className="text-slate-400 hover:text-slate-600" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** Load data for a page; `reload()` after a mutation. */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    setError(null);
    loader()
      .then((d) => live && setData(d))
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [...deps, n]);
  return { data, error, reload: () => setN((x) => x + 1), setError };
}

export function fmtDate(iso: string | null): string {
  if (!iso) return "never";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function fmtMoney(n: number): string {
  return `$${n.toFixed(n < 1 ? 4 : 2)}`;
}
