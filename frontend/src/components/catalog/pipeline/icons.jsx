/**
 * Pipeline editor glyphs. Stroke icons on a 24 grid drawn in currentColor, so
 * every one of them follows the theme and the tone of whatever holds it.
 */

const PATHS = {
  source: (
    <>
      <circle cx="6" cy="5" r="2.25" />
      <circle cx="6" cy="19" r="2.25" />
      <circle cx="18" cy="7" r="2.25" />
      <path d="M6 7.25v9.5M18 9.25c0 4.5-6 3.5-10.5 7.75" />
    </>
  ),
  terminal: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2.5" />
      <path d="m7.5 9.5 3 2.5-3 2.5M13 15h3.5" />
    </>
  ),
  image: (
    <>
      <path d="M12 2.8 20.5 7.5v9L12 21.2 3.5 16.5v-9z" />
      <path d="M3.5 7.5 12 12.2l8.5-4.7M12 12.2v9" />
    </>
  ),
  rocket: (
    <>
      <path d="M13.5 15.5 8.5 10.5c1.6-4.1 5-7 10.5-7.3.2 0 .3.1.3.3-.3 5.5-3.2 8.9-7.3 10.5z" />
      <path d="M8.5 10.5 5 10l2.2-3.1h3.3M13.5 15.5l.5 3.5 3.1-2.2v-3.3" />
      <circle cx="15" cy="9" r="1.4" />
      <path d="M6.5 15.5c-1.3.6-2 2.4-2 4 1.6 0 3.4-.7 4-2" />
    </>
  ),
  cluster: (
    <>
      <path d="M12 3 20 7.5v9L12 21l-8-4.5v-9z" />
      <circle cx="12" cy="12" r="2.5" />
      <path d="M12 3v6.5M20 7.5l-5.8 3.3M20 16.5l-5.8-3.3M12 21v-6.5M4 16.5l5.8-3.3M4 7.5l5.8 3.3" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  grip: (
    <>
      <circle cx="9" cy="6" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="15" cy="6" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="9" cy="12" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="15" cy="12" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="9" cy="18" r="1.1" fill="currentColor" stroke="none" />
      <circle cx="15" cy="18" r="1.1" fill="currentColor" stroke="none" />
    </>
  ),
  copy: (
    <>
      <rect x="8.5" y="8.5" width="12" height="12" rx="2" />
      <path d="M15.5 8.5V5.5a2 2 0 0 0-2-2h-8a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h3" />
    </>
  ),
  trash: (
    <path d="M4 6.5h16M9 6.5V4.8c0-.7.6-1.3 1.3-1.3h3.4c.7 0 1.3.6 1.3 1.3v1.7M18 6.5l-.8 12.4a1.8 1.8 0 0 1-1.8 1.6H8.6a1.8 1.8 0 0 1-1.8-1.6L6 6.5M10 10.5v6M14 10.5v6" />
  ),
  up: <path d="m6 14.5 6-6 6 6" />,
  down: <path d="m6 9.5 6 6 6-6" />,
  chevron: <path d="m9 6 6 6-6 6" />,
  power: <path d="M12 3v8M7.1 6.1a8 8 0 1 0 9.8 0" />,
  branch: (
    <>
      <path d="M6 3v6a6 6 0 0 0 6 6h6" />
      <path d="m15 12 3 3-3 3" />
      <path d="M6 9v12" />
    </>
  ),
  forward: (
    <>
      <path d="M4 12h13" />
      <path d="m13 7 5 5-5 5" />
      <path d="M21 5v14" />
    </>
  ),
  shield: (
    <>
      <path d="M12 3 4.5 6v5.5c0 4.6 3.1 8.2 7.5 9.5 4.4-1.3 7.5-4.9 7.5-9.5V6z" />
      <path d="m8.8 12 2.2 2.2 4.3-4.4" />
    </>
  ),
  key: (
    <>
      <circle cx="8" cy="15" r="4" />
      <path d="m10.9 12.1 8.6-8.6M16.5 6.5l2.5 2.5M14 9l2 2" />
    </>
  ),
  alert: (
    <>
      <path d="M10.3 4.2 2.8 17.5A2 2 0 0 0 4.5 20.5h15a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z" />
      <path d="M12 9.5v4M12 17h.01" />
    </>
  ),
  check: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  x: <path d="M18 6 6 18M6 6l12 12" />,
  variable: (
    <>
      <path d="M8 4c-2 0-3 1-3 3v2c0 1.5-.8 2.5-2 3 1.2.5 2 1.5 2 3v2c0 2 1 3 3 3" />
      <path d="M16 4c2 0 3 1 3 3v2c0 1.5.8 2.5 2 3-1.2.5-2 1.5-2 3v2c0 2-1 3-3 3" />
      <path d="m9.5 9.5 5 5M14.5 9.5l-5 5" />
    </>
  ),
  file: (
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5M9 13h6M9 17h4" />
    </>
  ),
  network: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z" />
    </>
  ),
  server: (
    <>
      <rect x="3.5" y="4" width="17" height="7" rx="2" />
      <rect x="3.5" y="13" width="17" height="7" rx="2" />
      <path d="M7.5 7.5h.01M7.5 16.5h.01" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3.2 2" />
    </>
  ),
  inputs: (
    <>
      <rect x="3" y="4.5" width="18" height="5" rx="1.5" />
      <rect x="3" y="14.5" width="18" height="5" rx="1.5" />
      <path d="M6.5 7h5M6.5 17h3" />
    </>
  ),
  stages: (
    <>
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="6" cy="18" r="2.5" />
      <path d="M6 8.5v7M11 6h9M11 18h9M11 12h6" />
    </>
  ),
  more: (
    <>
      <circle cx="5.5" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="18.5" cy="12" r="1.2" fill="currentColor" stroke="none" />
    </>
  ),
  upload: (
    <>
      <path d="M12 15V3.5M7.5 8 12 3.5 16.5 8" />
      <path d="M4 14.5v3.5a2.5 2.5 0 0 0 2.5 2.5h11a2.5 2.5 0 0 0 2.5-2.5v-3.5" />
    </>
  ),
  reset: (
    <>
      <path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1" />
      <path d="M3.5 3.5v5h5" />
    </>
  ),
  undo: (
    <>
      <path d="M9 14 4 9l5-5" />
      <path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" />
    </>
  ),
  sparkle: (
    <path d="M12 3.5c.5 3.9 2.6 6 6.5 6.5-3.9.5-6 2.6-6.5 6.5-.5-3.9-2.6-6-6.5-6.5 3.9-.5 6-2.6 6.5-6.5zM18.5 15.5c.2 1.6 1 2.4 2.5 2.5-1.5.2-2.3 1-2.5 2.5-.2-1.5-1-2.3-2.5-2.5 1.5-.1 2.3-.9 2.5-2.5z" />
  ),
  eye: (
    <>
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" />
      <circle cx="12" cy="12" r="2.8" />
    </>
  ),
  lock: (
    <>
      <rect x="5" y="10.5" width="14" height="10" rx="2" />
      <path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" />
    </>
  ),
  link: (
    <>
      <path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1" />
      <path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1" />
    </>
  ),
  refresh: (
    <>
      <path d="M20 11.5A8 8 0 0 0 6.3 6.3L4 8.5" />
      <path d="M4 4v4.5h4.5" />
      <path d="M4 12.5a8 8 0 0 0 13.7 5.2l2.3-2.2" />
      <path d="M20 20v-4.5h-4.5" />
    </>
  ),
  pullRequest: (
    <>
      <circle cx="6" cy="5.5" r="2.25" />
      <circle cx="6" cy="18.5" r="2.25" />
      <circle cx="18" cy="18.5" r="2.25" />
      <path d="M6 7.75v8.5M18 16.25V9.5a3 3 0 0 0-3-3h-4" />
      <path d="m13 4-2.5 2.5L13 9" />
    </>
  ),
  message: (
    <path d="M20 14.5a2 2 0 0 1-2 2H8.5L4 20.5V5.5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z" />
  ),
  gauge: (
    <>
      <path d="M4.2 17.5a9 9 0 1 1 15.6 0" />
      <path d="m12 13 4-5" />
      <circle cx="12" cy="13.5" r="1.3" fill="currentColor" stroke="none" />
    </>
  ),
  history: (
    <>
      <path d="M3.5 12a8.5 8.5 0 1 0 2.5-6" />
      <path d="M3.5 4v4.5H8" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
};

export function PlIcon({ name, size, className, title }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : "true"}
      role={title ? "img" : undefined}
      className={className}
      width={size}
      height={size}
    >
      {title && <title>{title}</title>}
      {PATHS[name] || PATHS.terminal}
    </svg>
  );
}
