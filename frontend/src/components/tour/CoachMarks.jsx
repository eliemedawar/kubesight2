import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const SPOT_PAD = 8;
const POP_GAP = 12;
const EDGE = 12;
const FIND_INTERVAL = 150;
// Targets can appear late (data still loading) — poll this long before
// concluding the element genuinely isn't on the page and skipping the step.
const FIND_TIMEOUT = 2000;
const DEFAULT_POP = { width: 330, height: 170 };

const clamp = (value, min, max) => Math.min(Math.max(value, min), max);

function computePopoverPosition(rect, pop, vw, vh) {
  if (!rect) {
    return {
      top: Math.max(EDGE, vh / 2 - pop.height / 2),
      left: Math.max(EDGE, vw / 2 - pop.width / 2),
      placement: "center",
      arrow: null,
    };
  }
  const spot = {
    top: rect.top - SPOT_PAD,
    left: rect.left - SPOT_PAD,
    width: rect.width + SPOT_PAD * 2,
    height: rect.height + SPOT_PAD * 2,
  };
  const centerX = spot.left + spot.width / 2;
  const centerY = spot.top + spot.height / 2;

  let placement;
  if (spot.top + spot.height + POP_GAP + pop.height <= vh - EDGE) {
    placement = "bottom";
  } else if (spot.top - POP_GAP - pop.height >= EDGE) {
    placement = "top";
  } else if (spot.left + spot.width + POP_GAP + pop.width <= vw - EDGE) {
    placement = "right";
  } else if (spot.left - POP_GAP - pop.width >= EDGE) {
    placement = "left";
  } else {
    placement = "center";
  }

  let top;
  let left;
  let arrow = null;
  if (placement === "bottom" || placement === "top") {
    top = placement === "bottom" ? spot.top + spot.height + POP_GAP : spot.top - POP_GAP - pop.height;
    left = clamp(centerX - pop.width / 2, EDGE, Math.max(EDGE, vw - EDGE - pop.width));
    arrow = { left: clamp(centerX - left, 22, pop.width - 22) };
  } else if (placement === "right" || placement === "left") {
    left = placement === "right" ? spot.left + spot.width + POP_GAP : spot.left - POP_GAP - pop.width;
    top = clamp(centerY - pop.height / 2, EDGE, Math.max(EDGE, vh - EDGE - pop.height));
    arrow = { top: clamp(centerY - top, 22, pop.height - 22) };
  } else {
    top = Math.max(EDGE, vh / 2 - pop.height / 2);
    left = Math.max(EDGE, vw / 2 - pop.width / 2);
  }
  return { top, left, placement, arrow };
}

