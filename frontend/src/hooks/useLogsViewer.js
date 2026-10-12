import { useCallback, useEffect, useRef, useState } from "react";
import { getContainerLogs, streamContainerLogs } from "../api/clustersApi.js";
import { appendLogLines, applyLogSnapshot, getIncrementalTailCursor } from "../utils/logFormat.js";
import { buildLogTimeQuery } from "../utils/logTimeRange.js";

// When the server has no free live-stream slot (429), the viewer polls for new
// lines instead and tries the stream again a little later.
const POLL_FALLBACK_MS = 3000;
const STREAM_RETRY_MS = 60000;

function logTargetKey(filters) {
  return [
    filters.cluster,
    filters.namespace,
    filters.pod,
    filters.container,
    filters.timeRange,
    filters.customFrom,
    filters.customTo,
    filters.previous ? "prev" : "curr",
  ].join("|");
}

export default function useLogsViewer(filters, { enabled = true } = {}) {
  const [logLines, setLogLines] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [liveEnabled, setLiveEnabled] = useState(true);

  const filtersRef = useRef(filters);
  const logLinesRef = useRef(logLines);
  const liveEnabledRef = useRef(liveEnabled);
  const logTargetKeyRef = useRef("");
  const targetGenerationRef = useRef(0);
  const initialLoadDoneRef = useRef(false);
  const tailCursorRef = useRef(null);

  filtersRef.current = filters;
  logLinesRef.current = logLines;
  liveEnabledRef.current = liveEnabled;

  const hasTarget =
    enabled &&
    Boolean(filters.cluster && filters.namespace && filters.pod && filters.container);

  const fetchLogs = useCallback(async ({ silent = false, incremental = false } = {}) => {
    const targetGeneration = targetGenerationRef.current;
    const currentFilters = filtersRef.current;
    const requestTargetKey = logTargetKey(currentFilters);
    const currentTimeQuery = buildLogTimeQuery(currentFilters);

    if (currentTimeQuery.error) {
      if (!silent) {
        setError(currentTimeQuery.error);
        setLogLines([]);
      }
      return false;
    }

    if (
      !currentFilters.cluster ||
      !currentFilters.namespace ||
      !currentFilters.pod ||
      !currentFilters.container
    ) {
      return false;
    }

    if (!silent) {
      setLoading(true);
      setError("");
    }

    try {
      const query = {
        live: liveEnabledRef.current ? "true" : "false",
        timestamps: "true",
        previous: currentFilters.previous ? "true" : "false",
        tailLines: "200",
      };

      if (incremental && liveEnabledRef.current) {
        const sinceTime =
          tailCursorRef.current ||
          (logLinesRef.current.length ? getIncrementalTailCursor(logLinesRef.current) : null);
        if (sinceTime) {
          query.sinceTime = sinceTime;
        }
      } else if (!liveEnabledRef.current && currentTimeQuery.query) {
        Object.assign(query, currentTimeQuery.query);
      } else if (liveEnabledRef.current && currentTimeQuery.query?.sinceSeconds) {
        Object.assign(query, { sinceSeconds: currentTimeQuery.query.sinceSeconds });
      }

      const payload = await getContainerLogs(
        currentFilters.cluster,
        currentFilters.namespace,
        currentFilters.pod,
        currentFilters.container,
        query
      );

      if (targetGeneration !== targetGenerationRef.current) {
        return false;
      }
      if (logTargetKey(filtersRef.current) !== requestTargetKey) {
        return false;
      }

      const incoming = payload.lines || [];
      const incomingCursor = getIncrementalTailCursor(incoming);
      if (incomingCursor) {
        tailCursorRef.current = incomingCursor;
      }

      if (incremental && liveEnabledRef.current) {
        if (logLinesRef.current.length) {
          setLogLines((prev) => appendLogLines(prev, incoming));
        } else if (incoming.length) {
          const snapshot = applyLogSnapshot(incoming);
          if (snapshot?.length) {
            setLogLines(snapshot);
          }
        }
      } else {
        const snapshot = applyLogSnapshot(incoming);
        if (snapshot?.length) {
          setLogLines(snapshot);
        } else if (!silent) {
          setLogLines([]);
        }
      }
      return true;
    } catch (loadError) {
      if (targetGeneration !== targetGenerationRef.current) {
        return false;
      }
      if (!silent) {
        setError(loadError.message);
      }
      return false;
    } finally {
      if (!incremental && targetGeneration === targetGenerationRef.current) {
        initialLoadDoneRef.current = true;
      }
      if (!silent && targetGeneration === targetGenerationRef.current) {
        setLoading(false);
      }
    }
  }, []);

  const logTargetKeyValue = logTargetKey(filters);
  const prevLiveRef = useRef(liveEnabled);

  // Reset on target change. Live mode is fed by the streaming effect below
  // (the `kubectl logs -f` tail provides the initial backlog), so only the
  // snapshot (non-live) mode performs an explicit fetch here.
  useEffect(() => {
    if (!hasTarget) {
      setLogLines([]);
      setError("");
      setLoading(false);
      logTargetKeyRef.current = "";
      return undefined;
    }

    if (logTargetKeyRef.current === logTargetKeyValue) {
      return undefined;
    }

    const timeQuery = buildLogTimeQuery(filters);
    if (timeQuery.error && !liveEnabledRef.current) {
      setLogLines([]);
      setError(timeQuery.error);
      setLoading(false);
      return undefined;
    }

    logTargetKeyRef.current = logTargetKeyValue;
    targetGenerationRef.current += 1;
    initialLoadDoneRef.current = false;
    tailCursorRef.current = null;
    setLogLines([]);
    setError("");
    if (!liveEnabledRef.current) {
      fetchLogs({ silent: false, incremental: false });
    }

    return undefined;
  }, [hasTarget, logTargetKeyValue, filters, fetchLogs]);

  // When the user toggles live off, load a snapshot for the current target;
  // toggling it on clears the buffer so the stream repopulates from its tail.
  useEffect(() => {
    const wasLive = prevLiveRef.current;
    prevLiveRef.current = liveEnabled;
    if (wasLive === liveEnabled || !hasTarget) {
      return;
    }
    setLogLines([]);
    setError("");
    tailCursorRef.current = null;
    if (!liveEnabled) {
      fetchLogs({ silent: false, incremental: false });
    }
  }, [liveEnabled, hasTarget, fetchLogs]);

  // Live mode: follow logs over a Server-Sent Events stream (kubectl logs -f).
  // The stream is closed while the browser tab is hidden and reopened (from
  // the newest line already shown) when it is visible again; the server ends
  // a stream after a while (reason "max_duration") and it is reopened the same
  // way. With no free stream slot (429), new lines are polled instead.
  useEffect(() => {
    if (!liveEnabled || !hasTarget) {
      return undefined;
    }

    let cancelled = false;
    let controller = null;
    let pollTimer = null;
    let retryTimer = null;
    const currentFilters = filtersRef.current;
    const isHidden = () => typeof document !== "undefined" && document.hidden;

    const baseQuery = { timestamps: "true", tailLines: "200" };
    const timeQuery = buildLogTimeQuery(currentFilters);
    if (timeQuery.query?.sinceSeconds) {
      baseQuery.sinceSeconds = timeQuery.query.sinceSeconds;
    }

    const stopPolling = () => {
      if (pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
      if (retryTimer) {
        window.clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    const closeStream = () => {
      if (controller) {
        controller.abort();
        controller = null;
      }
    };

    const startPolling = () => {
      if (pollTimer || cancelled) {
        return;
      }
      pollTimer = window.setInterval(() => {
        if (!isHidden()) {
          fetchLogs({ silent: true, incremental: true });
        }
      }, POLL_FALLBACK_MS);
      retryTimer = window.setTimeout(() => {
        stopPolling();
        openStream();
      }, STREAM_RETRY_MS);
    };

    function openStream() {
      if (cancelled || controller || pollTimer || isHidden()) {
        return;
      }
      const own = new AbortController();
      controller = own;
      const query = { ...baseQuery };
      // Reconnecting: continue after the newest line already on screen.
      const cursor = logLinesRef.current.length ? getIncrementalTailCursor(logLinesRef.current) : null;
      if (cursor) {
        query.sinceTime = cursor;
        delete query.sinceSeconds;
      }
      let endReason = null;
      streamContainerLogs(
        currentFilters.cluster,
        currentFilters.namespace,
        currentFilters.pod,
        currentFilters.container,
        {
          query,
          signal: own.signal,
          onEvent: ({ event, data }) => {
            if (cancelled) {
              return;
            }
            if (event === "line" && data?.line) {
              setLogLines((prev) => appendLogLines(prev, [data.line]));
            } else if (event === "error" && data?.message) {
              setError(data.message);
            } else if (event === "end") {
              endReason = data?.reason || null;
            }
          },
        }
      )
        .then(() => {
          if (controller === own) {
            controller = null;
          }
          if (!cancelled && endReason === "max_duration") {
            openStream();
          }
        })
        .catch((streamError) => {
          if (controller === own) {
            controller = null;
          }
          if (cancelled || streamError.name === "AbortError") {
            return;
          }
          if (streamError.status === 429) {
            fetchLogs({ silent: true, incremental: true });
            startPolling();
            return;
          }
          setError(streamError.message);
        })
        .finally(() => {
          if (!cancelled) {
            initialLoadDoneRef.current = true;
          }
        });
    }

    const onVisibility = () => {
      if (isHidden()) {
        closeStream();
      } else if (!pollTimer) {
        openStream();
      }
    };

    setError("");
    openStream();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibility);
      stopPolling();
      closeStream();
    };
  }, [liveEnabled, hasTarget, logTargetKeyValue, fetchLogs]);

  const refreshLogs = useCallback(() => {
    fetchLogs({ silent: false, incremental: false });
  }, [fetchLogs]);

  const clearLogs = useCallback(() => {
    setLogLines([]);
    tailCursorRef.current = null;
  }, []);

  return {
    logLines,
    setLogLines,
    loading,
    error,
    liveEnabled,
    setLiveEnabled,
    refreshLogs,
    clearLogs,
    hasTarget,
  };
}
