import { useEffect, useId, useRef, useState } from "react";
import { IconMoreVertical } from "./icons.jsx";

/**
 * Compact action menu shared by field cards and section headers.
 *
 * Keeping secondary/destructive actions here makes the common action obvious
 * while preserving keyboard access and an explicit danger treatment.
 */
export default function ZohoActionMenu({ label, items, onAction, align = "right" }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return undefined;
    const closeOutside = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    const closeOnEscape = (event) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  if (!items?.length) return null;

  return (
    <div className="sg-zh-menu" ref={rootRef}>
      <button
        type="button"
        className="btn-ghost sg-zh-menu-trigger"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen((value) => !value)}
      >
        <IconMoreVertical />
      </button>
      {open ? (
        <div
          id={menuId}
          className={`sg-zh-menu-popover sg-zh-menu-popover--${align}`}
          role="menu"
        >
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              className={item.danger ? "sg-zh-menu-danger" : ""}
              disabled={item.disabled}
              onClick={() => {
                setOpen(false);
                onAction(item.key);
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
