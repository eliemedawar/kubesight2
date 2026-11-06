import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  deleteAssistantConversation,
  getAssistantConversation,
  getAssistantStatus,
  listAssistantConversations,
  sendAssistantMessage,
  startAssistantConversation,
} from "../../api/assistantApi.js";
import { useEscapeToClose } from "../../hooks/useEscapeToClose.js";
import { parseApiTime } from "../../lib/apiTime.js";
import { parseBlocks, parseInline } from "../../lib/chatMarkdown.js";
import { confirmDialog } from "../../lib/dialogs.js";
import { setVisibleInterval } from "../../lib/visibleInterval.js";

const STORAGE_KEY = "kubesight.assistant.conversation";
// While Hermes answers, the reply row fills in as it streams: poll quickly so
// the text appears as it is written.
const POLL_MS = 700;

const SUGGESTIONS = [
  "What needs attention right now?",
  "Why did the last failed build fail?",
  "What is deployed in this namespace, and at which versions?",
  "Which pods are restarting the most?",
];

function readStoredId() {
  try {
    const value = Number(window.localStorage.getItem(STORAGE_KEY));
    return Number.isInteger(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}

function storeId(id) {
  try {
    if (id) window.localStorage.setItem(STORAGE_KEY, String(id));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Remembering the open chat is a convenience; without storage it starts fresh.
  }
}

function formatElapsed(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatWhen(value) {
  const time = parseApiTime(value);
  if (Number.isNaN(time)) return "";
  const date = new Date(time);
  const today = new Date();
  return date.toDateString() === today.toDateString()
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function Inline({ text }) {
  return parseInline(text).map((span, index) => {
    switch (span.type) {
      case "code":
        return <code key={index}>{span.text}</code>;
      case "strong":
        return <strong key={index}>{span.text}</strong>;
      case "em":
        return <em key={index}>{span.text}</em>;
      case "link":
        return span.href.startsWith("#/") ? (
          <a key={index} href={span.href}>{span.text}</a>
        ) : (
          <a key={index} href={span.href} target="_blank" rel="noopener noreferrer">{span.text}</a>
        );
      default:
        return <Fragment key={index}>{span.text}</Fragment>;
    }
  });
}

function CodeBlock({ lang, text }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="as-code">
      <div className="as-code-bar">
        <span>{lang || "text"}</span>
        <button type="button" className="as-code-copy" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre><code>{text}</code></pre>
    </div>
  );
}

function Markdown({ text }) {
  const blocks = useMemo(() => parseBlocks(text), [text]);
  return blocks.map((block, index) => {
    switch (block.type) {
      case "heading": {
        const Tag = block.level <= 2 ? "h4" : "h5";
        return <Tag key={index}><Inline text={block.text} /></Tag>;
      }
      case "code":
        return <CodeBlock key={index} lang={block.lang} text={block.text} />;
      case "list": {
        const Tag = block.ordered ? "ol" : "ul";
        return (
          <Tag key={index} start={block.ordered && block.start !== 1 ? block.start : undefined}>
            {block.items.map((item, i) => <li key={i}><Inline text={item} /></li>)}
          </Tag>
        );
      }
      case "quote":
        return <blockquote key={index}><Inline text={block.text} /></blockquote>;
      case "table":
        return (
          <div key={index} className="as-table-wrap">
            <table>
              <thead>
                <tr>{block.head.map((cell, i) => <th key={i}><Inline text={cell} /></th>)}</tr>
              </thead>
              <tbody>
                {block.rows.map((row, r) => (
                  <tr key={r}>{row.map((cell, i) => <td key={i}><Inline text={cell} /></td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      case "rule":
        return <hr key={index} />;
      default:
        return <p key={index}><Inline text={block.text} /></p>;
    }
  });
}

function HermesMark() {
  return (
    <span className="as-mark" aria-hidden="true">
      <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10 2.5 11.8 8.2 17.5 10l-5.7 1.8L10 17.5l-1.8-5.7L2.5 10l5.7-1.8Z" />
      </svg>
    </span>
  );
}

function PendingReply({ since, activity, content }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const started = parseApiTime(since);
  const elapsed = Number.isNaN(started) ? "" : ` · ${formatElapsed(now - started)}`;
  return (
    <>
      {content ? <div className="as-md as-md--streaming"><Markdown text={content} /></div> : null}
      <div className="as-pending" role="status">
        <span className="as-dots" aria-hidden="true"><i /><i /><i /></span>
        <span className="as-pending-text">{activity || (content ? "Writing" : "Thinking")}{elapsed}</span>
      </div>
    </>
  );
}

/**
 * Ctrl/⌘+G: chat with Hermes — the same Hermes as the Telegram bot, with the
 * kubesight skill and KubeSight's MCP tools — without leaving the page. Admins
 * only (the backend refuses everyone else). Non-modal, so the page stays
 * usable beside it; each question carries what is on screen.
 */
export default function AssistantPanel({ open, onClose, context }) {
  const [status, setStatus] = useState(null);
  const [conversation, setConversation] = useState(null);
  const [history, setHistory] = useState(null);
  const [showHistory, setShowHistory] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef(null);
  const scrollRef = useRef(null);
  const loadedRef = useRef(false);

  useEscapeToClose(onClose, true, open);

  // First open: is Hermes wired up, and which chat was open last time.
  useEffect(() => {
    if (!open || loadedRef.current) return;
    loadedRef.current = true;
    getAssistantStatus()
      .then(setStatus)
      .catch((err) => setStatus({ configured: false, hint: err.message }));
    const storedId = readStoredId();
    if (storedId) {
      getAssistantConversation(storedId)
        .then(setConversation)
        .catch(() => storeId(null));
    }
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const returnTo = document.activeElement;
    window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      if (returnTo instanceof HTMLElement && returnTo.isConnected) {
        returnTo.focus({ preventScroll: true });
      }
    };
  }, [open]);

  // While Hermes is answering, poll the conversation until the reply lands.
  const pending = Boolean(conversation?.pending);
  const conversationId = conversation?.id;
  useEffect(() => {
    // Not while a send is in flight: a poll then would show the chat without it.
    if (!pending || !conversationId || sending) return undefined;
    return setVisibleInterval(() => {
      getAssistantConversation(conversationId)
        .then((next) => setConversation((current) => (current?.id === next.id ? next : current)))
        .catch(() => {});
    }, POLL_MS);
  }, [pending, conversationId, sending]);

  // Follow the answer as it grows, unless the admin scrolled up to read.
  const messages = conversation?.messages || [];
  const lastMessage = messages[messages.length - 1];
  const followKey = `${messages.length}:${lastMessage?.content?.length || 0}:${lastMessage?.activity || ""}`;
  const pinnedRef = useRef(true);
  useEffect(() => {
    const box = scrollRef.current;
    if (box && pinnedRef.current) box.scrollTop = box.scrollHeight;
  }, [followKey, pending, open, showHistory]);

  // Grow the composer with its text, up to a cap.
  useEffect(() => {
    const box = inputRef.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight, 180)}px`;
  }, [draft, open]);

  const send = useCallback(
    async (text) => {
      const message = String(text ?? draft).trim();
      if (!message || sending || pending) return;
      setSending(true);
      setError("");
      // Show the question and a "thinking" reply at once; the server's copy
      // replaces them a moment later.
      const before = conversation;
      const now = new Date().toISOString();
      setConversation({
        ...(conversation || { id: null, title: message }),
        pending: true,
        messages: [
          ...(conversation?.messages || []),
          { id: "local-question", role: "user", content: message, status: "done", createdAt: now },
          { id: "local-reply", role: "assistant", content: "", status: "pending", createdAt: now },
        ],
      });
      setDraft("");
      pinnedRef.current = true;
      try {
        const next = conversation
          ? await sendAssistantMessage(conversation.id, message, context)
          : await startAssistantConversation(message, context);
        setConversation(next);
        storeId(next.id);
        setHistory(null);
      } catch (err) {
        setConversation(before);
        setDraft(message);
        setError(err.serverMessage || err.message || "Could not send the message.");
      } finally {
        setSending(false);
        window.requestAnimationFrame(() => inputRef.current?.focus());
      }
    },
    [context, conversation, draft, pending, sending]
  );

  const newChat = () => {
    setConversation(null);
    storeId(null);
    setShowHistory(false);
    setError("");
    window.requestAnimationFrame(() => inputRef.current?.focus());
  };

  const openHistory = () => {
    setShowHistory((shown) => !shown);
    if (!history) {
      listAssistantConversations()
        .then(setHistory)
        .catch((err) => {
          setHistory([]);
          setError(err.message);
        });
    }
  };

  const pickConversation = (id) => {
    setShowHistory(false);
    setError("");
    getAssistantConversation(id)
      .then((next) => {
        setConversation(next);
        storeId(next.id);
      })
      .catch((err) => setError(err.message));
  };

  const removeConversation = async (item) => {
    const ok = await confirmDialog({
      title: "Delete this chat?",
      message: `“${item.title}” and its messages are deleted for good.`,
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await deleteAssistantConversation(item.id);
      setHistory((rows) => (rows || []).filter((row) => row.id !== item.id));
      if (conversation?.id === item.id) newChat();
    } catch (err) {
      setError(err.message);
    }
  };

  if (!open) return null;

  const notConfigured = status && status.configured === false;
  const busy = sending || pending;
  const lastQuestion = [...messages].reverse().find((m) => m.role === "user")?.content;

  return (
    <aside className="as-panel" role="dialog" aria-modal="false" aria-label="Hermes assistant">
      <header className="as-head">
        <HermesMark />
        <div className="as-head-text">
          <h3>Hermes</h3>
          <p>{conversation ? conversation.title : "Ask about your clusters, builds and deploys"}</p>
        </div>
        <div className="as-head-actions">
          <button aria-label="Past chats" type="button" className="as-icon-btn" onClick={openHistory} aria-pressed={showHistory} title="Past chats">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M3.5 10a6.5 6.5 0 1 0 1.9-4.6M3.5 3.5v3h3" />
              <path d="M10 6.5V10l2.5 1.5" />
            </svg>
          </button>
          <button aria-label="New chat" type="button" className="as-icon-btn" onClick={newChat} title="New chat">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
              <path d="M10 4.5v11M4.5 10h11" />
            </svg>
          </button>
          <button aria-label="Close" type="button" className="as-icon-btn" onClick={onClose} title="Close (Esc)">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
              <path d="m5.5 5.5 9 9M14.5 5.5l-9 9" />
            </svg>
          </button>
        </div>
      </header>

      {showHistory ? (
        <div className="as-history">
          {history === null ? (
            <p className="as-muted">Loading…</p>
          ) : history.length ? (
            <ul>
              {history.map((item) => (
                <li key={item.id} className={item.id === conversation?.id ? "is-current" : undefined}>
                  <button type="button" className="as-history-pick" onClick={() => pickConversation(item.id)}>
                    <span className="as-history-title">{item.title}</span>
                    <span className="as-history-when">{formatWhen(item.updatedAt)}</span>
                  </button>
                  <button aria-label={`Delete “${item.title}”`} type="button" className="as-icon-btn as-history-delete" onClick={() => removeConversation(item)} title="Delete chat">
                    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <path d="M4.5 6h11M8 6V4.5h4V6M6 6l.7 9.5h6.6L14 6" />
                    </svg>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="as-muted">No past chats yet.</p>
          )}
        </div>
      ) : null}

      <div
        className="as-scroll"
        ref={scrollRef}
        hidden={showHistory}
        onScroll={(event) => {
          const box = event.currentTarget;
          pinnedRef.current = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
        }}
      >
        {notConfigured ? (
          <div className="as-notice">
            <strong>Hermes is not connected.</strong>
            <p>{status.hint}</p>
          </div>
        ) : null}
        {!messages.length && !notConfigured ? (
          <div className="as-empty">
            <p>Hermes reads KubeSight through its tools and asks before it changes anything.</p>
            <div className="as-suggestions">
              {SUGGESTIONS.map((text) => (
                <button key={text} type="button" className="as-suggestion" onClick={() => send(text)} disabled={busy}>
                  {text}
                </button>
              ))}
            </div>
          </div>
        ) : null}
        {messages.map((message) =>
          message.role === "user" ? (
            <div key={message.id} className="as-msg as-msg--user">
              <div className="as-bubble">{message.content}</div>
            </div>
          ) : (
            <div key={message.id} className="as-msg as-msg--hermes">
              {message.status === "pending" ? (
                <PendingReply since={message.createdAt} activity={message.activity} content={message.content} />
              ) : message.status === "failed" ? (
                <div className="as-failed" role="alert">
                  <p>{message.error || "Hermes did not answer."}</p>
                  {lastQuestion && message.id === messages[messages.length - 1].id ? (
                    <button type="button" className="as-retry" onClick={() => send(lastQuestion)} disabled={busy}>
                      Ask again
                    </button>
                  ) : null}
                </div>
              ) : (
                <div className="as-md"><Markdown text={message.content} /></div>
              )}
            </div>
          )
        )}
      </div>

      <form
        className="as-composer"
        onSubmit={(event) => {
          event.preventDefault();
          send();
        }}
      >
        {error ? <p className="as-error" role="alert">{error}</p> : null}
        <div className="as-input-row">
          <textarea
            ref={inputRef}
            className="as-input"
            rows={1}
            value={draft}
            placeholder={pending ? "Hermes is answering…" : "Ask Hermes…"}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                send();
              }
            }}
            disabled={notConfigured}
            maxLength={8000}
            aria-label="Message to Hermes"
          />
          <button aria-label="Send" type="submit" className="as-send" disabled={busy || notConfigured || !draft.trim()} title="Send (Enter)">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M10 15.5v-11M5.5 9 10 4.5 14.5 9" />
            </svg>
          </button>
        </div>
        <p className="as-hint">
          {context?.clusterName ? `Sees: ${[context.pageLabel, context.clusterName, context.namespace].filter(Boolean).join(" · ")}` : context?.pageLabel ? `Sees: ${context.pageLabel}` : ""}
          <span><kbd>Enter</kbd> send · <kbd>Shift+Enter</kbd> new line · <kbd>Ctrl+G</kbd> close</span>
        </p>
      </form>
    </aside>
  );
}
