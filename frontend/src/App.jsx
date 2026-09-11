import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import setuLogo from './assets/setu_logo.svg';

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
  const [attachment, setAttachment] = useState(null); // { file, filename, size }
  const [streaming, setStreaming] = useState(false);
  const [tab, setTab] = useState('chat'); // chat | admin

  // Admin state
  const [adminRoles, setAdminRoles] = useState([]);
  const [adminProjects, setAdminProjects] = useState([]);
  const [adminUsers, setAdminUsers] = useState([]);
  const [adminAudit, setAdminAudit] = useState([]);

  const threadEndRef = useRef(null);
  const abortRef = useRef(null);

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

  const msgs = activeConversation?.messages;
  const lastMsg = msgs?.[msgs.length - 1];
  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [msgs?.length, lastMsg?.content]);

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

  function attachFile(file) {
    if (!file) return;
    setError('');
    setAttachment({ file, filename: file.name, size: file.size });
  }

  function stopStreaming() {
    abortRef.current?.abort();
  }

  async function doStream(convId, text, att) {
    const tempUserId = `tmp-u-${Date.now()}`;
    const tempReplyId = `tmp-a-${Date.now()}`;
    const userContent = att
      ? `[Uploaded document: ${att.filename}]${text ? `\n\n${text}` : ''}`
      : text;

    setError('');
    setStreaming(true);

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    setActiveConversation((prev) => ({
      ...prev,
      messages: [
        ...(prev.messages || []),
        { id: tempUserId, role: 'USER', content: userContent, citations: [] },
        { id: tempReplyId, role: 'ASSISTANT', content: '', citations: [], streaming: true },
      ],
    }));

    const patch = (id, fn) =>
      setActiveConversation((prev) => {
        if (!prev || prev.id !== convId) return prev;
        return { ...prev, messages: prev.messages.map((m) => (m.id === id ? fn(m) : m)) };
      });

    const handlers = {
      onStart: (data) => patch(tempUserId, () => data.user_message),
      onDelta: (chunk) => patch(tempReplyId, (m) => ({ ...m, content: m.content + chunk })),
      onDone: (data) => {
        patch(tempReplyId, () => ({ ...data.reply, streaming: false }));
        setActiveConversation((prev) =>
          prev && prev.id === convId ? { ...prev, ...data.conversation } : prev,
        );
      },
    };

    try {
      if (att) await api.streamUpload(userId, convId, att.file, text, handlers, ctrl.signal);
      else await api.streamMessage(userId, convId, text, handlers, ctrl.signal);
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
      setStreaming(false);
    }
  }

  async function sendMessage(e) {
    e.preventDefault();
    if (!activeConversation || streaming) return;
    const typed = messageText.trim();
    const att = attachment;
    if (!typed && !att) return;
    setMessageText('');
    setAttachment(null);
    await doStream(activeConversation.id, typed, att);
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

  function onComposerKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage(e);
    }
  }

  // ---------- Login screen ----------
  if (!userId) {
    return (
      <div className="login-screen">
        <div className="login-card">
          <div className="login-logo-wrap">
            <img src={setuLogo} alt="Setu" className="login-logo" />
          </div>
          <p className="login-sub">Choose an account to start chatting</p>
          {error && <div className="alert error">{error}</div>}
          <ul className="account-list">
            {accounts.map((a) => (
              <li key={a.id}>
                <button type="button" onClick={() => setUserId(a.id)}>
                  <span className="acct-avatar">{(a.name || '?').charAt(0)}</span>
                  <span className="acct-body">
                    <span className="acct-name">{a.name}</span>
                    <span className="acct-meta">
                      {a.email} · {a.roles?.map((r) => r.name).join(', ')}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
    );
  }

  const messages = activeConversation?.messages || [];

  return (
    <div className="layout">
      {/* ---------- Sidebar ---------- */}
      <aside className="sidebar">
        <div className="sidebar-top">
          <div className="brand">
            <img src={setuLogo} alt="Setu" className="sidebar-logo" />
          </div>

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
        </div>

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

        <div className="sidebar-bottom">
          {session?.is_admin && (
            <button
              type="button"
              className={`side-link ${tab === 'admin' ? 'active' : ''}`}
              onClick={() => {
                if (tab === 'admin') {
                  setTab('chat');
                } else {
                  setTab('admin');
                  loadAdmin();
                }
              }}
            >
              <GearIcon />
              {tab === 'admin' ? 'Back to chat' : 'Admin'}
            </button>
          )}
          <div className="user-chip">
            <span className="acct-avatar sm">
              {(session?.user?.name || '?').charAt(0)}
            </span>
            <span className="user-chip-body">
              <span className="user-chip-name">
                {session?.user?.name || userId}
                {session?.is_admin && <span className="badge">admin</span>}
              </span>
            </span>
            <button type="button" className="side-link tiny" onClick={logout}>
              Switch
            </button>
          </div>
        </div>
      </aside>

      {/* ---------- Main ---------- */}
      <main className="main">
        {error && <div className="alert error floating">{error}</div>}

        {tab === 'admin' && session?.is_admin ? (
          <AdminView
            userId={userId}
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
                    userId={userId}
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
              <img src={setuLogo} alt="Setu" className="welcome-logo" />
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
                    const conv = await api.createConversation(userId, projectId);
                    await loadConversations();
                    const detail = await api.getConversation(userId, conv.id);
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

// Split a user message into its optional document marker and the typed
// question, so an uploaded PDF/DOCX shows as a compact chip. The extracted text
// itself now arrives as the assistant reply, so the marker carries only a
// filename (no embedded body).
function parseUserContent(content) {
  const up = content.match(/^\[Uploaded document: (.+?)\](?:\n\n)?/);
  if (up) {
    return { question: content.slice(up[0].length), doc: { filename: up[1] } };
  }
  return { question: content, doc: null };
}

function Message({ m, userId, onError, onGrow }) {
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
          <div className="typing">
            <span></span>
            <span></span>
            <span></span>
          </div>
        ) : m.business_plan_id && !m.streaming ? (
          // The reply's text is the plan's plain-text summary (kept for the
          // agent's memory); the card is the interactive version of it.
          <BusinessPlanCard planId={m.business_plan_id} userId={userId} onError={onError} onGrow={onGrow} />
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

const PLAN_STATUS = {
  DRAFT: 'Needs your review',
  CONFIRMED: 'Vetting',
  DONE: 'Vetted',
  DISCARDED: 'Discarded',
};

let draftKey = 0;
const toDraft = (items) =>
  items.map((i) => ({ key: i.id, description: i.description, location: i.location }));

// The businesses extracted from a document uploaded in chat. While DRAFT the
// user reviews them — yes (confirm), no (discard), or edit/remove/add — and
// once confirmed each is vetted against the repo one at a time, its result
// shown the moment it arrives over SSE. Reopening a half-vetted plan offers
// to resume; the server replays finished items and carries on from there.
function BusinessPlanCard({ planId, userId, onError, onGrow }) {
  const [plan, setPlan] = useState(null);
  const [draft, setDraft] = useState([]);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [vetting, setVetting] = useState(false);
  const [runningId, setRunningId] = useState(null);
  const abortRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    api.business
      .getPlan(userId, planId)
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
  }, [planId, userId, onError]);

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
        userId,
        planId,
        {
          onEvent: (event, data) => {
            if (event === 'item_start') {
              setRunningId(data.item_id);
            } else if (event === 'item_result') {
              setRunningId(null);
              setPlan((p) => ({ ...p, items: p.items.map((i) => (i.id === data.id ? data : i)) }));
              onGrow();
            }
          },
          // `done` carries the plan without its items; keep the ones we have.
          onDone: (data) => setPlan((p) => ({ ...p, ...data })),
        },
        controller.signal,
      );
    } catch (e) {
      if (e.name !== 'AbortError') onError(e.message);
    } finally {
      setVetting(false);
      setRunningId(null);
    }
  }

  async function confirm() {
    setBusy(true);
    onError('');
    try {
      let current = plan;
      if (dirty) {
        current = await api.business.updateItems(
          userId,
          planId,
          kept.map(({ description, location }) => ({ description: description.trim(), location })),
        );
        setDraft(toDraft(current.items));
        setDirty(false);
      }
      const confirmed = await api.business.confirm(userId, planId);
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
      const discarded = await api.business.discard(userId, planId);
      setPlan((p) => ({ ...p, ...discarded }));
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const vettedCount = plan.items.filter((i) => i.vetting_status !== 'PENDING').length;

  return (
    <div className="plan-card">
      <div className="plan-head">
        <PaperclipIcon />
        <span className="plan-file" title={plan.source_filename}>
          {plan.source_filename}
        </span>
        <span className={`plan-status ${plan.status.toLowerCase()}`}>{PLAN_STATUS[plan.status]}</span>
      </div>

      {plan.status === 'DRAFT' ? (
        <>
          <p className="plan-intro">
            {plan.items.length
              ? `I found ${plan.items.length} business requirement${plan.items.length === 1 ? '' : 's'}. Is this list right? Edit, remove or add any, then confirm to vet each one against the codebase.`
              : 'I could not find distinct business requirements in this document. Add them below, then confirm to vet them against the codebase.'}
          </p>
          <ol className="plan-edit-list">
            {draft.map((it) => (
              <li key={it.key}>
                <div className="plan-edit-body">
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
              : `Vetting one at a time — ${vettedCount} of ${plan.items.length} done.`}
          </p>
          <ol className="vet-list">
            {plan.items.map((item) => (
              <VettedItem key={item.id} item={item} running={runningId === item.id} />
            ))}
          </ol>
          {plan.status === 'CONFIRMED' && !vetting && (
            <div className="plan-actions">
              <button type="button" className="save-btn" onClick={startVetting}>
                Resume vetting
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

const yesNo = (v) => (v == null ? '—' : v ? 'Yes' : 'No');

function VettedItem({ item, running }) {
  const state = running ? 'running' : item.vetting_status.toLowerCase();
  const label = { running: 'Vetting…', pending: 'Waiting', done: 'Done', error: 'Failed' }[state];
  return (
    <li className={`vet-item ${state}`}>
      <div className="vet-head">
        <span className="vet-no">{item.seq_no}</span>
        <span className="vet-desc">
          {item.description}
          {item.location && <span className="plan-loc">{item.location}</span>}
        </span>
        <span className={`vet-state ${state}`}>{label}</span>
      </div>
      {item.vetting_status === 'DONE' && (
        <div className="vet-body">
          <p className="vet-verdict">{item.verdict}</p>
          <div className="vet-flags">
            <span className="pill">Changes existing business: {yesNo(item.is_existing_business_change)}</span>
            {item.is_existing_business_change && (
              <span className="pill">Feasible: {yesNo(item.change_feasible)}</span>
            )}
            <span className="pill">Impacts other features: {yesNo(item.impacts_other_features)}</span>
          </div>
          {item.feasibility_notes && (
            <>
              <div className="vet-label">Feasibility</div>
              <p className="vet-notes">{item.feasibility_notes}</p>
            </>
          )}
          {item.impact_notes && (
            <>
              <div className="vet-label">Impact</div>
              <p className="vet-notes">{item.impact_notes}</p>
            </>
          )}
        </div>
      )}
      {item.vetting_status === 'ERROR' && <p className="vet-error">{item.error_message}</p>}
    </li>
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

function AdminView({ userId, loading, onRefresh, onError, roles, projects, users, audit }) {
  // modal = { type: 'role' | 'project' | 'user', entity: object | null }
  const [modal, setModal] = useState(null);
  const [busy, setBusy] = useState(false);

  const close = () => setModal(null);

  async function remove(kind, entity) {
    const label = entity.name || entity.email;
    if (!window.confirm(`Delete ${kind} “${label}”? This cannot be undone.`)) return;
    onError('');
    try {
      if (kind === 'role') await api.admin.deleteRole(userId, entity.id);
      else if (kind === 'project') await api.admin.deleteProject(userId, entity.id);
      else await api.admin.deleteUser(userId, entity.id);
      await onRefresh();
    } catch (e) {
      onError(e.message);
    }
  }

  async function reindex(id) {
    onError('');
    try {
      await api.admin.reindexProject(userId, id);
      await onRefresh();
    } catch (e) {
      onError(e.message);
    }
  }

  async function submit(payload) {
    setBusy(true);
    // Any thrown error propagates to FormShell, which keeps the modal open and
    // shows it; the `finally` still clears the busy flag.
    try {
      const { type, entity } = modal;
      const id = entity?.id;
      if (type === 'role') {
        if (id) await api.admin.updateRole(userId, id, payload);
        else await api.admin.createRole(userId, payload);
      } else if (type === 'project') {
        if (id) await api.admin.updateProject(userId, id, payload);
        else await api.admin.createProject(userId, payload);
      } else {
        if (id) await api.admin.updateUser(userId, id, payload);
        else await api.admin.createUser(userId, payload);
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

/** Wraps a form: manages the submit error and shared footer buttons. */
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

  const toggle = (id) =>
    setRoleIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));

  return (
    <FormShell
      busy={busy}
      onSubmit={onSubmit}
      onCancel={onCancel}
      buildPayload={() => ({
        name,
        email,
        job_title: jobTitle,
        is_active: isActive,
        role_ids: roleIds,
      })}
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
        // Only send the token when the admin actually typed one; blank leaves
        // the stored token untouched (backend treats null as "no change").
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
