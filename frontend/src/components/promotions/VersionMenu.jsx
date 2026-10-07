import { useEffect, useRef } from "react";

import { PrIcon } from "./icons.jsx";

/**
 * "Choose version" for one application at one gate: every version running
 * below the target, nearest environment first. A version from further down
 * skips environments — it is marked, and the release asks for a reason.
 */
export default function VersionMenu({ choices, current, onPick, onClose }) {
  const ref = useRef(null);
  useEffect(() => {
    const onDown = (event) => {
      if (ref.current && !ref.current.contains(event.target)) onClose();
    };
    const onKey = (event) => event.key === "Escape" && onClose();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    ref.current?.querySelector("button")?.focus();
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  return (
    <div className="pr-vmenu" ref={ref} role="menu" aria-label="Versions to deploy">
      <p className="pr-vmenu-title">Deploy which version?</p>
      {choices.length === 0 && <p className="pr-vmenu-empty">Nothing runs below this environment.</p>}
      {choices.map((choice) => (
        <button
          key={choice.image}
          type="button"
          role="menuitemradio"
          aria-checked={choice.image === current}
          className={`btn-ghost pr-vmenu-item${choice.image === current ? " is-on" : ""}`}
          onClick={() => onPick(choice)}
        >
          <span className="pr-ver pr-ver--new">{choice.tag}</span>
          <span className="pr-vmenu-where">
            in {choice.environmentName}
            {!choice.healthy && <em> · not healthy</em>}
          </span>
          {choice.skips.length > 0 ? (
            <span className="pr-skip">skips {choice.skips.join(", ")}</span>
          ) : (
            <span className="pr-vmenu-next">next step</span>
          )}
          {choice.image === current && <PrIcon.Check />}
        </button>
      ))}
      <p className="pr-vmenu-foot">
        A version that skips an environment is sent as an exception: you give a reason, someone else approves.
      </p>
    </div>
  );
}
