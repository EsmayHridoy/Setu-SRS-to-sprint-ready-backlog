const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

function headers(userId, json = true) {
  const h = {};
  if (json) h['Content-Type'] = 'application/json';
  if (userId) h['X-User-Id'] = userId;
  return h;
}

async function request(path, { method = 'GET', userId, body } = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: headers(userId, body !== undefined),
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (res.status === 204) return null;

  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }

  if (!res.ok) {
    const detail = data?.detail || data?.message || text || res.statusText;
    throw new Error(`${res.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
  }

  return data;
}

async function streamSSE(path, userId, body, handlers, signal) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: headers(userId, true),
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '');
    let detail = text;
    try {
      detail = JSON.parse(text)?.detail || text;
    } catch {
      /* keep raw text */
    }
    throw new Error(`${res.status}: ${detail || res.statusText}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  // SSE frames are separated by a blank line. Buffer partial reads until we
  // have a whole frame, then parse its `event:` and `data:` lines.
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);

      let event = 'message';
      const dataLines = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;

      let data;
      try {
        data = JSON.parse(dataLines.join('\n'));
      } catch {
        continue;
      }

      if (event === 'start') handlers.onStart?.(data);
      else if (event === 'delta') handlers.onDelta?.(data.text);
      else if (event === 'done') handlers.onDone?.(data);
      else if (event === 'error') throw new Error(data.detail || 'Stream error');
    }
  }
}

export const api = {
  health: () => request('/api/health'),

  // Auth
  listAccounts: () => request('/api/auth/accounts'),
  getSession: (userId) => request('/api/auth/session', { userId }),

  // Projects (caller grants)
  listProjects: (userId) => request('/api/projects', { userId }),

  // Conversations / chat
  listConversations: (userId, projectId) => {
    const q = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    return request(`/api/conversations${q}`, { userId });
  },
  createConversation: (userId, projectId) =>
    request('/api/conversations', { method: 'POST', userId, body: { project_id: projectId } }),
  getConversation: (userId, id) => request(`/api/conversations/${id}`, { userId }),
  deleteConversation: (userId, id) =>
    request(`/api/conversations/${id}`, { method: 'DELETE', userId }),
  sendMessage: (userId, conversationId, content) =>
    request(`/api/conversations/${conversationId}/messages`, {
      method: 'POST',
      userId,
      body: { content },
    }),

  // Streaming reply over Server-Sent Events. Calls the handlers as frames
  // arrive: onStart({ conversation, user_message, reply_id }), onDelta(text),
  // onDone({ conversation, reply }). Returns a promise that resolves when the
  // stream ends. Pass `signal` (an AbortSignal) to cancel.
  streamMessage: (userId, conversationId, content, handlers = {}, signal) =>
    streamSSE(`/api/conversations/${conversationId}/messages/stream`, userId, { content }, handlers, signal),

  // Admin
  admin: {
    listRoles: (userId) => request('/api/admin/roles', { userId }),
    createRole: (userId, body) =>
      request('/api/admin/roles', { method: 'POST', userId, body }),
    updateRole: (userId, id, body) =>
      request(`/api/admin/roles/${id}`, { method: 'PUT', userId, body }),
    deleteRole: (userId, id) =>
      request(`/api/admin/roles/${id}`, { method: 'DELETE', userId }),

    listProjects: (userId) => request('/api/admin/projects', { userId }),
    createProject: (userId, body) =>
      request('/api/admin/projects', { method: 'POST', userId, body }),
    updateProject: (userId, id, body) =>
      request(`/api/admin/projects/${id}`, { method: 'PUT', userId, body }),
    deleteProject: (userId, id) =>
      request(`/api/admin/projects/${id}`, { method: 'DELETE', userId }),
    reindexProject: (userId, id) =>
      request(`/api/admin/projects/${id}/reindex`, { method: 'POST', userId }),

    listUsers: (userId) => request('/api/admin/users', { userId }),
    createUser: (userId, body) =>
      request('/api/admin/users', { method: 'POST', userId, body }),
    updateUser: (userId, id, body) =>
      request(`/api/admin/users/${id}`, { method: 'PUT', userId, body }),
    deleteUser: (userId, id) =>
      request(`/api/admin/users/${id}`, { method: 'DELETE', userId }),

    listAudit: (userId, limit = 100) =>
      request(`/api/admin/audit?limit=${limit}`, { userId }),
  },
};

export { API_BASE };
