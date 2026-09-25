import { useEffect, useMemo, useRef, useState } from "react";
import { navIcon } from "./navIcons.jsx";

function score(target, query) {
  if (!query) return 1;
  const label = target.label.toLowerCase();
  const hay = `${label} ${target.context.toLowerCase()}`;
  if (label.startsWith(query)) return 3;
  if (label.includes(query)) return 2;
  // Every word of the query somewhere in label or context.
  return query.split(/\s+/).every((word) => hay.includes(word)) ? 1 : 0;
}

/**
 * Ctrl/⌘+K: type a few letters, Enter, you are there. Lists every page and
 * workspace tab the user may open — the same set the sidebar shows.
 */
export default function JumpPalette({ open, targets, onClose, onSelect }) {
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      window.requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    return targets
      .map((target, index) => ({ target, index, s: score(target, q) }))
      .filter((row) => row.s > 0)
      .sort((a, b) => b.s - a.s || a.index - b.index)
      .map((row) => row.target);
  }, [targets, query]);

  useEffect(() => {
    setCursor(0);
  }, [query]);

  useEffect(() => {
    listRef.current
      ?.querySelector(`[data-index="${cursor}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  if (!open) return null;

  const choose = (target) => {
    if (!target) return;
    onSelect(target);
  };

  const handleKeyDown = (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setCursor((c) => Math.min(c + 1, Math.max(results.length - 1, 0)));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(results[cursor]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  };

  return (
    <div className="jump-overlay" role="presentation" onMouseDown={onClose}>
      <div
        className="jump-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Jump to a page"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="jump-input-row">
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
            <circle cx="8.5" cy="8.5" r="5.5" />
            <path d="m13 13 4 4" />
          </svg>
          <input
            ref={inputRef}
            className="jump-input"
            type="text"
            placeholder="Jump to a page…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={handleKeyDown}
            role="combobox"
            aria-expanded="true"
            aria-controls="jump-results"
            aria-activedescendant={results[cursor] ? `jump-opt-${cursor}` : undefined}
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="jump-esc">Esc</kbd>
        </div>
        <ul id="jump-results" className="jump-results" role="listbox" ref={listRef}>
          {results.length ? (
            results.map((target, index) => (
              <li
                key={target.id}
                id={`jump-opt-${index}`}
                data-index={index}
                role="option"
                aria-selected={index === cursor}
                className={`jump-option${index === cursor ? " is-cursor" : ""}`}
                onMouseMove={() => setCursor(index)}
                onClick={() => choose(target)}
              >
                <span className="jump-option-icon">{navIcon(target.pageKey)}</span>
                <span className="jump-option-label">{target.label}</span>
                <span className="jump-option-context">{target.context}</span>
              </li>
            ))
          ) : (
            <li className="jump-empty">No page matches “{query}”.</li>
          )}
        </ul>
      </div>
    </div>
  );
}
