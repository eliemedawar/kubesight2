"""
Retune src/index.css from Signal v3 to Cardinal v4.

Token values only. No selector is added or removed here, so every rule that
already routes through a variable picks the new system up for free.

Run from frontend/:  python scripts/cardinal-tokens.py
"""
import io
import sys

PATH = "src/index.css"

LIGHT = [
    # Surfaces: cool neutral with a faint cardinal bias, and a firmer step
    # between the canvas and a panel so cards read as objects again.
    ("  --bg-main: #f8f7f5;", "  --bg-main: #f3f1f4;"),
    ("  --bg-panel-strong: #faf9f7;", "  --bg-panel-strong: #faf9fb;"),
    ("  --bg-interactive: #f3f2ef;", "  --bg-interactive: #f0eef3;"),
    ("  --bg-inset: #f4f3f0;", "  --bg-inset: #f5f4f7;"),

    # Ink
    ("  --text-strong: #131417;", "  --text-strong: #100e14;"),
    ("  --text-main: #1a1b1e;", "  --text-main: #141218;"),
    ("  --text-subtle: #494b52;", "  --text-subtle: #413d49;"),
    ("  --text-muted: #6e7076;", "  --text-muted: #6c6776;"),

    # Cardinal. Wine shifted, 5.8:1 on white, so it can be typeset.
    ("  --accent: #ff2929;", "  --accent: #c41e3a;"),
    ("  --accent-hover: #e31b1b;", "  --accent-hover: #a3162f;"),
    ("  --accent-strong: #d71f1f; /* red as TEXT on white — 5:1 */",
     "  --accent-strong: #b01834; /* red as TEXT on white, 5.8:1 */"),
    ("  --accent-soft: rgba(255, 41, 41, 0.09);", "  --accent-soft: rgba(196, 30, 58, 0.08);"),
    ("  --accent-border: rgba(255, 41, 41, 0.35);", "  --accent-border: rgba(196, 30, 58, 0.28);"),

    # Status. Critical moves to Ember: warm, brick shifted, a full hue step
    # off the brand so a Deploy button and a failing pod stop arguing.
    ("  --ok: #178a45;", "  --ok: #0f7a45;"),
    ("  --ok-soft: rgba(23, 138, 69, 0.1);", "  --ok-soft: rgba(15, 122, 69, 0.1);"),
    ("  --ok-border: rgba(23, 138, 69, 0.3);", "  --ok-border: rgba(15, 122, 69, 0.3);"),
    ("  --warn: #a66104;", "  --warn: #9a5c00;"),
    ("  --warn-soft: rgba(166, 97, 4, 0.1);", "  --warn-soft: rgba(199, 133, 10, 0.14);"),
    ("  --warn-border: rgba(166, 97, 4, 0.3);", "  --warn-border: rgba(176, 111, 0, 0.3);"),
    ("  --danger: #d71f1f;", "  --danger: #c62a1b;"),
    ("  --danger-soft: rgba(215, 31, 31, 0.09);", "  --danger-soft: rgba(224, 59, 46, 0.1);"),
    ("  --danger-border: rgba(215, 31, 31, 0.3);", "  --danger-border: rgba(224, 59, 46, 0.3);"),
    ("  --danger-hover: #b31212;", "  --danger-hover: #a52113;"),
    ("  --info: #0e7490;", "  --info: #0e6f91;"),
    ("  --info-soft: rgba(14, 116, 144, 0.1);", "  --info-soft: rgba(14, 111, 145, 0.1);"),
    ("  --info-border: rgba(14, 116, 144, 0.28);", "  --info-border: rgba(14, 111, 145, 0.28);"),

    ("  --ai: #7c3aed;", "  --ai: #5646c8;"),
    ("  --ai-soft: rgba(124, 58, 237, 0.1);", "  --ai-soft: rgba(86, 70, 200, 0.1);"),
    ("  --ai-border: rgba(124, 58, 237, 0.3);", "  --ai-border: rgba(86, 70, 200, 0.3);"),

    # Charts: Cardinal leads, then hues that cannot be read as a status.
    ("  --chart-1: #ff2929;", "  --chart-1: #c41e3a;"),
    ("  --chart-2: #2563eb;", "  --chart-2: #2b59c3;"),
    ("  --chart-3: #d97706;", "  --chart-3: #0e7c6b;"),
    ("  --chart-4: #0d9488;", "  --chart-4: #b06f00;"),
    ("  --chart-5: #7c3aed;", "  --chart-5: #6b4fd0;"),
    ("  --chart-6: #059669;", "  --chart-6: #0e6f91;"),
    ("  --chart-7: #4f46e5;", "  --chart-7: #8a4bbd;"),
    ("  --chart-8: #0891b2;", "  --chart-8: #3f7d9e;"),

    ("  --border: #e9e7e3;", "  --border: #e3e0e6;"),
    ("  --border-soft: #f0efeb;", "  --border-soft: #edebf1;"),
    ("  --border-strong: #d8d6d0;", "  --border-strong: #cecad7;"),
    ("  --shadow-sm: 0 1px 2px rgba(26, 27, 30, 0.05);",
     "  --shadow-sm: 0 1px 2px rgba(20, 18, 24, 0.05);"),
    ("  --ring: 0 0 0 3px rgba(255, 41, 41, 0.15);",
     "  --ring: 0 0 0 3px rgba(196, 30, 58, 0.22);"),
    ("  --backdrop: rgba(23, 21, 18, 0.42);", "  --backdrop: rgba(20, 18, 24, 0.44);"),

    # Shape. Radius is spent by role now, not stamped on everything.
    ("  --radius-sm: 0.5rem;", "  --radius-sm: 0.375rem;"),
    ("  --radius-md: 0.75rem;", "  --radius-md: 0.5rem;"),
    ("  --radius-lg: 1.125rem;", "  --radius-lg: 0.625rem;"),
    ("  --radius-xl: 1.375rem;", "  --radius-xl: 0.875rem;"),
    ("  --radius-full: 999px;",
     "  --radius-full: 999px;\n"
     "  /* Cardinal spends radius by role. Controls read as objects with\n"
     "     edges, status chips read as nearly square labels, and 999px is\n"
     "     reserved for the brand mark, status dots and ring gauges. */\n"
     "  --radius-control: 0.5rem;\n"
     "  --radius-chip: 0.25rem;"),

    # Type
    ("  --font-sans: 'Inter', 'Segoe UI', Roboto, Arial, sans-serif;",
     "  --font-sans: 'Instrument Sans', 'Segoe UI', Roboto, Arial, sans-serif;"),
    ("  --font-display: 'Plus Jakarta Sans', 'Inter', 'Segoe UI', Roboto, Arial, sans-serif;",
     "  --font-display: 'Archivo', 'Instrument Sans', 'Segoe UI', Roboto, Arial, sans-serif;"),
    ("  --font-mono: 'IBM Plex Mono', Consolas, Menlo, Monaco, monospace;",
     "  --font-mono: 'JetBrains Mono', Consolas, Menlo, Monaco, monospace;"),

    # Sidebar
    ("  --sidebar-border: #e9e7e3;", "  --sidebar-border: #e3e0e6;"),
    ("  --sidebar-nav-text: #55575e;", "  --sidebar-nav-text: #514d59;"),
    ("  --sidebar-nav-hover-bg: #f3f2ef;", "  --sidebar-nav-hover-bg: #f0eef3;"),
    ("  --sidebar-nav-hover-text: #1a1b1e;", "  --sidebar-nav-hover-text: #141218;"),
    ("  --sidebar-nav-active-bg: #fff1ef;", "  --sidebar-nav-active-bg: rgba(196, 30, 58, 0.08);"),
    ("  --sidebar-nav-active-text: #d71f1f;", "  --sidebar-nav-active-text: #b01834;"),
    ("  --sidebar-nav-active-border: #ff2929;", "  --sidebar-nav-active-border: #c41e3a;"),
    ("  --sidebar-section-label: #8b8d96;", "  --sidebar-section-label: #948e9e;"),
    ("  --sidebar-brand-text: #1a1b1e;", "  --sidebar-brand-text: #141218;"),
    ("  --sidebar-brand-sub: #8b8d96;", "  --sidebar-brand-sub: #948e9e;"),
    ("  --sidebar-footer-text: #8b8d96;", "  --sidebar-footer-text: #948e9e;"),
]

