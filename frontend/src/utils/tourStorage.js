// Coach-mark (guided tips) persistence. Like the theme, this is a per-browser,
// per-user preference kept in localStorage — it never syncs to the workspace
// settings API, so one user's dismissed tips don't affect teammates.

const storageKey = (userId) => `kubesight.coachmarks.v1.${userId}`;

const EMPTY_STATE = Object.freeze({ seen: {}, muted: false });

export function readTourState(userId) {
  if (!userId) {
    return { ...EMPTY_STATE, seen: {} };
  }
  try {
    const raw = localStorage.getItem(storageKey(userId));
    if (!raw) {
      return { ...EMPTY_STATE, seen: {} };
    }
    const parsed = JSON.parse(raw);
    return {
      seen: parsed && typeof parsed.seen === "object" && parsed.seen ? parsed.seen : {},
      muted: parsed?.muted === true,
    };
  } catch {
    return { ...EMPTY_STATE, seen: {} };
  }
}

function writeTourState(userId, state) {
  if (!userId) {
    return;
  }
  try {
    localStorage.setItem(storageKey(userId), JSON.stringify(state));
  } catch {
    /* storage unavailable (private mode) — tips will just reappear */
  }
}

export function markTourSeen(userId, tourKey) {
  if (!tourKey) {
    return;
  }
  const state = readTourState(userId);
  if (state.seen[tourKey]) {
    return;
  }
  writeTourState(userId, { ...state, seen: { ...state.seen, [tourKey]: true } });
}

export function setToursMuted(userId, muted) {
  const state = readTourState(userId);
  writeTourState(userId, { ...state, muted: Boolean(muted) });
}

export function resetTourState(userId) {
  if (!userId) {
    return;
  }
  try {
    localStorage.removeItem(storageKey(userId));
  } catch {
    /* ignore */
  }
}
