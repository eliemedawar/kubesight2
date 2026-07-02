# KubeSight Design System — MASTER (v2 "Mission Control")

Global source of truth for the premium enterprise redesign. Every page and component follows
these rules. Page-specific overrides live in `design-system/pages/<page>.md` (none yet).

## 1. Identity

- **Feel:** mission-control console — calm, dense-but-breathable, precise. Grafana/Datadog/Linear tier.
- **Dark-first.** Deep blue-slate canvas, layered lighter panels, hairline borders, restrained glow.
- **Light theme** is a first-class citizen: white panels on `#f6f8fb` canvas, slate hairlines, soft shadows.
- Red is **only** for danger/error. Blue = interactive/brand. Green = healthy. Amber = warning.
  Cyan = info. Violet = AI (Hermes) only.

## 2. Fonts

Loaded via Google Fonts `<link>` in `index.html` (graceful system fallback for air-gapped installs):

- `--font-sans`: `"Inter"` — UI text (body, tables, forms). Weights 400/500/600/700.
- `--font-display`: `"Plus Jakarta Sans"` — page titles, stat values, brand. Weights 600/700/800.
- `--font-mono`: `"IBM Plex Mono"` — YAML, logs, IDs, versions, namespaces, kbd.

Numeric data in tables/stats: `font-variant-numeric: tabular-nums`.

## 3. Color tokens (CSS variables in `frontend/src/index.css`)

### Dark (default, `:root`)
| Token | Value | Use |
|---|---|---|
| `--bg-main` | `#0a0f1c` | app canvas |
| `--bg-panel` | `#111927` | cards, table shells, topbar |
| `--bg-panel-strong` | `#16202f` | nested panels, table header |
| `--bg-elevated` | `#1c2839` | modals, dropdowns, popovers |
| `--bg-interactive` | `#243146` | hover fills, inputs hover |
| `--bg-inset` | `#070b13` | code/log wells, YAML editors |
| `--text-strong` | `#f8fafc` | stat values, headings emphasis |
| `--text-main` | `#e6ecf4` | primary text |
| `--text-subtle` | `#b6c2d4` | secondary text |
| `--text-muted` | `#7c8ba1` | hints, labels, timestamps |
| `--accent` | `#3b82f6` (hover `#2f6fe0`, text-on-dark `--accent-strong: #71a5f9`) | interactive |
| `--ok` | `#26c165` | healthy |
| `--warn` | `#f5a623` | degraded |
| `--danger` | `#f04444` (hover `#d63333`) | error/destructive |
| `--info` | `#22b8dd` | informational |
| `--ai` | `#8b5cf6` (+ `--ai-soft`) | Hermes AI accents only |
| `--border` | `rgba(148,163,199,0.16)` | default hairline |
| `--border-soft` | `rgba(148,163,199,0.09)` | dividers |
| `--border-strong` | `rgba(148,163,199,0.28)` | inputs, focused edges |

Each status color has `-soft` (12% alpha fill) and `-border` (30% alpha) variants.
`--highlight: inset 0 1px 0 rgba(255,255,255,0.045)` — top inner highlight on panels/modals (dark only, `none` in light).
Chart palette `--chart-1..8`: blue `#3b82f6`, cyan `#22d3ee`, violet `#8b5cf6`, green `#34d399`, amber `#fbbf24`, rose `#fb7185`, indigo `#818cf8`, teal `#2dd4bf`.

### Light (`[data-theme="light"]`)
Canvas `#f4f6fa`, panel `#ffffff`, panel-strong `#f8fafc`, elevated `#ffffff`, interactive `#eef2f7`,
inset `#f1f5f9`; text strong `#0b1324` main `#1a2436` subtle `#42506a` muted `#64748b`;
accent `#2563eb`; borders `rgba(15,23,42,0.10/0.06/0.20)`; shadows slate-tinted, soft.

## 4. Shape, depth, motion

- Radius: `--radius-sm .375rem` (inputs, tags), `--radius-md .5rem` (buttons, rows), `--radius-lg .75rem` (cards, modals), `--radius-xl 1rem` (hero/login card), `--radius-full 999px` (pills, dots, avatar).
- Shadows (dark): `sm` = subtle 1-2px; `md` = dropdowns/hover-lift; `lg` = modals/drawers. Always pair panels with `--highlight`.
- Focus: `--ring: 0 0 0 3px var(--accent-soft)` + `border-color: var(--accent)`. `:focus-visible` on EVERY interactive element; never remove outlines without replacement.
- Motion: `--ease: cubic-bezier(.16,1,.3,1)`; durations `--dur-fast 120ms`, `--dur 200ms`, `--dur-slow 320ms`. Hover = color/border/shadow only (no layout shift, no scale on rows). Modals/dropdowns animate in with 6-8px translate + fade at `--dur`. Respect `prefers-reduced-motion` (kill all transitions/animations).

## 5. Type scale

`--font-size-xs .75rem` meta/labels · `sm .8125rem` table body, nav · `base .875rem` body ·
`md .9375rem` card titles · `lg 1.125rem` section headings · `xl 1.5rem` page titles ·
`2xl 1.875rem` stat values/login. Page titles: display font, weight 700, `letter-spacing -0.025em`.
Uppercase labels (eyebrow, section labels, table headers): `xs`/`0.6875rem`, weight 600, `letter-spacing .07em`, muted.

## 6. Component rules

