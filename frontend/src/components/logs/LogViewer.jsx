import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  filterLogLinesByLevel,
  filterLogLinesBySearch,
  formatLogLinesToLocalTime,
  stripLogTimestamps,
} from "../../utils/logFormat.js";

const SCROLL_THRESHOLD_PX = 48;

function isNearBottom(container) {
  if (!container) {
    return true;
  }
  const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
  return distance <= SCROLL_THRESHOLD_PX;
}

function LogContent({ text, revision }) {
  return (
    <pre className="log-viewer__content" data-revision={revision}>
      {text}
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
  onJumpToLatestRef,
}) {
  const scrollRef = useRef(null);
  const pinnedToBottomRef = useRef(true);
  const prevLineCountRef = useRef(0);
  const prevLogTextRef = useRef("");
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [hasNewLogs, setHasNewLogs] = useState(false);
  const [isMaximized, setIsMaximized] = useState(false);

  const toggleMaximized = useCallback(() => {
    setIsMaximized((value) => !value);
  }, []);

  // Allow Esc to exit the maximized view.
  useEffect(() => {
    if (!isMaximized) {
      return undefined;
    }
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        setIsMaximized(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
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
    } catch {
      /* clipboard unavailable */
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
    anchor.download = `kubesight-logs-${Date.now()}.txt`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section
      className={`card log-viewer ${isMaximized ? "log-viewer--maximized" : ""} ${className}`.trim()}
      role="log"
      aria-live={live ? "polite" : "off"}
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
            Copy
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
            onClick={toggleMaximized}
            aria-pressed={isMaximized}
          >
            {isMaximized ? "Exit fullscreen" : "Fullscreen"}
          </button>
        </div>
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
        <div className="log-viewer__frame">
          <div ref={scrollRef} className="log-viewer__scroll" onScroll={handleScroll}>
            <LogContent text={logText} revision={logRevision} />
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
