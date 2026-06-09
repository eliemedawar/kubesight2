/** Leading kubectl / RFC3339 timestamp on a log line. */
const LOG_LINE_PREFIX =
  /^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}(?::?\d{2})?))\s+(.*)$/;

export const MAX_LOG_BUFFER_LINES = 8000;

/** Common log level tokens in container output. */
export const LOG_LEVELS = ["ERROR", "WARN", "INFO", "DEBUG", "TRACE", "FATAL"];

/** Kubernetes probe access-log noise (readiness/liveness hit /health on different schedules). */
const HEALTH_PROBE_LINE =
  /"(?:GET|HEAD)\s+\/(?:health|healthz|readyz|livez)(?:\?[^\s"]*)?\s+HTTP\//i;

const LOGS_API_SELF_LINE =
  /"(?:GET|HEAD)\s+\/api\/(?:logs(?:\?[^\s"]*)?|clusters\/[^"]+\/logs(?:\?[^\s"]*)?)\s+HTTP\//i;

const LOGS_API_SELF_LINE_PLAIN =
  /(?:GET|HEAD)\s+\/api\/(?:logs(?:\?|\s)|clusters\/.+\/containers\/[^/\s]+\/logs(?:\?|\s))/i;

export function isHealthProbeLogLine(line) {
  return HEALTH_PROBE_LINE.test(String(line ?? ""));
}

export function isLogsApiSelfLine(line) {
  const text = String(line ?? "");
  return LOGS_API_SELF_LINE.test(text) || LOGS_API_SELF_LINE_PLAIN.test(text);
}

export function filterHealthProbeLogLines(lines) {
  if (!Array.isArray(lines)) {
    return [];
  }
  return lines.filter((line) => !isHealthProbeLogLine(line));
}

export function filterLogsApiSelfLines(lines) {
  if (!Array.isArray(lines)) {
    return [];
  }
  return lines.filter((line) => !isLogsApiSelfLine(line));
}

export function filterLiveLogNoise(lines) {
  if (!Array.isArray(lines)) {
    return [];
  }
  const materialized = lines.filter(Boolean);
  const withoutSelf = filterLogsApiSelfLines(materialized);
  const withoutNoise = filterHealthProbeLogLines(withoutSelf);
  if (withoutNoise.length) {
    return withoutNoise;
  }
  if (withoutSelf.length) {
    return withoutSelf;
  }
  if (materialized.some(isLogsApiSelfLine)) {
    return [];
  }
  return materialized;
}

function pad(value, width = 2) {
  return String(value).padStart(width, "0");
}

function normalizeTimestampToken(token) {
  return String(token ?? "").trim().replace(" ", "T");
}

