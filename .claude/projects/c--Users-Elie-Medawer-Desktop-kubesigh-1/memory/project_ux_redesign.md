---
name: project-ux-redesign
description: Enterprise UX redesign applied to KubeSight — design system tokens, dark sidebar, blue accent, skeleton loaders
metadata:
  type: project
---

## KubeSight Enterprise UX Redesign (2026-06-11)

**Why:** Product was using red (#dc2626) as both brand accent AND danger color — visually confusing in a monitoring tool where red must mean error.

### Design System Changes Applied to index.css
- **Primary accent** changed from `#dc2626` (red) → `#2563eb` (professional blue)
- **Sidebar** now uses dark navy `#0f172a` background (like Grafana/Portainer pattern)
- Danger colors (`--danger`, `--danger-soft`, `--danger-border`) preserved as red — only used for actual error states
- New CSS variables: `--danger-border`, `--danger-hover`, sidebar dark token set (`--sidebar-bg`, `--sidebar-nav-*`, etc.)
- Border radius tightened: `0.375rem / 0.5rem / 0.625rem` (was `0.5 / 0.625 / 0.75`)
- Added `--font-size-xs`, `--font-size-md` to typography scale
- Better shadows: `--shadow-sm/md/lg` with slate-based rgba
- Added skeleton loader keyframe + `.skeleton`, `.skeleton-text`, `.skeleton-row` utility classes
- Login screen redesigned: dark background matching sidebar, professional card
- Topbar: flat bottom-border only (removed rounded card style)
- Status dots: `.status-dot--ok/warn/danger/unknown` with optional pulse animation
- Table toolbar pattern: `.table-toolbar`, `.table-toolbar__search`, `.table-count`
- Tag/chip system: `.tag`, `.tag--blue/green/amber/red`
- Empty state pattern: `.empty-state`, `.empty-state__icon/title/description`
- Styled scrollbars (webkit) for consistent thin scrollbar across the app

### Fixed Color Conflicts
All `rgba(220, 38, 38, ...)` brand-as-hover colors changed to `rgba(37, 99, 235, ...)` in:
- inventory-app-card hover, inventory-hub-table row hover, inventory-icon-btn hover
- template-card hover, data-table-row--clickable hover
- app-details-tabs hover/active, wizard-stepper active, focus rings

**How to apply:** When adding new CSS, always use `--accent` for interactive/selected states and `--danger` only for actual error/destructive states. Never use hardcoded `#dc2626` or `rgba(220, 38, 38)`.

[[project-readiness-review]]
