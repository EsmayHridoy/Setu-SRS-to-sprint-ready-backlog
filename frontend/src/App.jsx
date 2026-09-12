import { useEffect, useRef, useState } from 'react';
import { api, setToken, getToken } from './api';
import setuLogo from './assets/setu_logo.svg';

export default function App() {
  const [session, setSession] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const [projectId, setProjectId] = useState('');
  const [conversations, setConversations] = useState([]);
  const [activeConversation, setActiveConversation] = useState(null);
  const [messageText, setMessageText] = useState('');
  const [attachment, setAttachment] = useState(null);
  const [streaming, setStreaming] = useState(false);
  const [tab, setTab] = useState('chat');
  const [showChangePwd, setShowChangePwd] = useState(false);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const userMenuRef = useRef(null);

  // Admin state
  const [adminRoles, setAdminRoles] = useState([]);
  const [adminProjects, setAdminProjects] = useState([]);
  const [adminUsers, setAdminUsers] = useState([]);
  const [adminAudit, setAdminAudit] = useState([]);

  const threadEndRef = useRef(null);
  const abortRef = useRef(null);
  const streamingRef = useRef(false);

  // On mount, try to restore session from a stored token.
  useEffect(() => {
    if (!getToken()) {
      return;
    }
    api.getSession()
      .then((s) => {
        setSession(s);
        if (s.projects?.length) setProjectId(s.projects[0].id);
      })
      .catch(() => {
        setToken('');
      });
  }, []);

  useEffect(() => {
    if (session && projectId) loadConversations(projectId);
  }, [session, projectId]);

  // Close user menu when clicking outside
  useEffect(() => {
    if (!showUserMenu) return;
    function handle(e) {
      if (userMenuRef.current && !userMenuRef.current.contains(e.target)) {
        setShowUserMenu(false);
      }
    }
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [showUserMenu]);

  const msgs = activeConversation?.messages;
  const lastMsg = msgs?.[msgs.length - 1];
  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [msgs?.length, lastMsg?.content]);

  async function loadConversations(pid = projectId) {
    if (!session) return;
    setError('');
    try {
      const list = await api.listConversations(pid || undefined);
      setConversations(list);
    } catch (e) {
      setError(e.message);
    }
  }

  async function openConversation(id) {
    stopStreaming();
    setError('');
    try {
      const detail = await api.getConversation(id);
      setActiveConversation(detail);
    } catch (e) {
      setError(e.message);
    }
  }

  async function createConversation() {
    if (!projectId) { setError('Select a project first'); return; }
    setError('');
    try {
      const conv = await api.createConversation(projectId);
      await loadConversations();
      await openConversation(conv.id);
    } catch (e) {
      setError(e.message);
    }
  }

  async function deleteConversation(id) {
    setError('');
    try {
      await api.deleteConversation(id);
      if (activeConversation?.id === id) setActiveConversation(null);
      await loadConversations();
    } catch (e) {
      setError(e.message);
    }
  }

  function attachFile(file) {
    if (!file) return;
    setError('');
    setAttachment({ file, filename: file.name, size: file.size });
  }

  function stopStreaming() {
    abortRef.current?.abort();
  }

  async function doStream(convId, text, att) {
    const tempUserId = `tmp-u-${crypto.randomUUID()}`;
    const tempReplyId = `tmp-a-${crypto.randomUUID()}`;
    const userContent = att
      ? `[Uploaded document: ${att.filename}]${text ? `\n\n${text}` : ''}`
      : text;

    setError('');
    streamingRef.current = true;
    setStreaming(true);

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    setActiveConversation((prev) => {
      if (!prev || prev.id !== convId) return prev;
      return {
        ...prev,
        messages: [
          ...(prev.messages || []),
          { id: tempUserId, role: 'USER', content: userContent, citations: [] },
          { id: tempReplyId, role: 'ASSISTANT', content: '', citations: [], streaming: true },
        ],
      };
    });

    const patch = (id, fn) =>
      setActiveConversation((prev) => {
        if (!prev || prev.id !== convId) return prev;
        return { ...prev, messages: prev.messages.map((m) => (m.id === id ? fn(m) : m)) };
      });

    const handlers = {
      onStart: (data) => patch(tempUserId, () => data.user_message),
      onDelta: (chunk) => patch(tempReplyId, (m) => ({ ...m, content: m.content + chunk })),
      // What the agent is doing while it works. Kept on the in-flight reply
      // only: the saved reply from `done` replaces it without them.
      onEvent: (event, data) => {
        if (event === 'status') {
          patch(tempReplyId, (m) => ({ ...m, steps: [...(m.steps || []), data.text] }));
        }
      },
      onDone: (data) => {
        patch(tempReplyId, () => ({ ...data.reply, streaming: false }));
        setActiveConversation((prev) =>
          prev && prev.id === convId ? { ...prev, ...data.conversation } : prev,
        );
      },
    };

    try {
      if (att) await api.streamUpload(convId, att.file, text, handlers, ctrl.signal);
      else await api.streamMessage(convId, text, handlers, ctrl.signal);
      await loadConversations();
    } catch (err) {
      if (ctrl.signal.aborted) {
        patch(tempReplyId, (m) => ({
          ...m,
          content: m.content || 'Response was stopped before it could complete.',
          streaming: false,
        }));
      } else {
        setError(err.message);
        setActiveConversation((prev) => {
          if (!prev || prev.id !== convId) return prev;
          return { ...prev, messages: prev.messages.filter((m) => m.id !== tempReplyId) };
        });
      }
    } finally {
      abortRef.current = null;
      streamingRef.current = false;
      setStreaming(false);
    }
  }

  async function sendMessage(e) {
    e.preventDefault();
    if (!activeConversation || streamingRef.current) return;
    const typed = messageText.trim();
    const att = attachment;
    if (!typed && !att) return;
    setMessageText('');
    setAttachment(null);
    await doStream(activeConversation.id, typed, att);
  }

  async function loadAdmin() {
    if (!session?.is_admin) return;
    setError('');
    setLoading(true);
    try {
      const [roles, projects, users, audit] = await Promise.all([
        api.admin.listRoles(),
        api.admin.listProjects(),
        api.admin.listUsers(),
        api.admin.listAudit(50),
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
    setToken('');
    setSession(null);
    setActiveConversation(null);
    setConversations([]);
    setTab('chat');
    setError('');
  }

  function onComposerKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage(e);
    }
  }

  // ---------- Login screen ----------
  if (!session) {
    return (
      <LoginScreen
        loading={loading}
        error={error}
        onLogin={async (email, password) => {
          setLoading(true);
          setError('');
          try {
            const { access_token } = await api.login(email, password);
            setToken(access_token);
            const s = await api.getSession();
            setSession(s);
            if (s.projects?.length) setProjectId(s.projects[0].id);
          } catch (e) {
            setToken('');
            if (e.message.startsWith('429')) {
              setError('Too many login attempts. Please wait 5 minutes and try again.');
            } else {
              setError(e.message.replace(/^\d+: /, ''));
            }
          } finally {
            setLoading(false);
          }
        }}
      />
    );
  }

  const messages = activeConversation?.messages || [];

  return (
    <div className="layout">
      {/* ---------- Sidebar ---------- */}
      <aside className="sidebar">
        <div className="sidebar-top">
          <div className="brand">
            <div className="sidebar-logo-wrap">
              <img src={setuLogo} alt="Setu" className="sidebar-logo" />
            </div>
          </div>

          {tab !== 'admin' && (
            <>
              <button
                type="button"
                className="new-chat"
                onClick={createConversation}
                disabled={!projectId}
              >
                <PlusIcon />
                New chat
              </button>

              <label className="field">
                <span className="field-label">Project</span>
                <select
                  value={projectId}
                  onChange={(e) => {
                    setProjectId(e.target.value);
                    setActiveConversation(null);
                  }}
                >
                  <option value="">— select —</option>
                  {session?.projects?.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
            </>
          )}
        </div>

        {tab !== 'admin' && (
          <div className="conv-scroll">
            <div className="conv-heading">Chats</div>
            {conversations.length === 0 ? (
              <p className="empty">No conversations yet</p>
            ) : (
              <ul className="conv-list">
                {conversations.map((c) => (
                  <li
                    key={c.id}
                    className={activeConversation?.id === c.id ? 'active' : ''}
                  >
                    <button
                      type="button"
                      className="conv-open"
                      onClick={() => openConversation(c.id)}
                      title={c.title || 'Untitled'}
                    >
                      {c.title || 'Untitled'}
                    </button>
                    <button
                      type="button"
                      className="conv-del"
                      onClick={() => deleteConversation(c.id)}
                      title="Delete"
                    >
                      <TrashIcon />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {tab === 'admin' && <div style={{ flex: 1 }} />}

        <div className="sidebar-bottom">
          <div className="user-menu-wrap" ref={userMenuRef}>
            {showUserMenu && (
              <div className="user-menu">
                <div className="user-menu-header">
                  <span className="acct-avatar sm">
                    {(session?.user?.name || '?').charAt(0)}
                  </span>
                  <div className="user-menu-info">
                    <span className="user-menu-name">{session?.user?.name}</span>
                    <span className="user-menu-email">{session?.user?.email}</span>
                  </div>
                </div>
                <div className="user-menu-divider" />
                {session?.is_admin && (
                  <button
                    type="button"
                    className="user-menu-item"
                    onClick={() => {
                      setShowUserMenu(false);
                      if (tab === 'admin') { setTab('chat'); } else { setTab('admin'); loadAdmin(); }
                    }}
                  >
                    <GearIcon /> {tab === 'admin' ? 'Back to chat' : 'Admin panel'}
                  </button>
                )}
                <button
                  type="button"
                  className="user-menu-item"
                  onClick={() => { setShowUserMenu(false); setShowChangePwd(true); }}
                >
                  <LockIcon /> Change password
                </button>
                <div className="user-menu-divider" />
                <button
                  type="button"
                  className="user-menu-item danger"
                  onClick={logout}
                >
                  <SignOutIcon /> Sign out
                </button>
              </div>
            )}

            <button
              type="button"
              className={`user-chip-btn ${showUserMenu ? 'open' : ''}`}
              onClick={() => setShowUserMenu((v) => !v)}
            >
              <span className="acct-avatar sm">
                {(session?.user?.name || '?').charAt(0)}
              </span>
              <span className="user-chip-body">
                <span className="user-chip-name">
                  {session?.user?.name}
                  {session?.is_admin && <span className="badge">admin</span>}
                </span>
              </span>
              <ChevronIcon open={showUserMenu} />
            </button>
          </div>

          {showChangePwd && (
            <Modal title="Change password" onClose={() => setShowChangePwd(false)}>
              <ChangePasswordModal onClose={() => setShowChangePwd(false)} />
            </Modal>
          )}
        </div>
      </aside>

      {/* ---------- Main ---------- */}
      <main className="main">
        {error && <div className="alert error floating">{error}</div>}

        {tab === 'admin' && session?.is_admin ? (
          <AdminView
            loading={loading}
            onRefresh={loadAdmin}
            onError={setError}
            roles={adminRoles}
            projects={adminProjects}
            users={adminUsers}
            audit={adminAudit}
          />
        ) : activeConversation ? (
          <>
            <div className="thread">
              <div className="thread-inner">
                {messages.map((m) => (
                  <Message
                    key={m.id}
                    m={m}
                    onError={setError}
                    onGrow={() => threadEndRef.current?.scrollIntoView({ behavior: 'smooth' })}
                  />
                ))}
                <div ref={threadEndRef} />
              </div>
            </div>
            <Composer
              value={messageText}
              onChange={setMessageText}
              onSubmit={sendMessage}
              onKeyDown={onComposerKeyDown}
              disabled={streaming}
              streaming={streaming}
              onStop={stopStreaming}
              attachment={attachment}
              onAttach={attachFile}
              onRemoveAttach={() => setAttachment(null)}
            />
          </>
        ) : (
          <div className="welcome">
            <div className="welcome-inner">
              <div className="welcome-logo-wrap">
                <img src={setuLogo} alt="Setu" className="welcome-logo" />
              </div>
              <h2>How can I help you today?</h2>
              <p className="welcome-sub">
                {projectId
                  ? 'Start a new chat or pick a conversation from the sidebar.'
                  : 'Select a project to begin.'}
              </p>
              <Composer
                value={messageText}
                onChange={setMessageText}
                onSubmit={async (e) => {
                  e.preventDefault();
                  if (!projectId) { setError('Select a project first'); return; }
                  const text = messageText.trim();
                  const att = attachment;
                  if (!text && !att) return;
                  setMessageText('');
                  setAttachment(null);
                  setError('');
                  try {
                    const conv = await api.createConversation(projectId);
                    await loadConversations();
                    const detail = await api.getConversation(conv.id);
                    setActiveConversation(detail);
                    await doStream(conv.id, text, att);
                  } catch (ex) {
                    setError(ex.message);
                  }
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    e.target.closest('form')?.requestSubmit();
                  }
                }}
                disabled={!projectId || loading || streaming}
                placeholder={projectId ? 'Message Setu…' : 'Select a project first'}
                centered
                attachment={attachment}
                onAttach={attachFile}
                onRemoveAttach={() => setAttachment(null)}
              />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

// ---------- Login screen ----------

function LoginScreen({ loading, error, onLogin }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  async function handleSubmit(e) {
    e.preventDefault();
    await onLogin(email.trim(), password);
  }

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-logo-wrap">
          <img src={setuLogo} alt="Setu" className="login-logo" />
        </div>
        <p className="login-sub">Sign in to continue</p>
        {error && <div className="alert error">{error}</div>}
        <form onSubmit={handleSubmit} className="login-form">
          <label className="form-field">
            <span className="form-label">Email</span>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoFocus
              autoComplete="email"
              placeholder="you@example.com"
            />
          </label>
          <label className="form-field">
            <span className="form-label">Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
              placeholder="••••••••"
            />
          </label>
          <button type="submit" className="save-btn" disabled={loading}>
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}

// ---------- Message parsing ----------

function parseUserContent(content) {
  const up = content.match(/^\[Uploaded document: (.+?)\](?:\n\n)?/);
  if (up) {
    return { question: content.slice(up[0].length), doc: { filename: up[1] } };
  }
  return { question: content, doc: null };
}

function Message({ m, onError, onGrow }) {
  const isUser = m.role?.toLowerCase() === 'user';
  const isEmpty = !m.content;
  const parsed = isUser ? parseUserContent(m.content) : null;
  return (
    <div className={`msg ${isUser ? 'user' : 'assistant'}`}>
      {!isUser && (
        <div className="msg-avatar">
          <img src="/favicon.svg" alt="Setu" className="msg-avatar-icon" />
        </div>
      )}
      <div className="msg-body">
        {!isUser && <AgentSteps steps={m.steps} active={Boolean(m.streaming) && isEmpty} />}
        {isUser ? (
          <div className="bubble">
            {parsed.doc && (
              <div className="doc-tag">
                <PaperclipIcon />
                <span>{parsed.doc.filename}</span>
              </div>
            )}
            {parsed.question && <div className="bubble-text">{parsed.question}</div>}
          </div>
        ) : m.streaming && isEmpty ? (
          !m.steps?.length && (
            <div className="typing">
              <span></span>
              <span></span>
              <span></span>
            </div>
          )
        ) : m.business_plan_id && !m.streaming ? (
          <BusinessPlanCard planId={m.business_plan_id} onError={onError} onGrow={onGrow} />
        ) : (
          <>
            <div className="msg-text">
              {m.content}
              {m.streaming && <span className="caret" />}
              {m.is_placeholder && !m.streaming && <span className="pill"> placeholder</span>}
            </div>
            {m.citations?.length > 0 && (
              <details className="citations">
                <summary>{m.citations.length} citations</summary>
                <ul>
                  {m.citations.map((c) => (
                    <li key={c.id}>
                      <span className="pill">{c.kind}</span> {c.source_ref}
                      {c.quoted_span ? `: ${c.quoted_span}` : ''}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </>
        )}
      </div>
    </div>
  );
}

const VISIBLE_STEPS = 4;
const STEPS_FADE_MS = 300;

// The agent's progress lines, shown in muted text while it works and faded
// out once the answer starts. Never stored: they live only on the in-flight
// reply, so a reload or the saved reply leaves no trace of them.
function AgentSteps({ steps, active }) {
  const [wasActive, setWasActive] = useState(active);
  const [fading, setFading] = useState(false);
  if (wasActive !== active) {
    setWasActive(active);
    setFading(!active);
  }

  useEffect(() => {
    if (!fading) return undefined;
    const timer = setTimeout(() => setFading(false), STEPS_FADE_MS);
    return () => clearTimeout(timer);
  }, [fading]);

  if (!(active || fading) || !steps?.length) return null;
  const first = Math.max(0, steps.length - VISIBLE_STEPS);
  return (
    <ul className={`agent-steps${active ? '' : ' leaving'}`} aria-live="polite">
      {steps.slice(first).map((step, i) => (
        <li key={first + i} className={active && first + i === steps.length - 1 ? 'current' : ''}>
          {step}
        </li>
      ))}
    </ul>
  );
}

const PLAN_STATUS = {
  DRAFT: 'Needs your review',
  CONFIRMED: 'Vetting',
  DONE: 'Vetted',
  DISCARDED: 'Discarded',
};

let draftKey = 0;
const toDraft = (items) =>
  items.map((i) => ({ key: i.id, description: i.description, location: i.location }));

function BusinessPlanCard({ planId, onError, onGrow }) {
  const [plan, setPlan] = useState(null);
  const [draft, setDraft] = useState([]);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [vetting, setVetting] = useState(false);
  const [runningId, setRunningId] = useState(null);
  const [runningStep, setRunningStep] = useState('');
  const [editingId, setEditingId] = useState(null);
  const abortRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    api.business
      .getPlan(planId)
      .then((p) => {
        if (cancelled) return;
        setPlan(p);
        setDraft(toDraft(p.items));
      })
      .catch((e) => onError(e.message));
    return () => {
      cancelled = true;
      abortRef.current?.abort();
    };
  }, [planId, onError]);

  if (!plan) {
    return (
      <div className="typing">
        <span></span>
        <span></span>
        <span></span>
      </div>
    );
  }

  const editItem = (key, description) => {
    setDraft((d) => d.map((it) => (it.key === key ? { ...it, description } : it)));
    setDirty(true);
  };
  const removeItem = (key) => {
    setDraft((d) => d.filter((it) => it.key !== key));
    setDirty(true);
  };
  const addItem = () => {
    setDraft((d) => [...d, { key: `new-${++draftKey}`, description: '', location: '' }]);
    setDirty(true);
  };
  const kept = draft.filter((it) => it.description.trim());

  async function startVetting() {
    const controller = new AbortController();
    abortRef.current = controller;
    setVetting(true);
    try {
      await api.business.streamVetting(
        planId,
        {
          onEvent: (event, data) => {
            if (event === 'item_start') {
              setRunningId(data.item_id);
              setRunningStep('');
            } else if (event === 'item_status') {
              setRunningStep(data.text);
            } else if (event === 'item_result') {
              setRunningId(null);
              setRunningStep('');
              setPlan((p) => ({ ...p, items: p.items.map((i) => (i.id === data.id ? data : i)) }));
              onGrow();
            }
          },
          onDone: (data) => setPlan((p) => ({ ...p, ...data })),
        },
        controller.signal,
      );
    } catch (e) {
      if (e.name !== 'AbortError') onError(e.message);
    } finally {
      setVetting(false);
      setRunningId(null);
      setRunningStep('');
    }
  }

  // Dropping the stream stops the server too: the item in progress is left
  // PENDING, so Resume vets it again and carries on with the rest.
  function stopVetting() {
    abortRef.current?.abort();
  }

  async function savePendingItem(itemId, description) {
    onError('');
    try {
      const updated = await api.business.updatePendingItem(planId, itemId, description);
      setPlan((p) => ({ ...p, items: p.items.map((i) => (i.id === updated.id ? updated : i)) }));
      setEditingId(null);
    } catch (e) {
      onError(e.message);
    }
  }

  async function confirm() {
    setBusy(true);
    onError('');
    try {
      let current = plan;
      if (dirty) {
        current = await api.business.updateItems(
          planId,
          kept.map(({ description, location }) => ({ description: description.trim(), location })),
        );
        setDraft(toDraft(current.items));
        setDirty(false);
      }
      const confirmed = await api.business.confirm(planId);
      setPlan({ ...current, ...confirmed });
    } catch (e) {
      onError(e.message);
      return;
    } finally {
      setBusy(false);
    }
    startVetting();
  }

  async function discard() {
    setBusy(true);
    onError('');
    try {
      const discarded = await api.business.discard(planId);
      setPlan((p) => ({ ...p, ...discarded }));
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const vettedCount = plan.items.filter((i) => i.vetting_status !== 'PENDING').length;
  const hasVetted = plan.items.some((i) => i.vetting_status === 'DONE');
  const paused = plan.status === 'CONFIRMED' && !vetting;

  return (
    <div className="plan-card">
      <div className="plan-head">
        <PaperclipIcon />
        <span className="plan-file" title={plan.source_filename}>
          {plan.source_filename}
        </span>
        <span className={`plan-status ${paused ? 'paused' : plan.status.toLowerCase()}`}>
          {paused ? 'Paused' : PLAN_STATUS[plan.status]}
        </span>
      </div>

      {plan.status === 'DRAFT' ? (
        <>
          <p className="plan-intro">
            {plan.items.length
              ? `I found ${plan.items.length} business requirement${plan.items.length === 1 ? '' : 's'}. Is this list right? Edit, remove or add any, then confirm to vet each one against the codebase.`
              : 'I could not find distinct business requirements in this document. Add them below, then confirm to vet them against the codebase.'}
          </p>
          <ol className="plan-edit-list">
            {draft.map((it, idx) => (
              <li key={it.key}>
                <div className="plan-edit-body">
                  <span className="vet-no">BR-{idx + 1}</span>
                  <textarea
                    rows={2}
                    value={it.description}
                    placeholder="Describe the business requirement…"
                    onChange={(e) => editItem(it.key, e.target.value)}
                    disabled={busy}
                  />
                  {it.location && <span className="plan-loc">{it.location}</span>}
                </div>
                <button
                  type="button"
                  className="plan-remove"
                  onClick={() => removeItem(it.key)}
                  disabled={busy}
                  title="Remove"
                >
                  ✕
                </button>
              </li>
            ))}
          </ol>
          <button type="button" className="plan-add" onClick={addItem} disabled={busy}>
            <PlusIcon /> Add a business
          </button>
          <div className="plan-actions">
            <button type="button" className="ghost-btn" onClick={discard} disabled={busy}>
              No, discard
            </button>
            <button type="button" className="save-btn" onClick={confirm} disabled={busy || !kept.length}>
              {busy ? 'Starting…' : `Yes, vet ${kept.length}`}
            </button>
          </div>
        </>
      ) : plan.status === 'DISCARDED' ? (
        <p className="plan-intro muted">Discarded — nothing from this document was vetted.</p>
      ) : (
        <>
          <p className="plan-intro">
            {plan.status === 'DONE'
              ? `All ${plan.items.length} vetted against the codebase.`
              : paused
                ? `Paused — ${vettedCount} of ${plan.items.length} vetted. You can edit any business still waiting, then resume to carry on from where it stopped.`
                : `Vetting one at a time — ${vettedCount} of ${plan.items.length} done.`}
          </p>
          <ol className="vet-list">
            {plan.items.map((item) => (
              <VettedItem
                key={item.id}
                planId={planId}
                item={item}
                running={runningId === item.id}
                step={runningId === item.id ? runningStep : ''}
                editable={paused && item.vetting_status === 'PENDING' && !editingId}
                editing={paused && editingId === item.id}
                onEdit={() => setEditingId(item.id)}
                onCancelEdit={() => setEditingId(null)}
                onSave={(description) => savePendingItem(item.id, description)}
                onItemUpdated={(updated) =>
                  setPlan((p) => ({ ...p, items: p.items.map((i) => (i.id === updated.id ? updated : i)) }))
                }
              />
            ))}
          </ol>
          {vetting && (
            <div className="plan-actions">
              <button type="button" className="ghost-btn icon-btn" onClick={stopVetting}>
                <StopIcon /> Stop vetting
              </button>
            </div>
          )}
          {paused && (
            <div className="plan-actions">
              <button
                type="button"
                className="save-btn"
                onClick={startVetting}
                disabled={Boolean(editingId)}
                title={editingId ? 'Save or cancel your edit first' : undefined}
              >
                Resume vetting
              </button>
            </div>
          )}
          {plan.status === 'DONE' && hasVetted && (
            <SrsStep
              planId={planId}
              srs={plan.srs}
              onSrs={(srs) => setPlan((p) => ({ ...p, srs }))}
              onError={onError}
              onGrow={onGrow}
            />
          )}
        </>
      )}
    </div>
  );
}

// The step after vetting: offer to write the vetted stories into the user's
// own SRS format. Nothing here is required -- "no" leaves the plan exactly as
// it was, and the answer is remembered only for this render, not stored.
//
// Once a format has been uploaded the plan itself carries the SRS state
// (BusinessPlanDetail.srs), so reloading the chat lands back on the download
// rather than on the question again.
function SrsStep({ planId, srs, onSrs, onError, onGrow }) {
  const [asked, setAsked] = useState(false);
  const [declined, setDeclined] = useState(false);
  const [busy, setBusy] = useState('');
  const fileRef = useRef(null);

  async function run(kind, work) {
    setBusy(kind);
    onError('');
    try {
      onSrs(await work());
      onGrow();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy('');
    }
  }

  function pickFile(e) {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (file) run('upload', () => api.business.uploadSrsFormat(planId, file));
  }

  async function download() {
    setBusy('download');
    onError('');
    try {
      await api.business.downloadSrs(planId);
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy('');
    }
  }

  const picker = (
    <input
      ref={fileRef}
      type="file"
      accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
      hidden
      onChange={pickFile}
    />
  );
  const choose = (label) => (
    <button
      type="button"
      className="save-btn"
      onClick={() => fileRef.current?.click()}
      disabled={Boolean(busy)}
    >
      {busy === 'upload' ? 'Writing your SRS…' : label}
    </button>
  );

  // Nothing uploaded yet: ask, then show the picker.
  if (!srs) {
    if (declined) {
      return (
        <div className="srs-step">
          <p className="plan-intro muted">
            No SRS format used. The vetted stories are above.{' '}
            <button type="button" className="link-btn" onClick={() => setDeclined(false)}>
              Upload a format after all
            </button>
          </p>
        </div>
      );
    }
    return (
      <div className="srs-step">
        <p className="plan-intro">
          {asked
            ? 'Choose your SRS format — a Word .docx. Each vetted story is written into the section of it that fits.'
            : 'Do you want these written into your own SRS format?'}
        </p>
        {picker}
        <div className="plan-actions">
          {asked ? (
            <>
              <button
                type="button"
                className="ghost-btn"
                onClick={() => setAsked(false)}
                disabled={Boolean(busy)}
              >
                Back
              </button>
              {choose('Choose a .docx…')}
            </>
          ) : (
            <>
              <button type="button" className="ghost-btn" onClick={() => setDeclined(true)}>
                No, thanks
              </button>
              <button type="button" className="save-btn" onClick={() => setAsked(true)}>
                Yes, upload SRS format
              </button>
            </>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="srs-step">
      <div className="srs-head">
        <PaperclipIcon />
        <span className="plan-file" title={srs.template_filename}>
          {srs.template_filename}
        </span>
      </div>

      {srs.status === 'READY' ? (
        <>
          <p className="plan-intro">
            Your SRS is ready — {srs.placements.length} vetted{' '}
            {srs.placements.length === 1 ? 'story' : 'stories'} written into it.
          </p>
          {srs.notice && <p className="srs-notice">{srs.notice}</p>}
          {Boolean(srs.placements.length) && (
            <ul className="srs-placements">
              {srs.placements.map((pl) => (
                <li key={pl.seq_no}>
                  <span className="srs-seq">Story {pl.seq_no}</span>
                  <span className="srs-arrow">→</span>
                  <span className="srs-section">{pl.section}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : srs.status === 'ERROR' ? (
        <p className="srs-error">{srs.error_message || 'The SRS could not be generated.'}</p>
      ) : (
        <p className="plan-intro">
          This format is saved but has not been written into yet.
        </p>
      )}

      {picker}
      <div className="plan-actions">
        {choose('Replace format')}
        {srs.status !== 'READY' && (
          <button
            type="button"
            className="save-btn"
            onClick={() => run('retry', () => api.business.regenerateSrs(planId))}
            disabled={Boolean(busy)}
          >
            {busy === 'retry' ? 'Writing your SRS…' : 'Try again'}
          </button>
        )}
        {srs.status === 'READY' && (
          <button
            type="button"
            className="save-btn icon-btn"
            onClick={download}
            disabled={Boolean(busy)}
          >
            <DownloadIcon /> {busy === 'download' ? 'Preparing…' : 'Download SRS'}
          </button>
        )}
      </div>
    </div>
  );
}

const yesNo = (v) => (v == null ? '—' : v ? 'Yes' : 'No');

// The model sometimes writes a numbered list ("1. Foo 2. Bar 3. Baz") as one
// run-on sentence instead of one point per line, so white-space: pre-wrap
// has no real newline to break on. Detect that shape from the punctuation
// alone -- a digit, a dot, a space, repeated -- and split it back into
// points regardless of whether the source had real line breaks.
function splitNumberedList(text) {
  if (!text) return null;
  const parts = text
    .split(/\s*(?=\d+\.\s)/g)
    .map((s) => s.trim())
    .filter(Boolean);
  if (parts.length < 2 || !parts.every((p) => /^\d+\.\s/.test(p))) return null;
  return parts.map((p) => p.replace(/^\d+\.\s*/, ''));
}

// The Scope field is written as "In scope: ... Out of scope: ..." -- same
// run-on problem as a numbered list, just a different shape. Split right
// before "Out of scope" regardless of whether the source had a real line
// break there.
function splitScopeStatement(text) {
  if (!text) return null;
  const match = text.match(/^(.*?)\s+(out[\s-]of[\s-]scope\s*:.*)$/is);
  if (!match) return null;
  const inPart = match[1].trim();
  const outPart = match[2].trim();
  return inPart && outPart ? [inPart, outPart] : null;
}

function NotesBlock({ text }) {
  const points = splitNumberedList(text);
  if (points) {
    return (
      <ol className="vet-notes-list">
        {points.map((p, i) => (
          <li key={i}>{p}</li>
        ))}
      </ol>
    );
  }
  const scoped = splitScopeStatement(text);
  if (scoped) {
    return (
      <div className="vet-notes-lines">
        {scoped.map((p, i) => (
          <p key={i}>{p}</p>
        ))}
      </div>
    );
  }
  return <p className="vet-notes">{text}</p>;
}

// Rewording a business that is still waiting to be vetted, while vetting is
// paused. Resume then vets the new wording.
function PendingItemEditor({ initial, onSave, onCancel }) {
  const [text, setText] = useState(initial);
  const [saving, setSaving] = useState(false);
  const trimmed = text.trim();
  const rows = Math.min(12, Math.max(3, text.split('\n').length));

  async function save() {
    setSaving(true);
    try {
      await onSave(trimmed);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="plan-edit-body vet-editor">
      <textarea
        rows={rows}
        value={text}
        placeholder="Describe the business requirement…"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === 'Escape' && !saving && onCancel()}
        disabled={saving}
        autoFocus
      />
      <div className="vet-editor-actions">
        <button type="button" className="ghost-btn" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
        <button
          type="button"
          className="save-btn"
          onClick={save}
          disabled={saving || !trimmed || trimmed === initial.trim()}
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  );
}

function VettedItem({ planId, item, running, step, editable, editing, onEdit, onCancelEdit, onSave, onItemUpdated }) {
  const state = running ? 'running' : item.vetting_status.toLowerCase();
  const label = { running: 'Vetting…', pending: 'Waiting', done: 'Done', error: 'Failed' }[state];
  return (
    <li className={`vet-item ${state}`}>
      <div className="vet-head">
        <span className="vet-no">BR-{item.seq_no}</span>
        {editing ? (
          <PendingItemEditor initial={item.description} onSave={onSave} onCancel={onCancelEdit} />
        ) : (
          <span className="vet-desc">
            {item.description}
            {item.location && <span className="plan-loc">{item.location}</span>}
          </span>
        )}
        {editable && (
          <button type="button" className="vet-edit-btn" onClick={onEdit} title="Edit this business">
            Edit
          </button>
        )}
        <span className={`vet-state ${state}`}>{label}</span>
      </div>
      {running && step && (
        <p className="vet-step" aria-live="polite">
          {step}
        </p>
      )}
      {item.vetting_status === 'DONE' && (
        <div className="vet-body">
          <p className="vet-verdict">{item.verdict}</p>

          <div className="vet-flags">
            <span className="pill">Requirement clear: {yesNo(item.is_requirement_clear)}</span>
            <span className="pill">Feasible: {yesNo(item.is_feasible)}</span>
            <span className="pill">Already supported: {yesNo(item.already_supported)}</span>
            <span className={`pill approval ${item.is_approved ? 'approved' : 'pending'}`}>
              {item.is_approved ? 'Approved' : 'Awaiting approval'}
            </span>
          </div>

          {item.user_story && (
            <>
              <div className="vet-label">User Story</div>
              <NotesBlock text={item.user_story} />
            </>
          )}
          {item.actors && (
            <>
              <div className="vet-label">Actors</div>
              <NotesBlock text={item.actors} />
            </>
          )}
          {item.scope && (
            <>
              <div className="vet-label">Scope</div>
              <NotesBlock text={item.scope} />
            </>
          )}
          {item.pre_condition && (
            <>
              <div className="vet-label">Pre-condition</div>
              <NotesBlock text={item.pre_condition} />
            </>
          )}
          {item.impacted_areas && (
            <>
              <div className="vet-label">Impacted Areas</div>
              <NotesBlock text={item.impacted_areas} />
            </>
          )}
          {item.requirements && (
            <>
              <div className="vet-label">Requirements</div>
              <NotesBlock text={item.requirements} />
            </>
          )}
          {item.acceptance_criteria && (
            <>
              <div className="vet-label">Acceptance Criteria</div>
              <NotesBlock text={item.acceptance_criteria} />
            </>
          )}
          {item.exceptions && (
            <>
              <div className="vet-label">Exceptions</div>
              <NotesBlock text={item.exceptions} />
            </>
          )}

          <ItemDiscussion planId={planId} item={item} onItemUpdated={onItemUpdated} />
        </div>
      )}
      {item.vetting_status === 'ERROR' && <p className="vet-error">{item.error_message}</p>}
    </li>
  );
}

function ItemDiscussion({ planId, item, onItemUpdated }) {
  const [open, setOpen] = useState(false);
  const [comments, setComments] = useState(null); // null = not loaded yet
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [approving, setApproving] = useState(false);
  const [error, setError] = useState('');
  const listRef = useRef(null);

  useEffect(() => {
    if (!open || comments !== null) return;
    let cancelled = false;
    api.business
      .getItemComments(planId, item.id)
      .then((rows) => {
        if (!cancelled) setComments(rows);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [open, planId, item.id, comments]);

  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [comments]);

  async function send() {
    const content = draft.trim();
    if (!content || sending) return;
    setSending(true);
    setError('');
    try {
      const result = await api.business.postItemComment(planId, item.id, content);
      setComments((c) => [...(c || []), result.comment, result.reply]);
      setDraft('');
      onItemUpdated(result.item);
    } catch (e) {
      setError(e.message);
    } finally {
      setSending(false);
    }
  }

  async function approve() {
    setApproving(true);
    setError('');
    try {
      const updated = await api.business.approveItem(planId, item.id);
      onItemUpdated(updated);
    } catch (e) {
      setError(e.message);
    } finally {
      setApproving(false);
    }
  }

  return (
    <div className="item-discussion">
      <div className="item-discussion-actions">
        <button type="button" className="ghost-btn" onClick={() => setOpen((o) => !o)}>
          {open ? 'Hide discussion' : 'Discuss this'}
        </button>
        {!item.is_approved && (
          <button type="button" className="save-btn" onClick={approve} disabled={approving}>
            {approving ? 'Approving…' : 'Approve'}
          </button>
        )}
      </div>

      {open && (
        <div className="item-thread">
          <div className="item-thread-list" ref={listRef}>
            {comments === null && !error && <p className="item-thread-empty">Loading…</p>}
            {comments?.length === 0 && <p className="item-thread-empty">No discussion yet -- ask a question or give new information.</p>}
            {comments?.map((c) => (
              <div key={c.id} className={`item-comment ${c.role.toLowerCase()}`}>
                <span className="item-comment-role">{c.role === 'USER' ? 'You' : 'Setu'}</span>
                <p>{c.content}</p>
                {c.changed_verdict && <span className="pill changed">Verdict updated</span>}
              </div>
            ))}
          </div>

          {item.is_approved ? (
            <p className="item-thread-locked">Approved -- this discussion is now read-only.</p>
          ) : (
            <div className="item-thread-input">
              <textarea
                rows={2}
                value={draft}
                placeholder="Ask a question, or give new information that should change the verdict…"
                onChange={(e) => setDraft(e.target.value)}
                disabled={sending}
              />
              <button type="button" className="save-btn" onClick={send} disabled={sending || !draft.trim()}>
                {sending ? 'Thinking…' : 'Send'}
              </button>
            </div>
          )}
          {error && <p className="vet-error">{error}</p>}
        </div>
      )}
    </div>
  );
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function Composer({
  value,
  onChange,
  onSubmit,
  onKeyDown,
  disabled,
  placeholder,
  centered,
  attachment,
  onAttach,
  onRemoveAttach,
  streaming,
  onStop,
}) {
  const fileRef = useRef(null);
  const canSend = (value.trim() || attachment) && !disabled;

  function pickFile(e) {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (file) onAttach(file);
  }

  return (
    <div className={`composer-wrap ${centered ? 'centered' : ''}`}>
      {attachment && (
        <div className="attach-chip">
          <PaperclipIcon />
          <span className="attach-name" title={attachment.filename}>
            {attachment.filename}
          </span>
          <span className="attach-meta">{formatBytes(attachment.size)}</span>
          <button type="button" className="attach-remove" onClick={onRemoveAttach} title="Remove">
            ✕
          </button>
        </div>
      )}
      <form className="composer" onSubmit={onSubmit}>
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          hidden
          onChange={pickFile}
        />
        <button
          type="button"
          className="attach-btn"
          onClick={() => fileRef.current?.click()}
          disabled={disabled}
          title="Attach a PDF or DOCX"
        >
          <PaperclipIcon />
        </button>
        <textarea
          rows={1}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={placeholder || 'Message Setu…'}
        />
        {streaming && onStop ? (
          <button type="button" className="stop-btn" onClick={onStop} title="Stop generating">
            <StopIcon />
          </button>
        ) : (
          <button type="submit" className="send-btn" disabled={!canSend} title="Send">
            <ArrowUpIcon />
          </button>
        )}
      </form>
      <p className="composer-hint">Attach a PDF/DOCX · Enter to send · Shift+Enter for a new line</p>
    </div>
  );
}

function ChangePasswordModal({ onClose }) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  async function handle(e) {
    e.preventDefault();
    setErr('');
    if (next !== confirm) { setErr('New passwords do not match.'); return; }
    if (next.length < 6) { setErr('New password must be at least 6 characters.'); return; }
    setBusy(true);
    try {
      await api.changePassword(current, next);
      onClose();
    } catch (ex) {
      setErr(ex.message.replace(/^\d+: /, ''));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={handle} className="modal-form">
      <label className="form-field">
        <span className="form-label">Current password</span>
        <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)}
          required autoFocus autoComplete="current-password" />
      </label>
      <label className="form-field">
        <span className="form-label">New password <em>(min 6 characters)</em></span>
        <input type="password" value={next} onChange={(e) => setNext(e.target.value)}
          required autoComplete="new-password" />
      </label>
      <label className="form-field">
        <span className="form-label">Confirm new password</span>
        <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)}
          required autoComplete="new-password" />
      </label>
      {err && <div className="alert error">{err}</div>}
      <div className="modal-actions">
        <button type="button" className="ghost-btn" onClick={onClose}>Cancel</button>
        <button type="submit" className="save-btn" disabled={busy}>
          {busy ? 'Saving…' : 'Change password'}
        </button>
      </div>
    </form>
  );
}

function ResetPasswordModal({ user, onClose }) {
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  async function handle(e) {
    e.preventDefault();
    setErr('');
    if (next !== confirm) { setErr('Passwords do not match.'); return; }
    if (next.length < 6) { setErr('Password must be at least 6 characters.'); return; }
    setBusy(true);
    try {
      await api.admin.resetPassword(user.id, next);
      onClose();
    } catch (ex) {
      setErr(ex.message.replace(/^\d+: /, ''));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={handle} className="modal-form">
      <p className="modal-desc">
        Reset password for <strong>{user.name}</strong>. The previous password is not required.
      </p>
      <label className="form-field">
        <span className="form-label">New password <em>(min 6 characters)</em></span>
        <input type="password" value={next} onChange={(e) => setNext(e.target.value)}
          required autoFocus autoComplete="new-password" />
      </label>
      <label className="form-field">
        <span className="form-label">Confirm new password</span>
        <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)}
          required autoComplete="new-password" />
      </label>
      {err && <div className="alert error">{err}</div>}
      <div className="modal-actions">
        <button type="button" className="ghost-btn" onClick={onClose}>Cancel</button>
        <button type="submit" className="save-btn" disabled={busy}>
          {busy ? 'Saving…' : 'Reset password'}
        </button>
      </div>
    </form>
  );
}

function AdminView({ loading, onRefresh, onError, roles, projects, users, audit }) {
  const [modal, setModal] = useState(null);
  const [resetTarget, setResetTarget] = useState(null);
  const [busy, setBusy] = useState(false);

  const close = () => setModal(null);

  async function remove(kind, entity) {
    const label = entity.name || entity.email;
    if (!window.confirm(`Delete ${kind} "${label}"? This cannot be undone.`)) return;
    onError('');
    try {
      if (kind === 'role') await api.admin.deleteRole(entity.id);
      else if (kind === 'project') await api.admin.deleteProject(entity.id);
      else await api.admin.deleteUser(entity.id);
      await onRefresh();
    } catch (e) {
      onError(e.message);
    }
  }

  async function reindex(id) {
    onError('');
    try {
      await api.admin.reindexProject(id);
      await onRefresh();
    } catch (e) {
      onError(e.message);
    }
  }

  async function submit(payload) {
    setBusy(true);
    try {
      const { type, entity } = modal;
      const id = entity?.id;
      if (type === 'role') {
        if (id) await api.admin.updateRole(id, payload);
        else await api.admin.createRole(payload);
      } else if (type === 'project') {
        if (id) await api.admin.updateProject(id, payload);
        else await api.admin.createProject(payload);
      } else {
        if (id) await api.admin.updateUser(id, payload);
        else await api.admin.createUser(payload);
      }
      close();
      await onRefresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="admin">
      <div className="admin-inner">
        <div className="admin-head">
          <h2>Admin</h2>
          <button type="button" onClick={onRefresh} disabled={loading}>
            Refresh
          </button>
        </div>

        <div className="section-head">
          <h3>Roles ({roles.length})</h3>
          <button type="button" className="new-btn" onClick={() => setModal({ type: 'role', entity: null })}>
            <PlusIcon /> New role
          </button>
        </div>
        <ul className="data-list">
          {roles.map((r) => (
            <li key={r.id}>
              <span className="grow">
                <strong>{r.name}</strong> {r.is_admin && <span className="badge">admin</span>} —
                users: {r.user_count} — projects:{' '}
                {r.projects?.map((p) => p.name).join(', ') || 'none'}
              </span>
              <button type="button" onClick={() => setModal({ type: 'role', entity: r })}>
                Edit
              </button>
              <button type="button" className="danger-btn" onClick={() => remove('role', r)}>
                Delete
              </button>
            </li>
          ))}
        </ul>

        <div className="section-head">
          <h3>Projects ({projects.length})</h3>
          <button type="button" className="new-btn" onClick={() => setModal({ type: 'project', entity: null })}>
            <PlusIcon /> New project
          </button>
        </div>
        <ul className="data-list">
          {projects.map((p) => (
            <li key={p.id}>
              <span className="grow">
                <strong>{p.name}</strong> <span className="pill">{p.status}</span> {p.provider} —
                artifacts: {p.artifact_count}
                {p.token_last4 && <span className="pill"> token ••{p.token_last4}</span>}
              </span>
              <button type="button" onClick={() => reindex(p.id)}>
                Reindex
              </button>
              <button type="button" onClick={() => setModal({ type: 'project', entity: p })}>
                Edit
              </button>
              <button type="button" className="danger-btn" onClick={() => remove('project', p)}>
                Delete
              </button>
            </li>
          ))}
        </ul>

        <div className="section-head">
          <h3>Users ({users.length})</h3>
          <button type="button" className="new-btn" onClick={() => setModal({ type: 'user', entity: null })}>
            <PlusIcon /> New user
          </button>
        </div>
        <ul className="data-list">
          {users.map((u) => (
            <li key={u.id}>
              <span className="grow">
                <strong>{u.name}</strong> ({u.email}) — {u.roles?.map((r) => r.name).join(', ') || 'no roles'}
              </span>
              {!u.is_active && <span className="badge muted">inactive</span>}
              <button type="button" onClick={() => setResetTarget(u)}>
                Reset pwd
              </button>
              <button type="button" onClick={() => setModal({ type: 'user', entity: u })}>
                Edit
              </button>
              <button type="button" className="danger-btn" onClick={() => remove('user', u)}>
                Delete
              </button>
            </li>
          ))}
        </ul>

        <h3>Audit (latest)</h3>
        <ul className="data-list audit">
          {audit.map((a) => (
            <li key={a.id}>
              {a.occurred_at}: {a.actor_name} {a.action} {a.entity_type} — {a.detail}
            </li>
          ))}
        </ul>
      </div>

      {modal && (
        <Modal
          title={`${modal.entity ? 'Edit' : 'New'} ${modal.type}`}
          onClose={close}
        >
          {modal.type === 'role' && (
            <RoleForm entity={modal.entity} projects={projects} busy={busy} onSubmit={submit} onCancel={close} />
          )}
          {modal.type === 'project' && (
            <ProjectForm entity={modal.entity} busy={busy} onSubmit={submit} onCancel={close} />
          )}
          {modal.type === 'user' && (
            <UserForm entity={modal.entity} roles={roles} busy={busy} onSubmit={submit} onCancel={close} />
          )}
        </Modal>
      )}

      {resetTarget && (
        <Modal title="Reset password" onClose={() => setResetTarget(null)}>
          <ResetPasswordModal
            user={resetTarget}
            onClose={() => setResetTarget(null)}
          />
        </Modal>
      )}
    </div>
  );
}

function Modal({ title, onClose, children }) {
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="modal-overlay" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button type="button" className="modal-close" onClick={onClose} title="Close">
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

function FormShell({ busy, onSubmit, onCancel, buildPayload, children }) {
  const [err, setErr] = useState('');
  async function handle(e) {
    e.preventDefault();
    setErr('');
    try {
      await onSubmit(buildPayload());
    } catch (ex) {
      setErr(ex.message);
    }
  }
  return (
    <form onSubmit={handle} className="modal-form">
      {children}
      {err && <div className="alert error">{err}</div>}
      <div className="modal-actions">
        <button type="button" className="ghost-btn" onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="save-btn" disabled={busy}>
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>
    </form>
  );
}

function CheckList({ label, items, selected, toggle, empty }) {
  return (
    <div className="form-field">
      <span className="form-label">{label}</span>
      {items.length === 0 ? (
        <p className="empty">{empty}</p>
      ) : (
        <div className="check-list">
          {items.map((it) => (
            <label key={it.id} className="check-row">
              <input
                type="checkbox"
                checked={selected.includes(it.id)}
                onChange={() => toggle(it.id)}
              />
              <span>
                {it.name}
                {it.is_admin && <span className="badge">admin</span>}
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

function RoleForm({ entity, projects, busy, onSubmit, onCancel }) {
  const [name, setName] = useState(entity?.name || '');
  const [description, setDescription] = useState(entity?.description || '');
  const [isAdmin, setIsAdmin] = useState(entity?.is_admin || false);
  const [projectIds, setProjectIds] = useState(entity?.projects?.map((p) => p.id) || []);

  const toggle = (id) =>
    setProjectIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));

  return (
    <FormShell
      busy={busy}
      onSubmit={onSubmit}
      onCancel={onCancel}
      buildPayload={() => ({ name, description, is_admin: isAdmin, project_ids: projectIds })}
    >
      <label className="form-field">
        <span className="form-label">Name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} required minLength={2} />
      </label>
      <label className="form-field">
        <span className="form-label">Description</span>
        <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <label className="check-row standalone">
        <input type="checkbox" checked={isAdmin} onChange={(e) => setIsAdmin(e.target.checked)} />
        <span>Administrator role</span>
      </label>
      <CheckList
        label="Granted projects"
        items={projects}
        selected={projectIds}
        toggle={toggle}
        empty="No projects exist yet."
      />
    </FormShell>
  );
}

function UserForm({ entity, roles, busy, onSubmit, onCancel }) {
  const [name, setName] = useState(entity?.name || '');
  const [email, setEmail] = useState(entity?.email || '');
  const [jobTitle, setJobTitle] = useState(entity?.job_title || '');
  const [isActive, setIsActive] = useState(entity ? entity.is_active : true);
  const [roleIds, setRoleIds] = useState(entity?.roles?.map((r) => r.id) || []);
  const [password, setPassword] = useState('');

  const isEdit = Boolean(entity);
  const toggle = (id) =>
    setRoleIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));

  return (
    <FormShell
      busy={busy}
      onSubmit={onSubmit}
      onCancel={onCancel}
      buildPayload={() => {
        const payload = {
          name,
          email,
          job_title: jobTitle,
          is_active: isActive,
          role_ids: roleIds,
        };
        if (password) payload.password = password;
        return payload;
      }}
    >
      <label className="form-field">
        <span className="form-label">Name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} required minLength={2} />
      </label>
      <label className="form-field">
        <span className="form-label">Email</span>
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
      </label>
      <label className="form-field">
        <span className="form-label">Job title</span>
        <input value={jobTitle} onChange={(e) => setJobTitle(e.target.value)} />
      </label>
      <label className="form-field">
        <span className="form-label">
          Password{isEdit && <em> (leave blank to keep current)</em>}
        </span>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required={!isEdit}
          autoComplete="new-password"
          placeholder={isEdit ? 'Leave blank to keep current' : 'Required'}
        />
      </label>
      <label className="check-row standalone">
        <input type="checkbox" checked={isActive} onChange={(e) => setIsActive(e.target.checked)} />
        <span>Active</span>
      </label>
      <CheckList
        label="Assigned roles"
        items={roles}
        selected={roleIds}
        toggle={toggle}
        empty="No roles exist yet."
      />
    </FormShell>
  );
}

function ProjectForm({ entity, busy, onSubmit, onCancel }) {
  const [name, setName] = useState(entity?.name || '');
  const [description, setDescription] = useState(entity?.description || '');
  const [provider, setProvider] = useState(entity?.provider || 'GITHUB');
  const [repoUrl, setRepoUrl] = useState(entity?.repo_url || '');
  const [branch, setBranch] = useState(entity?.default_branch || 'main');
  const [status, setStatus] = useState(entity?.status || 'ACTIVE');
  const [token, setToken] = useState('');

  return (
    <FormShell
      busy={busy}
      onSubmit={onSubmit}
      onCancel={onCancel}
      buildPayload={() => {
        const payload = {
          name,
          description,
          provider,
          repo_url: repoUrl,
          default_branch: branch,
          status,
        };
        if (token) payload.access_token = token;
        return payload;
      }}
    >
      <label className="form-field">
        <span className="form-label">Name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} required minLength={2} />
      </label>
      <label className="form-field">
        <span className="form-label">Description</span>
        <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <div className="form-row">
        <label className="form-field">
          <span className="form-label">Provider</span>
          <select value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="GITHUB">GitHub</option>
            <option value="GITLAB">GitLab</option>
            <option value="MANUAL">Manual</option>
          </select>
        </label>
        <label className="form-field">
          <span className="form-label">Status</span>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="ACTIVE">Active</option>
            <option value="DISABLED">Disabled</option>
          </select>
        </label>
      </div>
      <div className="form-row">
        <label className="form-field grow">
          <span className="form-label">Repository URL</span>
          <input value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} placeholder="https://…" />
        </label>
        <label className="form-field">
          <span className="form-label">Default branch</span>
          <input value={branch} onChange={(e) => setBranch(e.target.value)} />
        </label>
      </div>
      <label className="form-field">
        <span className="form-label">
          Access token {entity?.token_last4 && <em>(stored ••{entity.token_last4} — leave blank to keep)</em>}
        </span>
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={entity ? 'Leave blank to keep current token' : 'Optional'}
          autoComplete="new-password"
        />
      </label>
    </FormShell>
  );
}

/* ---------- Icons ---------- */
function PlusIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}
function PaperclipIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21.44 11.05l-9.19 9.19a5 5 0 0 1-7.07-7.07l9.19-9.19a3.5 3.5 0 0 1 4.95 4.95l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
    </svg>
  );
}
function ArrowUpIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 19V5M5 12l7-7 7 7" />
    </svg>
  );
}
function StopIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <rect x="4" y="4" width="16" height="16" rx="2" />
    </svg>
  );
}
function DownloadIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3v12M7 11l5 5 5-5M4 20h16" />
    </svg>
  );
}
function TrashIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" />
    </svg>
  );
}
function GearIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}
function LockIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="11" width="18" height="11" rx="2" />
      <path d="M7 11V7a5 5 0 0 1 10 0v4" />
    </svg>
  );
}
function SignOutIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <polyline points="16 17 21 12 16 7" />
      <line x1="21" y1="12" x2="9" y2="12" />
    </svg>
  );
}
function ChevronIcon({ open }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ transform: open ? 'rotate(180deg)' : 'rotate(0deg)', transition: 'transform 0.2s' }}>
      <polyline points="18 15 12 9 6 15" />
    </svg>
  );
}
