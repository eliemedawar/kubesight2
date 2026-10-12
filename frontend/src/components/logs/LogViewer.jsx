import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  detectLogLevel,
  filterLogLinesByLevel,
  filterLogLinesBySearch,
  formatLogLinesToLocalTime,
  stripLogTimestamps,
} from "../../utils/logFormat.js";
import { useEscapeToClose } from "../../hooks/useEscapeToClose.js";

const SCROLL_THRESHOLD_PX = 48;

function isNearBottom(container) {
  if (!container) {
    return true;
  }
  const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
  return distance <= SCROLL_THRESHOLD_PX;
}

function LogContent({ lines, revision }) {
  return (
    <pre className="log-viewer__content" data-revision={revision}>
      {lines.map((line, index) => {
        const level = detectLogLevel(line);
        const isError =
          level === "ERROR" || level === "FATAL" || /\b(?:failed|failure)\b/i.test(line);
        return (
          <span
            key={index}
            className={`log-viewer__line${isError ? " log-viewer__line--error" : ""}`}
          >
            {line === "" ? " " : line}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}

export default function LogViewer({
  lines = [],
  live = true,
  streaming = false,
  loading = false,
  error = "",
  emptyMessage = "Select a pod and container to view logs.",
  className = "",
  searchText = "",
  levelFilter = "all",
  showTimestamps = true,
  onRefresh,
  onClear,
  onSearchChange,
  onJumpToLatestRef,
  downloadName = "",
}) {
  const scrollRef = useRef(null);
  const pinnedToBottomRef = useRef(true);
  const prevLineCountRef = useRef(0);
  const prevLogTextRef = useRef("");
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [hasNewLogs, setHasNewLogs] = useState(false);
  const [isMaximized, setIsMaximized] = useState(false);
  // "" | "copied" | "failed" — Copy otherwise gives no sign it did anything.
  const [copyState, setCopyState] = useState("");

  useEffect(() => {
    if (!copyState) {
      return undefined;
    }
    const timer = window.setTimeout(() => setCopyState(""), 2000);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  const toggleMaximized = useCallback(() => {
    setIsMaximized((value) => !value);
  }, []);

  // Allow Esc to exit the maximized view (only that, when the viewer sits in
  // a dialog).
  useEscapeToClose(() => setIsMaximized(false), true, isMaximized);

  // Leaving fullscreen unmounts the search box; if focus was there (or was
  // dropped), put it back on the toggle instead of the page body. A layout
  // effect, so it lands before the dialog-focus observer looks for lost focus.
  const maximizeButtonRef = useRef(null);
  const wasMaximizedRef = useRef(false);
  useLayoutEffect(() => {
    if (wasMaximizedRef.current && !isMaximized) {
      const active = document.activeElement;
      if (!active || active === document.body || !active.isConnected) {
        maximizeButtonRef.current?.focus({ preventScroll: true });
      }
    }
    wasMaximizedRef.current = isMaximized;
  }, [isMaximized]);

  const filteredLines = useMemo(() => {
    let visible = lines;
    visible = filterLogLinesBySearch(visible, searchText);
    visible = filterLogLinesByLevel(visible, levelFilter);
    return visible;
  }, [lines, searchText, levelFilter]);

  const displayLines = useMemo(() => {
    if (showTimestamps) {
      return formatLogLinesToLocalTime(filteredLines);
    }
    return stripLogTimestamps(filteredLines);
  }, [filteredLines, showTimestamps]);

  const logText = useMemo(() => displayLines.join("\n"), [displayLines]);
  const logRevision = useMemo(() => {
    if (!displayLines.length) {
      return 0;
    }
    const lastLine = displayLines[displayLines.length - 1] ?? "";
    return displayLines.length * 100000 + lastLine.length + lastLine.charCodeAt(0);
  }, [displayLines]);
  const hasLines = displayLines.length > 0;
  const hasRawLines = lines.length > 0;

  const syncScrollState = useCallback(() => {
    const container = scrollRef.current;
    if (!container) {
      return;
    }
    const atBottom = isNearBottom(container);
    pinnedToBottomRef.current = atBottom;
    setIsAtBottom(atBottom);
    if (atBottom) {
      setHasNewLogs(false);
    }
  }, []);

  const handleScroll = useCallback(() => {
    syncScrollState();
  }, [syncScrollState]);

  const jumpToLatest = useCallback(() => {
    const container = scrollRef.current;
    if (!container) {
      return;
    }
    pinnedToBottomRef.current = true;
    setIsAtBottom(true);
    setHasNewLogs(false);
    container.scrollTo({ top: container.scrollHeight, behavior: "auto" });
  }, []);

  useLayoutEffect(() => {
    if (onJumpToLatestRef) {
      onJumpToLatestRef.current = jumpToLatest;
    }
  }, [jumpToLatest, onJumpToLatestRef]);

  useLayoutEffect(() => {
    const container = scrollRef.current;
    if (!container || !hasLines) {
      prevLineCountRef.current = 0;
      prevLogTextRef.current = "";
      return;
    }

    const previousCount = prevLineCountRef.current;
    const previousText = prevLogTextRef.current;
    const grew = displayLines.length > previousCount;
    const contentChanged = logText !== previousText;
    prevLineCountRef.current = displayLines.length;
    prevLogTextRef.current = logText;

    if (!live) {
      return;
    }

    if (pinnedToBottomRef.current || previousCount === 0) {
      const scrollToBottom = () => {
        container.scrollTop = container.scrollHeight;
      };
      scrollToBottom();
      requestAnimationFrame(() => {
        scrollToBottom();
        pinnedToBottomRef.current = true;
        setIsAtBottom(true);
        setHasNewLogs(false);
      });
      return;
    }

    if (grew || (contentChanged && previousCount > 0)) {
      setHasNewLogs(true);
    }
  }, [displayLines, live, hasLines, logText]);

  useLayoutEffect(() => {
    if (live) {
      return;
    }
    syncScrollState();
  }, [live, syncScrollState]);

  const showJumpControl = hasLines && !isAtBottom;

  const handleCopy = async () => {
    if (!logText) {
      return;
    }
    try {
      await navigator.clipboard.writeText(logText);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  const handleDownload = () => {
    if (!logText) {
      return;
    }
    const blob = new Blob([logText], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    const base = String(downloadName || "kubesight-logs").replace(/[^\w.-]+/g, "_");
    anchor.download = `${base}-${Date.now()}.txt`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  // Maximized, the viewer covers the page: a modal, so focus stays in it.
  const maximizedDialogProps = isMaximized
    ? { role: "dialog", "aria-modal": "true", "aria-label": "Logs, full screen" }
    : {};

  return (
    <section
      className={`card log-viewer ${isMaximized ? "log-viewer--maximized" : ""} ${className}`.trim()}
      {...maximizedDialogProps}
    >
      <div className="log-viewer__toolbar">
        <div className="log-viewer__toolbar-actions">
          <button type="button" className="btn-outline btn-sm" onClick={onRefresh} disabled={loading}>
            Refresh
          </button>
          <button type="button" className="btn-outline btn-sm" onClick={onClear} disabled={!hasRawLines}>
            Clear
          </button>
          <button type="button" className="btn-outline btn-sm" onClick={handleCopy} disabled={!hasLines}>
            {copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy failed" : "Copy"}
          </button>
          <button type="button" className="btn-outline btn-sm" onClick={handleDownload} disabled={!hasLines}>
            Download
          </button>
          <button type="button" className="btn-outline btn-sm" onClick={jumpToLatest} disabled={!hasLines}>
            Jump to latest
          </button>
          <button
            type="button"
            className="btn-outline btn-sm"
            ref={maximizeButtonRef}
            onClick={toggleMaximized}
            aria-pressed={isMaximized}
          >
            {isMaximized ? "Exit fullscreen" : "Fullscreen"}
          </button>
        </div>

        {isMaximized && onSearchChange ? (
          <div className="log-viewer__toolbar-search">
            <input
              type="search"
              className="form-input log-viewer__search-input"
              placeholder="Filter log text…"
              value={searchText}
              onChange={(event) => onSearchChange(event.target.value)}
              aria-label="Filter log text"
            />
          </div>
        ) : null}
      </div>

      {loading && !hasRawLines ? <p className="muted log-viewer__status">Loading logs…</p> : null}
      {error ? <p className="banner-message error log-viewer__status">{error}</p> : null}
      {!loading && !error && !hasRawLines ? (
        <p className="muted log-viewer__status">{emptyMessage}</p>
      ) : null}
      {!loading && !error && hasRawLines && !hasLines ? (
        <p className="muted log-viewer__status">No log lines match the current search or level filter.</p>
      ) : null}

      {hasLines ? (
        <div className="log-viewer__frame" role="log" aria-live={live ? "polite" : "off"}>
          <div ref={scrollRef} className="log-viewer__scroll" onScroll={handleScroll}>
            <LogContent lines={displayLines} revision={logRevision} />
          </div>
          {showJumpControl ? (
            <button
              type="button"
              className={`log-viewer__jump${hasNewLogs && live ? " log-viewer__jump--highlight" : ""}`}
              onClick={jumpToLatest}
              aria-label={hasNewLogs && live ? "New logs available, jump to latest" : "Jump to latest logs"}
            >
              {hasNewLogs && live ? "New logs available ↓" : "↓ Jump to Latest"}
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
