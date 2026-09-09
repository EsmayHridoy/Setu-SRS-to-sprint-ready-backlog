import { useEffect, useState } from 'react';
import { api, API_BASE } from './api';

const STORAGE_KEY = 'setu_user_id';

export default function App() {
  const [accounts, setAccounts] = useState([]);
  const [userId, setUserId] = useState(() => localStorage.getItem(STORAGE_KEY) || '');
  const [session, setSession] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const [projectId, setProjectId] = useState('');
  const [conversations, setConversations] = useState([]);
  const [activeConversation, setActiveConversation] = useState(null);
  const [messageText, setMessageText] = useState('');
  const [tab, setTab] = useState('chat'); // chat | admin

  // Admin state
  const [adminRoles, setAdminRoles] = useState([]);
  const [adminProjects, setAdminProjects] = useState([]);
  const [adminUsers, setAdminUsers] = useState([]);
  const [adminAudit, setAdminAudit] = useState([]);

  useEffect(() => {
    api
      .listAccounts()
      .then(setAccounts)
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!userId) {
      setSession(null);
      return;
    }
    localStorage.setItem(STORAGE_KEY, userId);
    loadSession(userId);
  }, [userId]);

  useEffect(() => {
    if (userId && projectId) loadConversations(projectId);
  }, [userId, projectId]);

  async function loadSession(id) {
    setLoading(true);
    setError('');
    try {
      const s = await api.getSession(id);
      setSession(s);
      if (s.projects?.length && !projectId) {
        setProjectId(s.projects[0].id);
      }
    } catch (e) {
      setError(e.message);
      setSession(null);
    } finally {
      setLoading(false);
    }
  }

  async function loadConversations(pid = projectId) {
    if (!userId) return;
    setError('');
    try {
      const list = await api.listConversations(userId, pid || undefined);
      setConversations(list);
    } catch (e) {
      setError(e.message);
    }
  }

  async function openConversation(id) {
    setError('');
    try {
      const detail = await api.getConversation(userId, id);
      setActiveConversation(detail);
    } catch (e) {
      setError(e.message);
    }
  }

  async function createConversation() {
    if (!projectId) {
      setError('Select a project first');
      return;
    }
    setError('');
    try {
      const conv = await api.createConversation(userId, projectId);
      await loadConversations();
      await openConversation(conv.id);
    } catch (e) {
      setError(e.message);
    }
  }

  async function deleteConversation(id) {
    setError('');
    try {
      await api.deleteConversation(userId, id);
      if (activeConversation?.id === id) setActiveConversation(null);
      await loadConversations();
    } catch (e) {
      setError(e.message);
    }
  }

  async function sendMessage(e) {
    e.preventDefault();
    if (!activeConversation || !messageText.trim()) return;
    setError('');
    setLoading(true);
    try {
      const result = await api.sendMessage(userId, activeConversation.id, messageText.trim());
      setMessageText('');
      setActiveConversation((prev) => ({
        ...prev,
        ...result.conversation,
        messages: [...(prev.messages || []), result.user_message, result.reply],
      }));
      await loadConversations();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function loadAdmin() {
    if (!userId || !session?.is_admin) return;
    setError('');
    setLoading(true);
    try {
      const [roles, projects, users, audit] = await Promise.all([
        api.admin.listRoles(userId),
        api.admin.listProjects(userId),
        api.admin.listUsers(userId),
        api.admin.listAudit(userId, 50),
      ]);
      setAdminRoles(roles);
      setAdminProjects(projects);
      setAdminUsers(users);
      setAdminAudit(audit);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  function logout() {
    localStorage.removeItem(STORAGE_KEY);
    setUserId('');
    setSession(null);
    setActiveConversation(null);
    setConversations([]);
    setTab('chat');
  }

  return (
    <div style={{ fontFamily: 'sans-serif', padding: 16, maxWidth: 960 }}>
      <h1>Setu API Client</h1>
      <p>API: {API_BASE}</p>

      {error && (
        <p style={{ color: 'crimson' }}>
          <strong>Error:</strong> {error}
        </p>
      )}
      {loading && <p>Loading…</p>}

      {/* Account picker */}
      <section>
        <h2>1. Select account</h2>
        {!userId ? (
          <ul>
            {accounts.map((a) => (
              <li key={a.id}>
                <button type="button" onClick={() => setUserId(a.id)}>
                  {a.name} ({a.email}) — {a.roles?.map((r) => r.name).join(', ')}
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p>
            Signed in as <strong>{session?.user?.name || userId}</strong>
            {session?.is_admin ? ' [admin]' : ''}{' '}
            <button type="button" onClick={logout}>
              Switch user
            </button>
          </p>
        )}
      </section>

      {session && (
        <>
          <nav>
            <button type="button" onClick={() => setTab('chat')}>
              Chat
            </button>
            {session.is_admin && (
              <button
                type="button"
                onClick={() => {
                  setTab('admin');
                  loadAdmin();
                }}
              >
                Admin
              </button>
            )}
          </nav>

          {tab === 'chat' && (
            <section>
              <h2>2. Chat</h2>

              <label>
                Project:{' '}
                <select
                  value={projectId}
                  onChange={(e) => {
                    setProjectId(e.target.value);
                    setActiveConversation(null);
                  }}
                >
                  <option value="">— select —</option>
                  {session.projects?.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>

              <div style={{ marginTop: 8 }}>
                <button type="button" onClick={() => loadConversations()}>
                  Refresh conversations
                </button>{' '}
                <button type="button" onClick={createConversation} disabled={!projectId}>
                  New conversation
                </button>
              </div>

              <h3>Conversations</h3>
              <ul>
                {conversations.map((c) => (
                  <li key={c.id}>
                    <button type="button" onClick={() => openConversation(c.id)}>
                      {c.title || 'Untitled'} — {c.project_name}
                    </button>{' '}
                    <button type="button" onClick={() => deleteConversation(c.id)}>
                      Delete
                    </button>
                  </li>
                ))}
              </ul>

              {activeConversation && (
                <div>
                  <h3>{activeConversation.title || 'Conversation'}</h3>
                  <div>
                    {(activeConversation.messages || []).map((m) => (
                      <div key={m.id} style={{ marginBottom: 12, borderBottom: '1px solid #ccc' }}>
                        <strong>{m.role}</strong>
                        {m.is_placeholder ? ' (placeholder)' : ''}
                        <pre style={{ whiteSpace: 'pre-wrap', margin: '4px 0' }}>{m.content}</pre>
                        {m.citations?.length > 0 && (
                          <details>
                            <summary>Citations ({m.citations.length})</summary>
                            <ul>
                              {m.citations.map((c) => (
                                <li key={c.id}>
                                  [{c.kind}] {c.source_ref}
                                  {c.quoted_span ? `: ${c.quoted_span}` : ''}
                                </li>
                              ))}
                            </ul>
                          </details>
                        )}
                      </div>
                    ))}
                  </div>

                  <form onSubmit={sendMessage}>
                    <textarea
                      rows={3}
                      style={{ width: '100%' }}
                      value={messageText}
                      onChange={(e) => setMessageText(e.target.value)}
                      placeholder="Ask something…"
                    />
                    <button type="submit" disabled={!messageText.trim() || loading}>
                      Send
                    </button>
                  </form>
                </div>
              )}
            </section>
          )}

          {tab === 'admin' && session.is_admin && (
            <section>
              <h2>Admin</h2>
              <button type="button" onClick={loadAdmin}>
                Refresh admin data
              </button>

              <h3>Roles ({adminRoles.length})</h3>
              <ul>
                {adminRoles.map((r) => (
                  <li key={r.id}>
                    {r.name} {r.is_admin ? '[admin]' : ''} — users: {r.user_count} — projects:{' '}
                    {r.projects?.map((p) => p.name).join(', ') || 'none'}
                  </li>
                ))}
              </ul>

              <h3>Projects ({adminProjects.length})</h3>
              <ul>
                {adminProjects.map((p) => (
                  <li key={p.id}>
                    {p.name} [{p.status}] {p.provider} — artifacts: {p.artifact_count}{' '}
                    <button
                      type="button"
                      onClick={async () => {
                        try {
                          await api.admin.reindexProject(userId, p.id);
                          loadAdmin();
                        } catch (e) {
                          setError(e.message);
                        }
                      }}
                    >
                      Reindex
                    </button>
                  </li>
                ))}
              </ul>

              <h3>Users ({adminUsers.length})</h3>
              <ul>
                {adminUsers.map((u) => (
                  <li key={u.id}>
                    {u.name} ({u.email}) — {u.roles?.map((r) => r.name).join(', ')}
                    {!u.is_active ? ' [inactive]' : ''}
                  </li>
                ))}
              </ul>

              <h3>Audit (latest)</h3>
              <ul>
                {adminAudit.map((a) => (
                  <li key={a.id}>
                    {a.occurred_at}: {a.actor_name} {a.action} {a.entity_type} — {a.detail}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}
