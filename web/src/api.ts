// Thin client for the VoiceAgent JSON API. Session is an httpOnly cookie; writes echo the CSRF
// token (readable cookie) in X-CSRF-Token, which the server checks on every mutating request.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)va_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  const res = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const data = text ? JSON.parse(text) : undefined;
  if (!res.ok) {
    const detail = data?.detail;
    const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : res.statusText;
    throw new ApiError(res.status, msg || `HTTP ${res.status}`);
  }
  return data as T;
}

export const api = {
  get: <T>(p: string) => request<T>("GET", p),
  post: <T>(p: string, b?: unknown) => request<T>("POST", p, b ?? {}),
  put: <T>(p: string, b?: unknown) => request<T>("PUT", p, b ?? {}),
  patch: <T>(p: string, b?: unknown) => request<T>("PATCH", p, b ?? {}),
  del: <T>(p: string) => request<T>("DELETE", p),
};

// -- types (mirror the API responses) --------------------------------------------------------

export type User = { id: string; email: string; name: string; is_instance_admin: boolean };
export type WorkspaceRef = { id: string; name: string; slug: string; role: string };
export type Me = { user: User; workspaces: WorkspaceRef[]; csrf_token: string };
export type Workspace = WorkspaceRef & { retention_days: number; inbound_webhook_url: string | null };
export type KeyInfo = { name: string; label: string; is_set: boolean; masked: string | null; updated_at: string | null };
export type AgentConfig = Record<string, any>;
export type Agent = { id: string; name: string; mode: "outbound" | "inbound"; is_active: boolean; config: AgentConfig; updated_at: string | null };
export type Lead = {
  id: string; connector_id: string | null; external_id: string; name: string; phone: string; email: string;
  company: string; timezone: string; has_consent: boolean; consent_at: string | null; consent_evidence: string;
  status: string; attempts: number; last_called_at: string | null; dialable_now: boolean; blocked_reason: string | null;
};
export type Connector = {
  id: string; type: "mcp" | "webhook" | "csv"; name: string; config: Record<string, any>; enabled: boolean;
  has_secret: boolean; secret_masked: string | null; last_sync_at: string | null; last_error: string | null;
};
export type Campaign = {
  id: string; name: string; agent_id: string; connector_id: string | null; status: string; status_reason: string;
  budget_usd: number; spent_usd: number; concurrency: number; max_attempts: number; retry_delay_minutes: number;
  outcomes: Record<string, number>; calls: number;
};
export type CallSummary = {
  id: string; agent_id: string | null; twilio_sid: string; direction: string; status: string; contact_name: string;
  contact_phone: string; outcome: string | null; end_reason: string; duration_seconds: number; cost_usd: number;
  booking_ref: string | null; booked_slot: string | null; latency_p50_ms: number | null; created_at: string | null;
};
export type CallDetail = CallSummary & { transcript: { speaker: string; text: string }[]; error: string | null };
export type PhoneNumber = { id: string; e164: string; twilio_sid: string; inbound_agent_id: string | null };
export type DncEntry = { phone: string; reason: string; source: string; created_at: string };
