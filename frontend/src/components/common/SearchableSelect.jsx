import {
  Children,
  isValidElement,
  useEffect,
  useId,
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
  allowCustom = false,
  customOptionLabel = (customValue) => `Use "${customValue}"`,
  "aria-label": ariaLabel,
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [dropPos, setDropPos] = useState(null);
  // Keyboard highlight within the visible options (-1 = none).
  const [activeIndex, setActiveIndex] = useState(-1);
  const triggerRef = useRef(null);
  const dropRef = useRef(null);
  const searchRef = useRef(null);
  const listId = useId();

  const items = useMemo(() => buildOptions(options, children), [options, children]);
  const showSearch = allowCustom || items.length > searchThreshold;

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

  // The panel renders hidden until it is positioned, and a hidden input cannot
  // take focus — so focus the search once it is visible, letting typing start
  // straight away without clicking into it.
  const positioned = Boolean(dropPos);
  useEffect(() => {
    if (open && positioned) searchRef.current?.focus({ preventScroll: true });
  }, [open, positioned]);

  useEffect(() => {
    if (!open) return undefined;
    const handler = (e) => {
      if (
        triggerRef.current && !triggerRef.current.contains(e.target) &&
        dropRef.current && !dropRef.current.contains(e.target)
      ) setOpen(false);
    };
    // Escape pressed inside the select is handled (and stopped) by
    // handleOpenKeyDown; this catches it when focus has wandered elsewhere.
    // Capture phase, and the key is claimed, so the app-wide dialog stack
    // (listening on document) leaves the dialog around the select open.
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  const selected = items.find((o) => sameValue(o.value, value))
    || (
      allowCustom && value != null && !sameValue(value, "")
        ? {
            value,
            label: value,
            text: String(value),
            disabled: false,
          }
        : undefined
    );
  const hasValue = selected && !sameValue(selected.value, "");
  const triggerLabel = selected ? selected.label : placeholder;

  const needle = search.trim().toLowerCase();
  const filtered = needle
    ? items.filter((o) => o.text.toLowerCase().includes(needle))
    : items;
  const customItem = allowCustom && search.trim() && !items.some(
    (item) => sameValue(item.value, search.trim()),
  )
    ? {
        value: search.trim(),
        label: typeof customOptionLabel === "function"
          ? customOptionLabel(search.trim())
          : customOptionLabel,
        text: search.trim(),
        disabled: false,
        custom: true,
      }
    : null;
  const visibleItems = customItem ? [...filtered, customItem] : filtered;

  // Opening (or changing the search) highlights the current value, else the
  // first option, so arrows and Enter work straight away.
  useEffect(() => {
    if (!open) {
      setActiveIndex(-1);
      return;
    }
    const current = visibleItems.findIndex((o) => sameValue(o.value, value) && !o.disabled);
    setActiveIndex(current >= 0 ? current : visibleItems.findIndex((o) => !o.disabled));
    // visibleItems is derived from search; re-running on it every render would
    // undo arrow-key moves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, search]);

  useEffect(() => {
    if (!open || activeIndex < 0) return;
    dropRef.current
      ?.querySelector(`[data-ss-index="${activeIndex}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [open, activeIndex, positioned]);

  const closeAndRefocus = () => {
    setOpen(false);
    triggerRef.current?.focus({ preventScroll: true });
  };

  const choose = (opt) => {
    if (opt.disabled) return;
    closeAndRefocus();
    onChange?.({ target: { value: opt.value, name } });
  };

  const moveActive = (step) => {
    if (!visibleItems.length) return;
    let next = activeIndex;
    for (let i = 0; i < visibleItems.length; i += 1) {
      next = next < 0 ? (step > 0 ? 0 : visibleItems.length - 1) : next + step;
      next = Math.min(Math.max(next, 0), visibleItems.length - 1);
      if (!visibleItems[next].disabled) break;
    }
    if (!visibleItems[next]?.disabled) setActiveIndex(next);
  };

  // Keys while the panel is open — from the search box, or from the trigger
  // when the list is short enough to have no search box.
  const handleOpenKeyDown = (e) => {
    if (!open) return;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        moveActive(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        moveActive(-1);
        break;
      case "Home":
      case "End":
        if (e.target === searchRef.current) return; // caret moves in the search box
        e.preventDefault();
        setActiveIndex(e.key === "Home" ? 0 : visibleItems.length - 1);
        break;
      case "Enter":
        e.preventDefault();
        if (visibleItems[activeIndex]) choose(visibleItems[activeIndex]);
        break;
      case " ":
        if (e.target === searchRef.current) return; // a space is part of the search
        e.preventDefault();
        if (visibleItems[activeIndex]) choose(visibleItems[activeIndex]);
        break;
      case "Escape":
        // Stop here so a dialog around the select does not close as well.
        e.preventDefault();
        e.stopPropagation();
        closeAndRefocus();
        break;
      case "Tab":
        setOpen(false);
        break;
      default:
    }
  };

  return (
    <div className={`ss-wrap${className ? ` ${className}` : ""}`} id={id}>
      <button
        ref={triggerRef}
        type="button"
        className={`ss-trigger${open ? " ss-trigger--open" : ""}${disabled ? " ss-trigger--disabled" : ""}`}
        onClick={() => !disabled && setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (disabled) return;
          if (open) {
            handleOpenKeyDown(e);
            return;
          }
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            setOpen(true);
            return;
          }
          // Typing on the closed trigger opens it with that letter already searched.
          if (!showSearch) return;
          if (e.key.length !== 1 || e.ctrlKey || e.metaKey || e.altKey || e.key === " ") return;
          e.preventDefault();
          setSearch(e.key);
          setOpen(true);
        }}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={
          open && !showSearch && activeIndex >= 0 ? `${listId}-${activeIndex}` : undefined
        }
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
          id={listId}
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
                ref={searchRef}
                className="ss-search"
                autoFocus
                placeholder={searchPlaceholder}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={handleOpenKeyDown}
                onMouseDown={(e) => e.stopPropagation()}
                role="combobox"
                aria-expanded="true"
                aria-controls={listId}
                aria-activedescendant={activeIndex >= 0 ? `${listId}-${activeIndex}` : undefined}
                aria-label={searchPlaceholder}
              />
            </div>
          )}
          <div className="ss-list">
            {visibleItems.length === 0 ? (
              <div className="ss-empty">No results</div>
            ) : (
              visibleItems.map((opt, i) => (
                <div
                  key={`${opt.custom ? "custom-" : ""}${String(opt.value)}-${i}`}
                  id={`${listId}-${i}`}
                  data-ss-index={i}
                  role="option"
                  aria-selected={sameValue(opt.value, value)}
                  aria-disabled={opt.disabled || undefined}
                  className={`ss-option${sameValue(opt.value, value) ? " ss-option--selected" : ""}${i === activeIndex ? " ss-option--active" : ""}${opt.disabled ? " ss-option--disabled" : ""}`}
                  onMouseEnter={() => !opt.disabled && setActiveIndex(i)}
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
