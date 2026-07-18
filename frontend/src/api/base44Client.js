// Local API shim replacing @base44/sdk. Exposes the same surface the app was
// written against (auth, entities, functions.invoke, integrations.Core.InvokeLLM)
// but talks to our own FastAPI backend with cookie-session auth.

class ApiError extends Error {
  constructor(status, data) {
    super((data && (data.detail || data.error)) || `Request failed (${status})`);
    this.status = status;
    this.data = data;
    // axios-like shape some callers expect (err?.response?.data?.error)
    this.response = { status, data };
  }
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    // non-JSON response (e.g. redirect target)
  }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

const SUBSCRIBE_POLL_MS = 60_000;

function makeEntity(name) {
  return {
    list: (sort, limit, skip) =>
      api(`/api/entities/${name}/query`, { method: "POST", body: { sort, limit, skip: skip || 0 } }),
    filter: (filter, sort, limit, skip) =>
      api(`/api/entities/${name}/query`, { method: "POST", body: { filter, sort, limit, skip: skip || 0 } }),
    create: (data) => api(`/api/entities/${name}`, { method: "POST", body: data }),
    bulkCreate: (records) => api(`/api/entities/${name}/bulk`, { method: "POST", body: { records } }),
    update: (id, data) => api(`/api/entities/${name}/${id}`, { method: "PUT", body: data }),
    delete: (id) => api(`/api/entities/${name}/${id}`, { method: "DELETE" }),
    // Realtime is replaced by polling: invoke the callback periodically so
    // dashboards refresh as background syncs land.
    subscribe: (callback) => {
      const timer = setInterval(callback, SUBSCRIBE_POLL_MS);
      return () => clearInterval(timer);
    },
  };
}

const entityCache = new Map();

export const base44 = {
  auth: {
    me: () => api("/api/auth/me"),
    updateMe: (data) => api("/api/auth/me", { method: "POST", body: data }),
    isAuthenticated: async () => {
      try {
        await api("/api/auth/me");
        return true;
      } catch {
        return false;
      }
    },
    logout: async () => {
      try {
        await api("/api/auth/logout", { method: "POST" });
      } finally {
        window.location.href = "/login";
      }
    },
    redirectToLogin: () => {
      window.location.href = "/login";
    },
  },
  entities: new Proxy(
    {},
    {
      get(_target, name) {
        if (typeof name !== "string") return undefined;
        if (!entityCache.has(name)) entityCache.set(name, makeEntity(name));
        return entityCache.get(name);
      },
    }
  ),
  functions: {
    // axios-like return shape: callers read res.data
    invoke: async (name, payload) => {
      const data = await api(`/api/functions/${name}`, { method: "POST", body: payload || {} });
      return { data };
    },
  },
  integrations: {
    Core: {
      InvokeLLM: async ({ prompt, response_json_schema }) => {
        const data = await api("/api/llm/invoke", {
          method: "POST",
          body: { prompt, response_json_schema },
        });
        return data.result;
      },
    },
  },
};
