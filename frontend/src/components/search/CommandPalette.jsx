import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { kindLabel, searchCommands } from "./commandSearch.js";

/**
 * Ctrl/Cmd+K.
 *
 * Now that every destination has a URL, going somewhere is a string away, and
 * the palette is the fastest path to it — faster than expanding a nav group and
 * reading a list. It only offers what the user could already reach, by
 * construction rather than by filtering; see commandSearch.js.
 */
export default function CommandPalette({ visiblePages, clusters, namespaces, clusterId }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef(null);
  const navigate = useNavigate();

  const results = useMemo(
    () => searchCommands(query, { visiblePages, clusters, namespaces, clusterId }),
    [query, visiblePages, clusters, namespaces, clusterId]
  );

  // Reset the highlight whenever the result set changes, so Enter never fires
  // the row that happened to be at that index a keystroke ago.
  useEffect(() => {
    setActive(0);
  }, [query]);

  useEffect(() => {
    const onKeyDown = (event) => {
      const isToggle = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k";
      if (isToggle) {
        event.preventDefault();
        setOpen((current) => !current);
        setQuery("");
        return;
      }
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    }
  }, [open]);

  if (!open) {
    return null;
  }

  const go = (result) => {
    if (!result?.href) {
      return;
    }
    setOpen(false);
    setQuery("");
    navigate(result.href);
  };

  const onInputKeyDown = (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => Math.min(current + 1, results.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) => Math.max(current - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      go(results[active]);
    }
  };

  return (
    <div
      className="modal-backdrop command-backdrop"
      role="presentation"
      onClick={() => setOpen(false)}
    >
      <div
        className="command-palette"
        role="dialog"
        aria-modal="true"
        aria-label="Search"
        onClick={(event) => event.stopPropagation()}
      >
        <input
          ref={inputRef}
          type="text"
          className="command-input"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onInputKeyDown}
          placeholder="Search pages, clusters, namespaces…"
          aria-label="Search pages, clusters and namespaces"
          aria-controls="command-results"
          autoComplete="off"
          spellCheck={false}
        />

        <ul className="command-results" id="command-results" role="listbox">
          {results.map((result, index) => (
            <li key={result.id} role="option" aria-selected={index === active}>
              <button
                type="button"
                className={`command-result${index === active ? " is-active" : ""}`}
                onClick={() => go(result)}
                onMouseEnter={() => setActive(index)}
              >
                <span className="command-result-label">{result.label}</span>
                {result.hint ? (
                  <span className="command-result-hint muted">{result.hint}</span>
                ) : null}
                <span className="command-result-kind muted">{kindLabel(result.kind)}</span>
              </button>
            </li>
          ))}
          {!results.length ? (
            <li className="command-empty muted">
              Nothing matches “{query}”. Only pages you have access to are searched.
            </li>
          ) : null}
        </ul>
      </div>
    </div>
  );
}
