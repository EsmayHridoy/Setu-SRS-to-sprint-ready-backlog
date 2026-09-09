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
