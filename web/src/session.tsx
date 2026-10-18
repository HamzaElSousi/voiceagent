import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, type Me, type WorkspaceRef } from "./api";

type Session = {
  me: Me | null;
  loading: boolean;
  workspace: WorkspaceRef | null;
  setWorkspaceId: (id: string) => void;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
};

const Ctx = createContext<Session | null>(null);
const WS_KEY = "va.workspace";

function storedWorkspace(): string | null {
  try {
    return localStorage.getItem(WS_KEY);
  } catch {
    return null;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [wsId, setWsId] = useState<string | null>(storedWorkspace());

  const refresh = useCallback(async () => {
    try {
      setMe(await api.get<Me>("/auth/me"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setMe(null);
      else throw e;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const setWorkspaceId = (id: string) => {
    setWsId(id);
    try {
      localStorage.setItem(WS_KEY, id);
    } catch {
      /* private mode: selection just won't persist */
    }
  };

  const logout = async () => {
    await api.post("/auth/logout");
    setMe(null);
  };

  const workspace = me?.workspaces.find((w) => w.id === wsId) ?? me?.workspaces[0] ?? null;
  return (
    <Ctx.Provider value={{ me, loading, workspace, setWorkspaceId, refresh, logout }}>{children}</Ctx.Provider>
  );
}

export function useSession(): Session {
  const s = useContext(Ctx);
  if (!s) throw new Error("useSession outside SessionProvider");
  return s;
}

/** Workspace-scoped API path: ws("/agents") -> "/workspaces/<id>/agents". */
export function useWs(): (path: string) => string {
  const { workspace } = useSession();
  return (path: string) => `/workspaces/${workspace?.id}${path}`;
}
