const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8001';

const TOKEN_KEY = 'setu_token';

let _token = localStorage.getItem(TOKEN_KEY) || '';

export function setToken(token) {
  _token = token;
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export function getToken() {
  return _token;
}

function headers(json = true) {
  const h = {};
  if (json) h['Content-Type'] = 'application/json';
  if (_token) h['Authorization'] = `Bearer ${_token}`;
  return h;
}

async function request(path, { method = 'GET', body } = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: headers(body !== undefined),
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
    const detail = data?.detail || data?.error || data?.message || text || res.statusText;
    throw new Error(`${res.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
  }

  return data;
}

// Multipart POST. The browser sets Content-Type (with the boundary) itself,
// so headers() is asked for no JSON content type.
async function requestForm(path, form) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: headers(false),
    body: form,
  });

  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }

  if (!res.ok) {
    const detail = data?.detail || data?.error || data?.message || text || res.statusText;
    throw new Error(`${res.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
  }

  return data;
}

// Fetches a file the API returns as bytes and hands it to the browser as a
// download. It cannot be a plain <a href> or window.open: the endpoint needs
// the Authorization header, which only fetch can set -- so the response is
// read into a blob and an object URL is clicked instead. `fallbackName` is
// used when the response has no Content-Disposition filename (or the header
// is not exposed to this origin).
async function download(path, fallbackName) {
  const res = await fetch(`${API_BASE}${path}`, { headers: headers(false) });

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    let detail = text;
    try {
      detail = JSON.parse(text)?.detail || text;
    } catch {
      /* keep raw text */
    }
    throw new Error(`${res.status}: ${detail || res.statusText}`);
  }

  const disposition = res.headers.get('Content-Disposition') || '';
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  const plain = /filename="?([^";]+)"?/i.exec(disposition);
  const name = utf8 ? decodeURIComponent(utf8[1]) : plain ? plain[1] : fallbackName;

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoked on a later tick: Safari cancels the download if the URL goes
  // away in the same one.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return name;
}

// Opens an SSE stream. Pass `{ json }` for a JSON body or `{ form }` for
// multipart (a FormData) — with FormData the browser sets Content-Type.
// Pass neither for a GET.
async function streamSSE(path, { json, form } = {}, handlers, signal) {
  const isForm = form !== undefined;
  const hasBody = isForm || json !== undefined;
  const res = await fetch(`${API_BASE}${path}`, {
    method: hasBody ? 'POST' : 'GET',
    headers: headers(hasBody && !isForm),
    body: isForm ? form : hasBody ? JSON.stringify(json) : undefined,
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
      else handlers.onEvent?.(event, data);
    }
  }
}

