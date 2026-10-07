/* Line icons for the Promotions page. 20×20, currentColor, 1.6 stroke. */

const base = {
  viewBox: "0 0 20 20",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
};

export const PrIcon = {
  Ladder: () => (
    <svg {...base}>
      <path d="M3 17h4v-4h4V9h4V5h2" />
      <path d="m14.5 2.5 2.5 2.5-2.5 2.5" />
    </svg>
  ),
  Arrow: () => (
    <svg {...base}>
      <path d="M4 10h12M12 6l4 4-4 4" />
    </svg>
  ),
  Up: () => (
    <svg {...base}>
      <path d="M10 16V4M5.5 8.5 10 4l4.5 4.5" />
    </svg>
  ),
  Lock: () => (
    <svg {...base}>
      <rect x="4.5" y="9" width="11" height="8" rx="1.5" />
      <path d="M7 9V6.5a3 3 0 0 1 6 0V9" />
    </svg>
  ),
  Bell: () => (
    <svg {...base}>
      <path d="M5 13.5V9a5 5 0 0 1 10 0v4.5l1.5 1.5h-13z" />
      <path d="M8.5 17.5a1.6 1.6 0 0 0 3 0" />
    </svg>
  ),
  Off: () => (
    <svg {...base}>
      <circle cx="10" cy="10" r="6.5" />
      <path d="m5.5 14.5 9-9" />
    </svg>
  ),
  Check: () => (
    <svg {...base}>
      <path d="m4.5 10.5 3.5 3.5 7.5-8" />
    </svg>
  ),
  Equal: () => (
    <svg {...base}>
      <path d="M5 8h10M5 12h10" />
    </svg>
  ),
  Clock: () => (
    <svg {...base}>
      <circle cx="10" cy="10" r="6.5" />
      <path d="M10 6.5V10l2.5 1.5" />
    </svg>
  ),
  Hourglass: () => (
    <svg {...base}>
      <path d="M6 3h8M6 17h8M6.5 3c0 3.5 7 4 7 7s-7 3.5-7 7M13.5 3c0 3.5-7 4-7 7s7 3.5 7 7" />
    </svg>
  ),
  Warn: () => (
    <svg {...base}>
      <path d="M10 3.5 17 16H3z" />
      <path d="M10 8.5v3.5M10 14.2v.1" />
    </svg>
  ),
  Stop: () => (
    <svg {...base}>
      <path d="M7 3h6l4 4v6l-4 4H7l-4-4V7z" />
      <path d="M7.5 10h5" />
    </svg>
  ),
  Hand: () => (
    <svg {...base}>
      <path d="M7 10V4.5a1.2 1.2 0 0 1 2.4 0V9M9.4 8.5V3.5a1.2 1.2 0 0 1 2.4 0V9M11.8 8.8V4.8a1.2 1.2 0 0 1 2.4 0v6.7c0 3-2 5-5 5-2.2 0-3.4-1-4.6-3L3 10.6a1.2 1.2 0 0 1 2-1.3L7 12" />
    </svg>
  ),
  Dot: () => (
    <svg {...base}>
      <circle cx="10" cy="10" r="3" fill="currentColor" stroke="none" />
    </svg>
  ),
  Refresh: () => (
    <svg {...base}>
      <path d="M16 10a6 6 0 1 1-1.8-4.3M16 3.5v3.5h-3.5" />
    </svg>
  ),
  Plus: () => (
    <svg {...base}>
      <path d="M10 4v12M4 10h12" />
    </svg>
  ),
  X: () => (
    <svg {...base}>
      <path d="m5 5 10 10M15 5 5 15" />
    </svg>
  ),
  Search: () => (
    <svg {...base}>
      <circle cx="9" cy="9" r="5" />
      <path d="m13 13 3.5 3.5" />
    </svg>
  ),
  Grip: () => (
    <svg {...base}>
      <path d="M7.5 5h.01M12.5 5h.01M7.5 10h.01M12.5 10h.01M7.5 15h.01M12.5 15h.01" strokeWidth="2.4" />
    </svg>
  ),
  ChevronUp: () => (
    <svg {...base}>
      <path d="m5.5 12 4.5-4.5 4.5 4.5" />
    </svg>
  ),
  ChevronDown: () => (
    <svg {...base}>
      <path d="m5.5 8 4.5 4.5L14.5 8" />
    </svg>
  ),
  Cluster: () => (
    <svg {...base}>
      <circle cx="10" cy="10" r="2.5" />
      <circle cx="4.5" cy="5" r="1.5" />
      <circle cx="15.5" cy="5" r="1.5" />
      <circle cx="4.5" cy="15" r="1.5" />
      <circle cx="15.5" cy="15" r="1.5" />
      <path d="m6 6.2 2.2 2.2M14 6.2l-2.2 2.2M6 13.8l2.2-2.2M14 13.8l-2.2-2.2" />
    </svg>
  ),
  ChevronLeft: () => (
    <svg {...base}>
      <path d="m12 5.5-4.5 4.5 4.5 4.5" />
    </svg>
  ),
  ChevronRight: () => (
    <svg {...base}>
      <path d="m8 5.5 4.5 4.5L8 14.5" />
    </svg>
  ),
  Layers: () => (
    <svg {...base}>
      <path d="m10 3 7 3.5-7 3.5-7-3.5z" />
      <path d="m3 10 7 3.5 7-3.5M3 13.5 10 17l7-3.5" />
    </svg>
  ),
  Asterisk: () => (
    <svg {...base}>
      <path d="M10 4v12M4.8 7l10.4 6M4.8 13l10.4-6" />
    </svg>
  ),
  Box: () => (
    <svg {...base}>
      <rect x="4" y="5" width="12" height="10" rx="1.5" />
      <path d="M4 8.5h12" />
    </svg>
  ),
  Flag: () => (
    <svg {...base}>
      <path d="M5 17V3.5M5 4h9l-2 3 2 3H5" />
    </svg>
  ),
  Pause: () => (
    <svg {...base}>
      <path d="M7.5 5v10M12.5 5v10" />
    </svg>
  ),
  Skip: () => (
    <svg {...base}>
      <path d="m5 5 6 5-6 5zM14.5 5v10" />
    </svg>
  ),
  Train: () => (
    <svg {...base}>
      <rect x="5" y="3" width="10" height="11" rx="2.5" />
      <path d="M5 9h10M7.5 17l-1.5 1.5M12.5 17l1.5 1.5M8 14v1.5M12 14v1.5" />
      <path d="M7.8 11.6h.01M12.2 11.6h.01" strokeWidth="2.2" />
    </svg>
  ),
  Trash: () => (
    <svg {...base}>
      <path d="M4 6h12M8 6V4.5h4V6M5.5 6l.8 10h7.4l.8-10" />
    </svg>
  ),
};

export function ModeIcon({ mode }) {
  if (mode === "entry") return <PrIcon.Flag />;
  if (mode === "enforce") return <PrIcon.Lock />;
  if (mode === "warn") return <PrIcon.Bell />;
  return <PrIcon.Off />;
}