export function parseLogLineTimestamp(line) {
  const text = String(line ?? "");
  const match = text.match(LOG_LINE_PREFIX);
  if (!match) {
    return null;
  }
  const parsed = new Date(normalizeTimestampToken(match[1]));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatDisplayTime(date) {
  return [
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`,
  ].join(" ");
}

/** Flask/Werkzeug access log timestamp embedded in the message body. */
const WERKZEUG_ACCESS_TIMESTAMP =
  /\s\[\d{2}\/[A-Za-z]{3}\/\d{4} \d{2}:\d{2}:\d{2}\]/;

const WERKZEUG_ACCESS_TIMESTAMP_GLOBAL =
  /\[(\d{2})\/([A-Za-z]{3})\/(\d{4}) (\d{2}):(\d{2}):(\d{2})\]/g;

const MONTH_INDEX = {
  Jan: 0,
  Feb: 1,
  Mar: 2,
  Apr: 3,
  May: 4,
  Jun: 5,
  Jul: 6,
  Aug: 7,
  Sep: 8,
  Oct: 9,
  Nov: 10,
  Dec: 11,
};

function parseWerkzeugUtcTimestamp(day, monthLabel, year, hour, minute, second) {
  const month = MONTH_INDEX[monthLabel];
  if (month === undefined) {
    return null;
  }
  const parsed = new Date(
    Date.UTC(Number(year), month, Number(day), Number(hour), Number(minute), Number(second))
  );
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function stripEmbeddedAccessLogTimestamp(message) {
  return message.replace(WERKZEUG_ACCESS_TIMESTAMP, "");
}

function convertEmbeddedTimestampsToLocal(message) {
  return message.replace(
    WERKZEUG_ACCESS_TIMESTAMP_GLOBAL,
    (full, day, monthLabel, year, hour, minute, second) => {
      const parsed = parseWerkzeugUtcTimestamp(day, monthLabel, year, hour, minute, second);
      if (!parsed) {
        return full;
      }
      return `[${formatDisplayTime(parsed)}]`;
    }
  );
}

function trimLogBuffer(lines) {
  if (lines.length <= MAX_LOG_BUFFER_LINES) {
    return lines;
  }
  return lines.slice(lines.length - MAX_LOG_BUFFER_LINES);
}

export function sortLogLinesByTimestamp(lines) {
  return [...lines]
    .map((line, index) => ({ line, index, timestamp: parseLogLineTimestamp(line) }))
    .sort((left, right) => {
      if (left.timestamp && right.timestamp) {
        const delta = left.timestamp.getTime() - right.timestamp.getTime();
        if (delta !== 0) {
          return delta;
        }
      } else if (left.timestamp) {
        return -1;
      } else if (right.timestamp) {
        return 1;
      }
      return left.index - right.index;
    })
    .map((entry) => entry.line);
}

/**
 * Rewrites a log line's leading UTC timestamp into a fixed local display format.
 * When kubectl already timestamps the line, strip duplicate app timestamps (e.g. Werkzeug).
 */
export function formatLogLineToLocalTime(line) {
  const text = String(line ?? "");
  const match = text.match(LOG_LINE_PREFIX);
  if (!match) {
    return convertEmbeddedTimestampsToLocal(text);
  }

  const parsed = new Date(normalizeTimestampToken(match[1]));
  if (Number.isNaN(parsed.getTime())) {
    return text;
  }

  const message = stripEmbeddedAccessLogTimestamp(match[2]);
  return `${formatDisplayTime(parsed)} ${message}`;
}

/** Return the raw RFC3339 token from a log line for incremental tail polling. */
export function extractLogTimestamp(line) {
  const match = String(line ?? "").match(LOG_LINE_PREFIX);
  return match ? normalizeTimestampToken(match[1]) : null;
}

export function formatRfc3339Utc(date) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) {
    return null;
  }
  return date.toISOString();
}

/** Cursor for kubectl --since-time: newest line + 1ms to avoid repeating the tail line. */
export function buildIncrementalSinceTime(token) {
  const normalized = normalizeTimestampToken(token);
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  return formatRfc3339Utc(new Date(parsed.getTime() + 1));
}

export function getNewestLogTimestamp(lines) {
  let newestToken = null;
  let newestTime = null;

  for (const line of lines) {
    const token = extractLogTimestamp(line);
    const parsed = parseLogLineTimestamp(line);
    if (!token || !parsed) {
      continue;
    }
    if (!newestTime || parsed.getTime() >= newestTime.getTime()) {
      newestTime = parsed;
      newestToken = token;
    }
  }

  return newestToken;
}

export function getIncrementalTailCursor(lines) {
  const newest = getNewestLogTimestamp(lines);
  return newest ? buildIncrementalSinceTime(newest) : null;
}

export function applyLogSnapshot(lines, { hideNoise = true } = {}) {
  if (!Array.isArray(lines) || !lines.length) {
    return null;
  }
  const visibleLines = hideNoise ? filterLiveLogNoise(lines) : lines;
  return trimLogBuffer(sortLogLinesByTimestamp(visibleLines));
}

/**
 * Append only new log lines for live streaming (no full-history reload).
 * Deduplicates by exact line text and preserves chronological order.
 */
export function appendLogLines(existing, incoming, { hideNoise = true } = {}) {
  const base = Array.isArray(existing) ? existing : [];
  if (!Array.isArray(incoming) || !incoming.length) {
    return base;
  }

  const visibleIncoming = hideNoise ? filterLiveLogNoise(incoming) : incoming;
  if (!visibleIncoming.length) {
    return base;
  }

  const seen = new Set(base);
  const newLines = visibleIncoming.filter((line) => !seen.has(line));
  if (!newLines.length) {
    return base;
  }

  return trimLogBuffer(sortLogLinesByTimestamp([...base, ...newLines]));
}

export function formatLogLinesToLocalTime(lines) {
  if (!Array.isArray(lines)) {
    return [];
  }
  return lines.map(formatLogLineToLocalTime);
}

export function getLogDisplayTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
  } catch {
    return "local time";
  }
}

/** Detect log level from line text (returns uppercase level or null). */
export function detectLogLevel(line) {
  const text = String(line ?? "");
  const match = text.match(/\b(ERROR|WARN|WARNING|INFO|DEBUG|TRACE|FATAL)\b/i);
  if (!match) {
    return null;
  }
  const level = match[1].toUpperCase();
  return level === "WARNING" ? "WARN" : level;
}

export function filterLogLinesBySearch(lines, searchText) {
  if (!searchText?.trim()) {
    return lines;
  }
  const needle = searchText.trim().toLowerCase();
  return lines.filter((line) => String(line).toLowerCase().includes(needle));
}

export function filterLogLinesByLevel(lines, levelFilter) {
  if (!levelFilter || levelFilter === "all") {
    return lines;
  }
  const target = levelFilter.toUpperCase();
  return lines.filter((line) => {
    const level = detectLogLevel(line);
    if (!level) {
      return target === "OTHER";
    }
    if (target === "WARN") {
      return level === "WARN" || level === "WARNING";
    }
    return level === target;
  });
}

export function stripLogTimestamps(lines) {
  return lines.map((line) => {
    const match = String(line).match(LOG_LINE_PREFIX);
    return match ? match[2] : line;
  });
}