export const api = {
  health: () => request('/api/health'),

  // Auth
  login: (email, password) =>
    request('/api/auth/login', { method: 'POST', body: { email, password } }),
  getSession: () => request('/api/auth/session'),
  changePassword: (current_password, new_password) =>
    request('/api/auth/change-password', { method: 'POST', body: { current_password, new_password } }),

  // Projects (caller grants)
  listProjects: () => request('/api/projects'),

  // Conversations / chat
  listConversations: (projectId) => {
    const q = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    return request(`/api/conversations${q}`);
  },
  createConversation: (projectId) =>
    request('/api/conversations', { method: 'POST', body: { project_id: projectId } }),
  getConversation: (id) => request(`/api/conversations/${id}`),
  deleteConversation: (id) =>
    request(`/api/conversations/${id}`, { method: 'DELETE' }),
  sendMessage: (conversationId, content) =>
    request(`/api/conversations/${conversationId}/messages`, {
      method: 'POST',
      body: { content },
    }),

  extractFile: (file) => {
    const form = new FormData();
    form.append('file', file);
    return requestForm('/api/uploads/extract', form);
  },

  streamMessage: (conversationId, content, handlers = {}, signal) =>
    streamSSE(
      `/api/conversations/${conversationId}/messages/stream`,
      { json: { content } },
      handlers,
      signal,
    ),

  streamUpload: (conversationId, file, prompt, handlers = {}, signal) => {
    const form = new FormData();
    form.append('file', file);
    if (prompt) form.append('prompt', prompt);
    return streamSSE(
      `/api/conversations/${conversationId}/messages/upload`,
      { form },
      handlers,
      signal,
    );
  },

  // Business plans
  business: {
    getPlan: (id) => request(`/api/business-plans/${id}`),
    updateItems: (id, items) =>
      request(`/api/business-plans/${id}/items`, { method: 'PATCH', body: { items } }),
    // One business still waiting to be vetted, e.g. while vetting is paused.
    updatePendingItem: (id, itemId, description) =>
      request(`/api/business-plans/${id}/items/${itemId}`, {
        method: 'PATCH',
        body: { description },
      }),
    confirm: (id) =>
      request(`/api/business-plans/${id}/confirm`, { method: 'POST' }),
    discard: (id) =>
      request(`/api/business-plans/${id}/discard`, { method: 'POST' }),
    streamVetting: (id, handlers = {}, signal) =>
      streamSSE(`/api/business-plans/${id}/vet/stream`, {}, handlers, signal),

    // The step after vetting: the user's own SRS format (.docx only), which
    // comes back with every vetted story written into it.
    uploadSrsFormat: (id, file) => {
      const form = new FormData();
      form.append('file', file);
      return requestForm(`/api/business-plans/${id}/srs`, form);
    },
    regenerateSrs: (id) =>
      request(`/api/business-plans/${id}/srs/regenerate`, { method: 'POST' }),
    downloadSrs: (id) =>
      download(`/api/business-plans/${id}/srs/download`, 'srs.docx'),

    // Discussion under one vetted item: a clarifying question gets a
    // conversational answer; new information can revise the verdict in
    // place. Approving is the BA's own explicit sign-off -- separate from
    // vetting itself, and required before an item is backlog-ready.
    getItemComments: (planId, itemId) =>
      request(`/api/business-plans/${planId}/items/${itemId}/comments`),
    postItemComment: (planId, itemId, content) =>
      request(`/api/business-plans/${planId}/items/${itemId}/comments`, {
        method: 'POST',
        body: { content },
      }),
    approveItem: (planId, itemId) =>
      request(`/api/business-plans/${planId}/items/${itemId}/approve`, {
        method: 'POST',
      }),
  },

  // Admin
  admin: {
    listRoles: () => request('/api/admin/roles'),
    createRole: (body) =>
      request('/api/admin/roles', { method: 'POST', body }),
    updateRole: (id, body) =>
      request(`/api/admin/roles/${id}`, { method: 'PUT', body }),
    deleteRole: (id) =>
      request(`/api/admin/roles/${id}`, { method: 'DELETE' }),

    listProjects: () => request('/api/admin/projects'),
    createProject: (body) =>
      request('/api/admin/projects', { method: 'POST', body }),
    updateProject: (id, body) =>
      request(`/api/admin/projects/${id}`, { method: 'PUT', body }),
    deleteProject: (id) =>
      request(`/api/admin/projects/${id}`, { method: 'DELETE' }),
    reindexProject: (id) =>
      request(`/api/admin/projects/${id}/reindex`, { method: 'POST' }),

    listUsers: () => request('/api/admin/users'),
    createUser: (body) =>
      request('/api/admin/users', { method: 'POST', body }),
    updateUser: (id, body) =>
      request(`/api/admin/users/${id}`, { method: 'PUT', body }),
    deleteUser: (id) =>
      request(`/api/admin/users/${id}`, { method: 'DELETE' }),

    listAudit: (limit = 100) =>
      request(`/api/admin/audit?limit=${limit}`),

    resetPassword: (id, new_password) =>
      request(`/api/admin/users/${id}/reset-password`, { method: 'POST', body: { new_password } }),
  },
};

export { API_BASE };
