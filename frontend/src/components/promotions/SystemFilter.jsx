import { useEffect, useRef, useState } from "react";

import { PrIcon } from "./icons.jsx";

/** Multi-select of systems, with a search — an estate has dozens of them. */
export default function SystemFilter({ systems, selected, onChange }) {
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (event) => ref.current && !ref.current.contains(event.target) && setOpen(false);
    const onKey = (event) => event.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const set = new Set(selected);
  const shown = systems.filter((s) => s.name.toLowerCase().includes(term.trim().toLowerCase()));
  const toggle = (name) => {
    const next = new Set(set);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    onChange([...next]);
  };
  const label = !selected.length ? "All systems" : selected.length === 1 ? selected[0] : `${selected.length} systems`;

  return (
    <div className="pr-sysfilter" ref={ref}>
      <button
        type="button"
        className={`btn-ghost pr-sysfilter-btn${selected.length ? " is-on" : ""}`}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <PrIcon.Layers />
        {label}
        <PrIcon.ChevronDown />
      </button>
      {open && (
        <div className="pr-sysfilter-pop" role="dialog" aria-label="Filter by system">
          <div className="pr-sysfilter-search">
            <PrIcon.Search />
            <input autoFocus value={term} onChange={(event) => setTerm(event.target.value)} placeholder="Find a system" />
          </div>
          <div className="pr-sysfilter-list">
            {shown.map((system) => (
              <label key={system.name} className="pr-sysfilter-opt">
                <input type="checkbox" checked={set.has(system.name)} onChange={() => toggle(system.name)} />
                <span>{system.name}</span>
                <em>{system.count}</em>
              </label>
            ))}
            {!shown.length && <p className="pr-muted">No system matches.</p>}
          </div>
          <div className="pr-sysfilter-foot">
            <button type="button" className="btn-ghost pr-link" onClick={() => onChange([])} disabled={!selected.length}>
              Clear
            </button>
            <button type="button" className="btn-ghost pr-link" onClick={() => setOpen(false)}>
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
