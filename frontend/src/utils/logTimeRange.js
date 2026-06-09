export const TIME_RANGE_OPTIONS = [
  { value: "15m", label: "Last 15 minutes", sinceSeconds: 900 },
  { value: "1h", label: "Last 1 hour", sinceSeconds: 3600 },
  { value: "6h", label: "Last 6 hours", sinceSeconds: 21600 },
  { value: "24h", label: "Last 24 hours", sinceSeconds: 86400 },
  { value: "custom", label: "Custom range" },
];

export const MAX_CUSTOM_RANGE_MS = 7 * 24 * 60 * 60 * 1000;

export function buildLogTimeQuery({ timeRange, customFrom, customTo }) {
  if (timeRange === "custom") {
    if (!customFrom || !customTo) {
      return {
        error: "Select both start and end times for a custom range.",
      };
    }

    const from = new Date(customFrom);
    const to = new Date(customTo);
    if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime())) {
      return { error: "Invalid custom time range." };
    }
    if (from >= to) {
      return { error: "Start time must be before end time." };
    }
    if (to.getTime() - from.getTime() > MAX_CUSTOM_RANGE_MS) {
      return { error: "Custom range cannot exceed 7 days." };
    }

    return {
      query: {
        sinceTime: from.toISOString(),
        untilTime: to.toISOString(),
      },
    };
  }

  const option = TIME_RANGE_OPTIONS.find((item) => item.value === timeRange);
  if (!option?.sinceSeconds) {
    return { query: {} };
  }

  return {
    query: {
      sinceSeconds: String(option.sinceSeconds),
    },
  };
}
