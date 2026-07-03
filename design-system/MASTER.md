# KubeSight Design System — MASTER (v3 "Signal")

Global source of truth. Built on the areeba identity: white space as the ground, ink text,
hairline borders, and **one confident red used as the instrument of attention**. Page-specific
overrides live in `design-system/pages/<page>.md` (none yet).

Interactive concept reference (9 mocked screens): the "Signal" artifact —
https://claude.ai/code/artifact/76be5afd-9f0f-4a87-8bb6-3ef5c437be96

## 1. Identity

- **Feel:** premium enterprise control plane in broad daylight — calm, dense-but-breathable, precise.
- **Light-first.** Warm paper canvas, white panels, warm-ink text, hairline borders, soft warm shadows.
- **Dark theme** is a first-class citizen: warm neutral ink surfaces (no blue cast), same red voice.
- **Red is the brand accent** (#FF2929): identity, THE primary action, active navigation, unread-critical
  counts. Semantic status stays separate — green = healthy, amber = degraded, crimson = danger — so a
  red button never reads as an outage and an outage never looks like a button.
- **The circle is the brand:** pill buttons, full-round tags/status, round dots and avatars, ring gauges.
- Violet = AI (Hermes) only. Info = cyan-teal.

## 2. Fonts

Self-hosted via `src/styles/fonts.css` (no CDN):

- `--font-sans`: `"Inter"` — UI text (body, tables, forms). Weights 400/500/600/700.
- `--font-display`: `"Plus Jakarta Sans"` — page titles, stat values, brand. Weights 600/700/800.
- `--font-mono`: `"IBM Plex Mono"` — machine truth ONLY: image tags, namespaces, versions, crons, IDs, kbd.

Numeric data in tables/stats: `font-variant-numeric: tabular-nums`.

## 3. Color tokens (CSS variables in `frontend/src/index.css`)

### Light (default, `:root`)
| Token | Value | Use |
|---|---|---|
| `--bg-main` | `#f8f7f5` | app canvas (warm paper) |
| `--bg-panel` | `#ffffff` | cards, table shells, topbar, sidebar |
| `--bg-panel-strong` | `#faf9f7` | nested panels, table header |
| `--bg-elevated` | `#ffffff` | modals, dropdowns, popovers |
| `--bg-interactive` | `#f3f2ef` | hover fills, inputs hover |
| `--bg-inset` | `#f4f3f0` | code/log wells, YAML editors |
| `--text-strong` | `#131417` | stat values, headings emphasis |
| `--text-main` | `#1a1b1e` | primary text |
| `--text-subtle` | `#494b52` | secondary text |
| `--text-muted` | `#6e7076` | hints, labels, timestamps |
| `--accent` | `#ff2929` (hover `#e31b1b`) | brand red — fills, active states |
| `--accent-strong` | `#d71f1f` | red as TEXT on white (5:1 AA) |
| `--ok` | `#178a45` | healthy |
| `--warn` | `#a66104` | degraded |
| `--danger` | `#d71f1f` (hover `#b31212`) | error/destructive |
| `--info` | `#0e7490` | informational |
| `--ai` | `#7c3aed` (+ `--ai-soft`) | Hermes AI accents only |
| `--border` | `#e9e7e3` | default hairline (warm) |
| `--border-soft` | `#f0efeb` | dividers |
| `--border-strong` | `#d8d6d0` | inputs, focused edges |

Each accent/status color has `-soft` (~10% alpha fill) and `-border` (~30% alpha) variants.
Shadows are warm-tinted (`rgba(26,27,30,…)`), `--highlight: none` in light.
Focus `--ring: 0 0 0 3px rgba(255,41,41,0.15)`.

Chart palette `--chart-1..8` (fixed categorical order, CVD-validated, red first):
`#ff2929`, `#2563eb`, `#d97706`, `#0d9488`, `#7c3aed`, `#059669`, `#4f46e5`, `#0891b2`.

### Dark (`[data-theme="dark"]`)
Warm neutral ink — canvas `#101112`, panel `#17181a`, panel-strong `#1c1d20`, elevated `#222327`,
interactive `#2a2b30`, inset `#0b0c0d`; text strong `#fafaf9` main `#ececea` subtle `#b9bab6`
muted `#85868c`; accent `#ff3b3b` (text-on-dark `--accent-strong: #ff7a70`); status colors
dark-tuned (`--ok #2fbe6b`, `--warn #f0a32b`, `--danger #ff4d4d`, `--info #2cb8dc`); borders
warm-white alphas; `--highlight` restored; charts brightened (`#ff5a52`, `#5b8df8`, …).
Sidebar tokens are overridden here (dark `#141416` surface, red-soft active).

### Sidebar (light default — white brand surface)
White bg, hairline border, muted ink nav text; hover = `#f3f2ef` fill; **active = `#fff1ef`
red-soft pill + `#d71f1f` text + `#ff2929` left bar**. Brand mark: red circle (red→red-ink
gradient) with white mark; brand wordmark solid `--sidebar-brand-text` (never gradient text).

## 4. Shape, depth, motion

- Radius: `--radius-sm .5rem` (tags, small wells), `--radius-md .75rem` (inputs, rows),
  `--radius-lg 1.125rem` (cards, modals — the 18px signature), `--radius-xl 1.375rem` (login/hero),
  `--radius-full 999px` (**all buttons**, pills, dots, avatars).
- **Buttons are pills** (`border-radius: var(--radius-full)`) — base border is `--border-strong`.
- Shadows: `sm` subtle 1px; `md` dropdowns/hover-lift; `lg` modals/drawers. Warm-tinted, soft.
- `.primary` carries a red glow: `0 2px 10px -2px rgba(255,41,41,0.45)`.
- Focus: red `--ring` + `border-color: var(--accent)`. `:focus-visible` on EVERY interactive element.
- Motion: `--ease: cubic-bezier(.16,1,.3,1)`; durations 120/200/320ms. Hover = color/border/shadow
  only (no layout shift, no scale). Respect `prefers-reduced-motion`.

## 5. Type scale

Unchanged from v2: `xs .75rem` meta · `sm .8125rem` table body/nav · `base .875rem` body ·
`md .9375rem` card titles · `lg 1.125rem` section headings · `xl 1.5rem` page titles ·
`2xl 1.875rem` stat values. Page titles: display font, 700–800, `letter-spacing -0.025em`.
Uppercase labels: xs, weight 600–700, `letter-spacing .07em+`, muted.

## 6. Component rules

### Attention model (three tiers)
1. **Act now** — brand red: one primary action per view, critical counters, active nav.
2. **Read this** — ink: headings, key values, row titles. Emphasis via weight/size, never color.
3. **Ambient** — muted grays: metadata, timestamps, hints.

### Buttons
- All pill-shaped. `.primary`: red fill, white text, red glow; hover `--accent-hover`. **One
  primary per view.** Secondary: white + `--border-strong`. `.btn-outline`: red border/text, hover
  red-soft. `.btn-ghost`: borderless muted. `.btn-danger`: crimson fill (destructive confirm only).
- Disabled: `opacity .5`. Async: disable + spinner, keep label.

### Cards
- White panel, 1px warm hairline, `--radius-lg` (18px), `--shadow-sm`, padding `--space-5`.
- Interactive cards: hover → `--border-strong` + `--shadow-md`; no transform.
- Stat cards: uppercase eyebrow label; display-font tabular value; tone via 3px left bar or icon
  chip (soft fill + status color), never full-bleed background.

### Tables
- `.table-shell` card (flush). Sticky header `--bg-panel-strong` + uppercase xs muted labels.
- Rows ≥44px, hairline dividers, hover `--bg-interactive`, clickable rows `cursor:pointer`.
- Numeric cells tabular + right-aligned; IDs/images/versions in mono; empty → EmptyState.

### Status
- `.status-pill`: soft fill + border + colored dot, full-round, xs/600–650. Severity always
  dot/icon + word — never color alone. Pulse animation reserved for live/critical.

### Forms
- Inputs: white bg, `--border-strong` hairline, `--radius-md`, focus = red border + red ring.
- Labels above (sm/600); hints xs muted; errors = danger border + xs danger message with icon.

### Modals / drawers
- Backdrop warm `rgba(23,21,18,.42)` + blur. Panel `--bg-elevated`, `--radius-lg`, `--shadow-lg`.
- Wizards: numbered stepper — active = red fill circle, done = ok check, upcoming = muted outline.

### Navigation
- Sidebar: white (see §3); section labels uppercase xs; links icon + label, radius-md,
  active = red-soft pill + red text + red left bar.
- Topbar: flat white bar, hairline bottom border; context selectors as compact pill fields;
  right: notifications bell (red badge), theme toggle, user block.

### Terminals / logs
- Pod-exec terminal stays dark in BOTH themes (`#101113` well) — it is a terminal.
- Log/YAML wells elsewhere use `--bg-inset` (theme-aware), mono sm, severity-tinted prefixes.

### Charts
- Colors read from `--chart-*` via `getComputedStyle` (chartDraw.js) — never hardcoded.
- Categorical hues assigned in fixed order (red is chart-1 = the brand voice); status colors in
  charts only for status meanings.

## 7. Hard rules

1. **No hardcoded colors in JSX/CSS** — tokens only. Charts read tokens at draw time.
2. **Red is budgeted.** Brand red = identity, one primary action, active nav, critical counts.
   If red starts appearing anywhere else, the screen loses its siren.
3. No emoji as icons — inline SVG only.
4. `cursor: pointer` + hover feedback + `:focus-visible` ring on everything clickable.
5. No layout-shifting hovers, no scale transforms on rows/cards.
6. Both themes verified for every change (body text contrast ≥ 4.5:1; red text on white uses
   `--accent-strong`, never raw `#ff2929`).
7. Tables must not break page width — scroll inside `.table-shell`.
8. Touch targets ≥ 40px in toolbars, ≥ 44px on mobile.
9. Machine truth wears mono: image tags, namespaces, crons, node names, build numbers.