DARK = [
    ("  --bg-main: #101112;", "  --bg-main: #100f13;"),
    ("  --bg-panel: #17181a;", "  --bg-panel: #191720;"),
    ("  --bg-panel-strong: #1c1d20;", "  --bg-panel-strong: #201d28;"),
    ("  --bg-elevated: #222327;", "  --bg-elevated: #262230;"),
    ("  --bg-interactive: #2a2b30;", "  --bg-interactive: #2c2834;"),
    ("  --bg-inset: #0b0c0d;", "  --bg-inset: #131118;"),

    ("  --text-strong: #fafaf9;", "  --text-strong: #fbf9fd;"),
    ("  --text-main: #ececea;", "  --text-main: #f0edf4;"),
    ("  --text-subtle: #b9bab6;", "  --text-subtle: #c6c1cf;"),
    ("  --text-muted: #85868c;", "  --text-muted: #948ea0;"),

    ("  --accent: #ff3b3b;", "  --accent: #e8395a;"),
    ("  --accent-hover: #e62e2e;", "  --accent-hover: #cf2b4a;"),
    ("  --accent-strong: #ff7a70; /* red as TEXT on dark panels */",
     "  --accent-strong: #ff8296; /* red as TEXT on dark panels */"),
    ("  --accent-soft: rgba(255, 59, 59, 0.14);", "  --accent-soft: rgba(232, 57, 90, 0.16);"),
    ("  --accent-border: rgba(255, 59, 59, 0.4);", "  --accent-border: rgba(232, 57, 90, 0.44);"),

    ("  --ok: #2fbe6b;", "  --ok: #2ebb76;"),
    ("  --ok-soft: rgba(47, 190, 107, 0.12);", "  --ok-soft: rgba(46, 187, 118, 0.15);"),
    ("  --ok-border: rgba(47, 190, 107, 0.32);", "  --ok-border: rgba(46, 187, 118, 0.32);"),
    ("  --warn: #f0a32b;", "  --warn: #d99a24;"),
    ("  --warn-soft: rgba(240, 163, 43, 0.12);", "  --warn-soft: rgba(217, 154, 36, 0.16);"),
    ("  --warn-border: rgba(240, 163, 43, 0.32);", "  --warn-border: rgba(217, 154, 36, 0.32);"),
    ("  --danger: #ff4d4d;", "  --danger: #ff6a56;"),
    ("  --danger-soft: rgba(255, 77, 77, 0.12);", "  --danger-soft: rgba(232, 68, 47, 0.18);"),
    ("  --danger-border: rgba(255, 77, 77, 0.32);", "  --danger-border: rgba(232, 68, 47, 0.36);"),
    ("  --danger-hover: #e33c3c;", "  --danger-hover: #e8442f;"),
    ("  --info: #2cb8dc;", "  --info: #3fb6d9;"),
    ("  --info-soft: rgba(44, 184, 220, 0.12);", "  --info-soft: rgba(63, 182, 217, 0.14);"),
    ("  --info-border: rgba(44, 184, 220, 0.32);", "  --info-border: rgba(63, 182, 217, 0.32);"),

    ("  --ai: #a78bfa;", "  --ai: #9d8cff;"),
    ("  --ai-soft: rgba(167, 139, 250, 0.14);", "  --ai-soft: rgba(157, 140, 255, 0.16);"),
    ("  --ai-border: rgba(167, 139, 250, 0.36);", "  --ai-border: rgba(157, 140, 255, 0.36);"),

    ("  --chart-1: #ff5a52;", "  --chart-1: #f45a76;"),
    ("  --chart-2: #5b8df8;", "  --chart-2: #6f97ef;"),
    ("  --chart-3: #f0a32b;", "  --chart-3: #2ec5a8;"),
    ("  --chart-4: #2dd4bf;", "  --chart-4: #e0a53c;"),
    ("  --chart-5: #a78bfa;", "  --chart-5: #a48ff0;"),
    ("  --chart-6: #34d399;", "  --chart-6: #4fb8d8;"),
    ("  --chart-7: #818cf8;", "  --chart-7: #c08ae0;"),
    ("  --chart-8: #22d3ee;", "  --chart-8: #7fa8c4;"),

    ("  --border: rgba(235, 232, 225, 0.12);", "  --border: rgba(240, 236, 246, 0.13);"),
    ("  --border-soft: rgba(235, 232, 225, 0.07);", "  --border-soft: rgba(240, 236, 246, 0.07);"),
    ("  --border-strong: rgba(235, 232, 225, 0.22);", "  --border-strong: rgba(240, 236, 246, 0.24);"),
    ("  --ring: 0 0 0 3px rgba(255, 59, 59, 0.22);", "  --ring: 0 0 0 3px rgba(232, 57, 90, 0.3);"),

    ("  --sidebar-bg: #141416;", "  --sidebar-bg: #17151d;"),
    ("  --sidebar-border: rgba(235, 232, 225, 0.1);", "  --sidebar-border: rgba(240, 236, 246, 0.11);"),
    ("  --sidebar-nav-text: #9a9ba0;", "  --sidebar-nav-text: #a49eb0;"),
    ("  --sidebar-nav-hover-text: #ececea;", "  --sidebar-nav-hover-text: #f0edf4;"),
    ("  --sidebar-nav-active-bg: rgba(255, 59, 59, 0.14);",
     "  --sidebar-nav-active-bg: rgba(232, 57, 90, 0.16);"),
    ("  --sidebar-nav-active-text: #ff7a70;", "  --sidebar-nav-active-text: #ff8296;"),
    ("  --sidebar-nav-active-border: #ff3b3b;", "  --sidebar-nav-active-border: #e8395a;"),
    ("  --sidebar-section-label: #6c6d73;", "  --sidebar-section-label: #746e80;"),
    ("  --sidebar-brand-text: #fafaf9;", "  --sidebar-brand-text: #fbf9fd;"),
]


def main():
    src = io.open(PATH, encoding="utf-8").read()
    head, sep, tail = src.partition('[data-theme="dark"] {')
    if not sep:
        sys.exit("could not find the dark theme block")

    missed = []
    for old, new in LIGHT:
        if old not in head:
            missed.append(("light", old.strip()))
        else:
            head = head.replace(old, new, 1)
    for old, new in DARK:
        if old not in tail:
            missed.append(("dark", old.strip()))
        else:
            tail = tail.replace(old, new, 1)

    io.open(PATH, "w", encoding="utf-8", newline="\n").write(head + sep + tail)
    print("applied %d light, %d dark" % (len(LIGHT) - sum(1 for m in missed if m[0] == "light"),
                                         len(DARK) - sum(1 for m in missed if m[0] == "dark")))
    for where, tok in missed:
        print("  MISSED (%s) %s" % (where, tok))
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