// Guided coach marks: dims the page, spotlights one element at a time and
// anchors an explainer popover next to it. Steps arrive pre-filtered for the
// user's permissions; as a second safety net any step whose target element
// never appears in the DOM (permission-hidden control, empty state, still
// loading) is skipped automatically after FIND_TIMEOUT.
export default function CoachMarks({
  steps,
  onFinish,
  onDismiss,
  onMuteAuto,
  showMuteOption = false,
}) {
  const [index, setIndex] = useState(0);
  const [rect, setRect] = useState(null);
  const [popSize, setPopSize] = useState(DEFAULT_POP);
  const popRef = useRef(null);

  // Parent passes inline callbacks; keep them in a ref so the effects below
  // don't restart (re-scroll, re-poll) every time the parent re-renders.
  const cbRef = useRef({});
  cbRef.current = { onFinish, onDismiss, onMuteAuto };
  const isLast = index === steps.length - 1;
  const stateRef = useRef({});
  stateRef.current = { isLast };

  const step = steps[index];

  const goNext = () => {
    if (isLast) {
      cbRef.current.onFinish?.();
    } else {
      setIndex(index + 1);
    }
  };
  const goBack = () => setIndex((i) => Math.max(0, i - 1));

  // Locate the target for the current step, keep its rect fresh while the
  // user scrolls/resizes, and skip the step if the element never shows up.
  useEffect(() => {
    if (!step) {
      return undefined;
    }
    let cancelled = false;
    let findTimer = null;
    let measureTimer = null;
    let attached = null;
    let startedAt = Date.now();
    setRect(null);

    const detach = () => {
      if (measureTimer) {
        clearInterval(measureTimer);
        measureTimer = null;
      }
      window.removeEventListener("scroll", onMove, true);
      window.removeEventListener("resize", onMove);
      attached = null;
    };

    const measure = () => {
      if (cancelled || !attached) {
        return;
      }
      if (!attached.isConnected) {
        // Target vanished (tab switch, list refresh) — search again.
        detach();
        setRect(null);
        startedAt = Date.now();
        find();
        return;
      }
      const r = attached.getBoundingClientRect();
      setRect((prev) =>
        prev &&
        Math.abs(prev.top - r.top) < 0.5 &&
        Math.abs(prev.left - r.left) < 0.5 &&
        Math.abs(prev.width - r.width) < 0.5 &&
        Math.abs(prev.height - r.height) < 0.5
          ? prev
          : { top: r.top, left: r.left, width: r.width, height: r.height }
      );
    };

    const onMove = () => measure();

    const attach = (el) => {
      attached = el;
      try {
        el.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
      } catch {
        /* scrollIntoView options unsupported — position math still works */
      }
      measure();
      measureTimer = setInterval(measure, 200);
      window.addEventListener("scroll", onMove, true);
      window.addEventListener("resize", onMove);
    };

    const find = () => {
      if (cancelled) {
        return;
      }
      const el = document.querySelector(step.target);
      if (el) {
        attach(el);
        return;
      }
      if (Date.now() - startedAt >= FIND_TIMEOUT) {
        if (index < steps.length - 1) {
          setIndex((i) => i + 1);
        } else {
          cbRef.current.onFinish?.();
        }
        return;
      }
      findTimer = setTimeout(find, FIND_INTERVAL);
    };

    find();
    return () => {
      cancelled = true;
      if (findTimer) {
        clearTimeout(findTimer);
      }
      detach();
    };
  }, [index, step, steps.length]);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        cbRef.current.onDismiss?.();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        if (stateRef.current.isLast) {
          cbRef.current.onFinish?.();
        } else {
          setIndex((i) => i + 1);
        }
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        setIndex((i) => Math.max(0, i - 1));
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);

  const ready = Boolean(rect);
  useEffect(() => {
    if (ready) {
      popRef.current?.querySelector(".cm-next")?.focus({ preventScroll: true });
    }
  }, [ready, index]);

  // Measure the rendered popover so placement math uses its real size.
  useLayoutEffect(() => {
    if (!popRef.current) {
      return;
    }
    const r = popRef.current.getBoundingClientRect();
    if (Math.abs(r.width - popSize.width) > 1 || Math.abs(r.height - popSize.height) > 1) {
      setPopSize({ width: r.width, height: r.height });
    }
  });

  if (!step) {
    return null;
  }

  const pos = computePopoverPosition(rect, popSize, window.innerWidth, window.innerHeight);

  // The dim is four panels around the spotlight hole rather than one huge
  // box-shadow — Chromium drops shadows with very large spread radii.
  const spot = rect
    ? {
        top: Math.max(0, rect.top - SPOT_PAD),
        left: Math.max(0, rect.left - SPOT_PAD),
        right: rect.left + rect.width + SPOT_PAD,
        bottom: rect.top + rect.height + SPOT_PAD,
      }
    : null;

  return createPortal(
    <div className="coachmarks" role="presentation">
      <div className="cm-blocker" aria-hidden="true" />
      {spot ? (
        <>
          <div className="cm-dim" aria-hidden="true" style={{ top: 0, left: 0, right: 0, height: spot.top }} />
          <div className="cm-dim" aria-hidden="true" style={{ top: spot.bottom, left: 0, right: 0, bottom: 0 }} />
          <div
            className="cm-dim"
            aria-hidden="true"
            style={{ top: spot.top, left: 0, width: spot.left, height: Math.max(0, spot.bottom - spot.top) }}
          />
          <div
            className="cm-dim"
            aria-hidden="true"
            style={{ top: spot.top, left: spot.right, right: 0, height: Math.max(0, spot.bottom - spot.top) }}
          />
          <div
            className="cm-spotlight"
            aria-hidden="true"
            style={{
              top: spot.top,
              left: spot.left,
              width: Math.max(0, spot.right - spot.left),
              height: Math.max(0, spot.bottom - spot.top),
            }}
          />
        </>
      ) : (
        <div className="cm-veil" aria-hidden="true" />
      )}
      {rect ? (
        <section
          ref={popRef}
          className="cm-pop"
          style={{ top: pos.top, left: pos.left }}
          data-placement={pos.placement}
          role="dialog"
          aria-modal="true"
          aria-label={`Tip ${index + 1} of ${steps.length}: ${step.title}`}
        >
          {pos.arrow ? (
            <span
              className="cm-arrow"
              aria-hidden="true"
              style={pos.arrow.left != null ? { left: pos.arrow.left } : { top: pos.arrow.top }}
            />
          ) : null}
          <header className="cm-head">
            <h3>{step.title}</h3>
            <button
              type="button"
              className="cm-close"
              onClick={() => cbRef.current.onDismiss?.()}
              aria-label="Close tour"
            >
              <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path
                  fillRule="evenodd"
                  d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"
                  clipRule="evenodd"
                />
              </svg>
            </button>
          </header>
          <p className="cm-body">{step.body}</p>
          <footer className="cm-foot">
            <div className="cm-foot-meta">
              <span className="cm-count">
                {index + 1} of {steps.length}
              </span>
              {showMuteOption ? (
                <button type="button" className="cm-mute" onClick={() => cbRef.current.onMuteAuto?.()}>
                  Don&rsquo;t show tips automatically
                </button>
              ) : null}
            </div>
            <div className="cm-foot-actions">
              {index > 0 ? (
                <button type="button" className="cm-btn cm-back" onClick={goBack}>
                  Back
                </button>
              ) : null}
              <button type="button" className="cm-btn cm-next" onClick={goNext}>
                {isLast ? "Done" : "Next"}
              </button>
            </div>
          </footer>
        </section>
      ) : null}
    </div>,
    document.body
  );
}
