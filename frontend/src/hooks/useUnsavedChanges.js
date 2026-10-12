import { useCallback, useEffect, useRef } from "react";
import { registerGuard } from "../routes/leaveGuard.js";

/**
 * Ask before a dirty editor is left behind.
 *
 *   const { release } = useUnsavedChanges(dirty, {
 *     message: "Your pipeline changes are not saved. Leave and discard them?",
 *   });
 *
 * While `isDirty` is true the page holds a leave guard: the router asks before
 * any move that would lose the draft (sidebar, Ctrl+K, a link, Back/Forward,
 * a typed address) and the browser asks before the tab closes or reloads.
 *
 * Options (read at the moment of asking, so they may change every render):
 * - `message`: the question. One sentence that names what would be lost.
 * - `blocks(next, current)`: which moves lose the draft. Default: another
 *   page, tab or record; a query-only change (a drawer, a filter) does not.
 *   `leavesPage` from routes/leaveGuard.js suits drafts that survive the
 *   page's own tabs.
 * - `onDiscard()`: the user chose to leave; forget the draft.
 * - `unload`: false when the draft survives a reload by itself.
 *
 * `release()` drops the guard at once. Call it right after a successful save
 * that navigates in the same handler: React has not re-rendered yet, so the
 * guard would still see the old dirty state and ask for nothing. It comes back
 * by itself the next time the editor turns dirty.
 */
export default function useUnsavedChanges(isDirty, options = {}) {
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const unregisterRef = useRef(null);

  useEffect(() => {
    if (!isDirty) {
      return undefined;
    }
    const unregister = registerGuard({
      get message() {
        return optionsRef.current.message;
      },
      get unload() {
        return optionsRef.current.unload;
      },
      blocks: (next, current) => {
        const { blocks } = optionsRef.current;
        return blocks ? blocks(next, current) : undefined;
      },
      onDiscard: () => {
        unregisterRef.current = null;
        optionsRef.current.onDiscard?.();
      },
    });
    unregisterRef.current = unregister;
    return () => {
      unregister();
      unregisterRef.current = null;
    };
  }, [isDirty]);

  const release = useCallback(() => {
    unregisterRef.current?.();
    unregisterRef.current = null;
  }, []);

  return { release };
}
