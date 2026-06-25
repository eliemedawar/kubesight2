import {
  Children,
  isValidElement,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

// Flatten a React node (option label) into a plain searchable string.
function flattenText(node) {
  if (node == null || node === false || node === true) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(flattenText).join("");
  if (isValidElement(node)) return flattenText(node.props?.children);
  return "";
}

// Normalize either an `options` array prop or native <option>/<optgroup> children
// into a flat list of { value, label, text, disabled }.
function buildOptions(options, children) {
  if (Array.isArray(options)) {
    return options.map((opt) => {
      if (opt && typeof opt === "object") {
        const label = opt.label ?? opt.value;
        return {
          value: opt.value,
          label,
          text: flattenText(label),
          disabled: Boolean(opt.disabled),
        };
      }
      return { value: opt, label: opt, text: flattenText(opt), disabled: false };
    });
  }

  const out = [];
  const walk = (nodes) => {
    Children.forEach(nodes, (child) => {
      if (!isValidElement(child)) return;
      if (child.type === "optgroup") {
        walk(child.props.children);
        return;
      }
      if (child.type === "option") {
        const label = child.props.children;
        const value = child.props.value !== undefined ? child.props.value : flattenText(label);
        out.push({
          value,
          label,
          text: flattenText(label),
          disabled: Boolean(child.props.disabled),
        });
        return;
      }
      // Unwrap fragments / conditional wrappers that still contain options.
      if (child.props && child.props.children) walk(child.props.children);
    });
  };
  walk(children);
  return out;
}

const sameValue = (a, b) => a === b || String(a ?? "") === String(b ?? "");

/**
 * Drop-in replacement for a native <select>. Renders a styled trigger plus a
 * floating panel; once there are more than `searchThreshold` options a search
 * box appears at the top. `onChange` is called with a native-like event object
 * ({ target: { value, name } }) so existing handlers using e.target.value work.
 */
export default function SearchableSelect({
  value,
  onChange,
  disabled = false,
  required = false,
  className = "",
  id,
  name,
  options,
  children,
  placeholder = "Select…",
  searchThreshold = 6,
  searchPlaceholder = "Search…",
  "aria-label": ariaLabel,
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [dropPos, setDropPos] = useState(null);
  const triggerRef = useRef(null);
  const dropRef = useRef(null);

  const items = useMemo(() => buildOptions(options, children), [options, children]);
  const showSearch = items.length > searchThreshold;

  useLayoutEffect(() => {
    if (!open) {
      setSearch("");
      setDropPos(null);
      return undefined;
    }

    const reposition = () => {
      const trigger = triggerRef.current;
      if (!trigger) return;
      const rect = trigger.getBoundingClientRect();
      const margin = 8;
      const gap = 3;
      const dropH = dropRef.current?.offsetHeight ?? 0;
      const spaceBelow = window.innerHeight - rect.bottom - margin;
      const spaceAbove = rect.top - margin;

      let top;
      let maxHeight;
      if (dropH <= spaceBelow || spaceBelow >= spaceAbove) {
        top = rect.bottom + gap;
        maxHeight = Math.max(120, spaceBelow);
      } else {
        maxHeight = Math.max(120, spaceAbove);
        top = rect.top - gap - Math.min(dropH, maxHeight);
      }

      const width = rect.width;
      let left = rect.left;
      if (left + width > window.innerWidth - margin) {
        left = Math.max(margin, window.innerWidth - margin - width);
      }
      left = Math.max(margin, left);

      setDropPos({ top, left, width, maxHeight });
    };

    reposition();
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open, search]);

  useEffect(() => {
    if (!open) return undefined;
    const handler = (e) => {
      if (
        triggerRef.current && !triggerRef.current.contains(e.target) &&
        dropRef.current && !dropRef.current.contains(e.target)
      ) setOpen(false);
    };
    const onKey = (e) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const selected = items.find((o) => sameValue(o.value, value));
  const hasValue = selected && !sameValue(selected.value, "");
  const triggerLabel = selected ? selected.label : placeholder;

  const needle = search.trim().toLowerCase();
  const filtered = needle
    ? items.filter((o) => o.text.toLowerCase().includes(needle))
    : items;

  const choose = (opt) => {
    if (opt.disabled) return;
    setOpen(false);
    onChange?.({ target: { value: opt.value, name } });
  };

  return (
    <div className={`ss-wrap${className ? ` ${className}` : ""}`} id={id}>
      <button
        ref={triggerRef}
        type="button"
        className={`ss-trigger${open ? " ss-trigger--open" : ""}${disabled ? " ss-trigger--disabled" : ""}`}
        onClick={() => !disabled && setOpen((v) => !v)}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className={hasValue ? "ss-value" : "ss-placeholder"}>{triggerLabel}</span>
        <span className="ss-arrow">▾</span>
      </button>
      {required ? (
        <input
          className="ss-required-mirror"
          tabIndex={-1}
          aria-hidden="true"
          value={value ?? ""}
          onChange={() => {}}
          required
        />
      ) : null}
      {open && (
        <div
          ref={dropRef}
          className="ss-dropdown"
          role="listbox"
          style={{
            position: "fixed",
            top: dropPos?.top ?? 0,
            left: dropPos?.left ?? 0,
            minWidth: dropPos?.width,
            maxHeight: dropPos?.maxHeight,
            visibility: dropPos ? "visible" : "hidden",
          }}
        >
          {showSearch && (
            <div className="ss-search-wrap">
              <input
                className="ss-search"
                autoFocus
                placeholder={searchPlaceholder}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onMouseDown={(e) => e.stopPropagation()}
              />
            </div>
          )}
          <div className="ss-list">
            {filtered.length === 0 ? (
              <div className="ss-empty">No results</div>
            ) : (
              filtered.map((opt, i) => (
                <div
                  key={`${String(opt.value)}-${i}`}
                  role="option"
                  aria-selected={sameValue(opt.value, value)}
                  className={`ss-option${sameValue(opt.value, value) ? " ss-option--selected" : ""}${opt.disabled ? " ss-option--disabled" : ""}`}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    choose(opt);
                  }}
                >
                  {opt.label}
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