### Buttons
- Base: `--radius-md`, `0.5rem 0.95rem`, weight 600, `--font-size-sm`, inline-flex + `gap .45rem`, `cursor: pointer`, transition `--dur-fast`.
- `.primary`: accent fill, white text, subtle shadow `0 1px 2px rgba(0,0,0,.3), inset 0 1px 0 rgba(255,255,255,.12)`; hover = `--accent-hover` (never scale).
- default/secondary: panel bg + `--border`, hover `--bg-interactive` + `--border-strong`.
- `.btn-outline`: transparent, accent border+text, hover `--accent-soft`.
- `.btn-ghost`: borderless muted, hover interactive fill.
- `.btn-danger`: danger fill (destructive confirm only); ghost-danger for row deletes.
- Disabled: `opacity .5`, `cursor: not-allowed`. Async: disable + spinner, keep label.

### Cards (`.card`, InfoCard, stat cards, every box)
- `--bg-panel`, 1px `--border`, `--radius-lg`, `--shadow-sm` + `--highlight`, padding `--space-5`.
- Card header: title `--font-size-md`/600 + optional muted sub; actions right-aligned; hairline divider `--border-soft` below when body follows.
- Interactive cards: hover → `--border-strong` + `--shadow-md`; no transform.
- Stat cards: label = uppercase eyebrow; value = display font `--font-size-2xl`/700 tabular; delta/trend chip right; tone expressed by a 3px left inset bar or icon chip, not full-bleed background.

### Tables
- Wrap in `.table-shell` card (flush padding). Sticky header: `--bg-panel-strong`, uppercase xs muted labels, `border-bottom: 1px solid var(--border)`.
- Rows: `--font-size-sm`, 44px min height, divider `--border-soft`, hover `--bg-interactive` (fast), clickable rows get `cursor:pointer`.
- Toolbar above: search input (with icon) + filters left, count + actions right.
- Numeric cells tabular + right-aligned. IDs/versions in mono. Pagination: ghost buttons + `Page x of y` muted.
- Empty table → EmptyState component, never a bare "No data" row.

### Status
- `.status-pill`: soft fill + border + colored dot, `--radius-full`, xs/600. `.status-dot--ok/warn/danger/unknown` 8px, optional pulse for live/critical.
- Severity always icon/dot + word, never color alone.

### Forms
- Inputs/selects/textareas: `--bg-panel` (inset well for code), 1px `--border-strong`, `--radius-md`, padding `.5rem .75rem`, placeholder muted; focus = accent border + `--ring`.
- Labels above, `--font-size-sm`/600; hint xs muted below; error = danger border + xs danger message with icon near field.
- Section grouping inside long forms: eyebrow + divider. Danger zones: danger-soft panel + danger border.

### Modals / drawers
- Backdrop `rgba(4,8,16,.62)` + `backdrop-filter: blur(4px)`. Panel: `--bg-elevated`, `--radius-lg`, `--shadow-lg` + highlight, max-height 85vh with internal scroll.
- Header: title md/700 + close ghost icon-button; footer right-aligned actions (primary rightmost), hairline dividers.
- Wizards: numbered stepper — active step accent fill circle, done = ok check, upcoming = muted outline; connector lines hairline.

### Navigation
- Sidebar: `--sidebar-bg #0a0e18` (both themes dark — brand surface), width `15rem`. Brand block: logo mark + "KubeSight" in display font + env subtitle. Section labels: uppercase xs. Links: icon 18px + label sm/500, `--radius-md`, hover soft white fill, active = `--accent-soft` fill + `--accent-strong` text + 2px accent left bar. Footer: version, muted.
- Topbar: flat bar on canvas (border-bottom hairline only, no floating card): context selectors (cluster/namespace) as compact labeled fields, right: notifications bell + theme toggle + user block with avatar.

### Feedback
- Skeletons: `--bg-interactive` base with shimmer sweep 1.6s; provided `.skeleton`, `.skeleton-text`, `.skeleton-row`; reserve layout space (no jumps).
- EmptyState: centered icon in soft circle, title md/600, description sm muted, optional primary action. Always actionable when user can create the missing thing.
- Errors: `ErrorBanner` danger-soft fill + danger border + icon + retry where applicable.
- Loading >300ms → skeleton or spinner with label.

### Topology
- Nodes: panel fill, 1px border, `--radius-md`, status = 3px left bar + dot; selected = accent border + ring. Edges `--border-strong`, animated dash for active flows only. Canvas bg `--bg-inset` with subtle dot-grid.

### Logs / code
- `--bg-inset` well, mono sm, line-height 1.65; severity-tinted line prefixes; sticky log toolbar.

## 7. Hard rules

1. **No hardcoded colors in JSX/CSS** — tokens only (SVG `currentColor` where possible). Canvas/JS chart code reads tokens via `getComputedStyle` helper or `lib/colors.js` constants that mirror tokens.
2. No emoji as icons — inline SVG (Lucide-style, 1.5px stroke, 24 viewBox).
3. `cursor: pointer` + hover feedback + `:focus-visible` ring on everything clickable.
4. No layout-shifting hovers, no scale transforms on rows/cards.
5. Both themes verified for every change (contrast ≥ 4.5:1 body text).
6. Transitions 120–320ms with `--ease`; honor `prefers-reduced-motion`.
7. Tables must not break page width — scroll inside `.table-shell`.
8. Touch targets ≥ 40px in toolbars, ≥ 44px on mobile.
