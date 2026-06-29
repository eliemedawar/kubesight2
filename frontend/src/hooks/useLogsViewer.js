import { useCallback, useEffect, useRef, useState } from "react";
import { getContainerLogs, streamContainerLogs } from "../api/clustersApi.js";
import { appendLogLines, applyLogSnapshot, getIncrementalTailCursor } from "../utils/logFormat.js";
import { buildLogTimeQuery } from "../utils/logTimeRange.js";

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
  useEffect(() => {
    if (!liveEnabled || !hasTarget) {
      return undefined;
    }

    const controller = new AbortController();
    let cancelled = false;
    const currentFilters = filtersRef.current;

    const query = { timestamps: "true", tailLines: "200" };
    const timeQuery = buildLogTimeQuery(currentFilters);
    if (timeQuery.query?.sinceSeconds) {
      query.sinceSeconds = timeQuery.query.sinceSeconds;
    }

    setError("");
    streamContainerLogs(
      currentFilters.cluster,
      currentFilters.namespace,
      currentFilters.pod,
      currentFilters.container,
      {
        query,
        signal: controller.signal,
        onEvent: ({ event, data }) => {
          if (cancelled) {
            return;
          }
          if (event === "line" && data?.line) {
            setLogLines((prev) => appendLogLines(prev, [data.line]));
          } else if (event === "error" && data?.message) {
            setError(data.message);
          }
        },
      }
    )
      .catch((streamError) => {
        if (!cancelled && streamError.name !== "AbortError") {
          setError(streamError.message);
        }
      })
      .finally(() => {
        if (!cancelled) {
          initialLoadDoneRef.current = true;
        }
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [liveEnabled, hasTarget, logTargetKeyValue]);

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
