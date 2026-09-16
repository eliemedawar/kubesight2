"""
Spend radius by role.

Signal made buttons, cards, tags and avatars all round, so roundness stopped
signalling anything. Cardinal keeps 999px for the three things that are
genuinely circular (brand mark, status dot, ring gauge) and moves everything
else onto a role token.

  --radius-control  0.5rem   buttons, inputs, segmented groups, toolbars
  --radius-chip     0.25rem  status chips, tags, badges, counts that are labels

Anything not listed below keeps its pill: dots, avatars, steppers, toggles,
progress tracks, scrollbars, close buttons and numeric bubbles are all
legitimately round.

Run from frontend/:  python scripts/cardinal-shape.py
"""
import glob
import io
import sys

CONTROL = {
    "src/index.css": [
        "button",
        ".log-viewer__jump",
        ".btn-ghost.exec-pod-modal__run",
        ".template-card__form-btn",
        ".inventory-section-tabs",
        ".inventory-section-tabs button",
    ],
    "src/pages/ApplicationIntelligencePage.css": [".ai-segmented button"],
    "src/styles/signal/alerts.css": [
        ".alerts-page .al-tabs",
        ".alerts-page .al-tabs button",
        ".alerts-page .al-search input",
        ".alerts-page .al-seg",
        ".alerts-page .al-seg button",
        ".alerts-page .al-ghostlink",
    ],
    "src/styles/signal/catalog.css": [".sg-cat-search"],
    "src/styles/signal/ci.css": [".sg-ci-card-run"],
    "src/styles/signal/clusterBuilder.css": [
        ".sg-cb-seg",
        ".sg-cb-seg button",
        ".sg-cb-search",
        ".sg-cb-roleset",
        ".sg-cb-roleset button",
    ],
    "src/styles/signal/coachmarks.css": [".cm-btn"],
    "src/styles/signal/overview.css": [".ov-range", ".ov-range-pill"],
    "src/styles/signal/screens.css": [".sg-tbar"],
    "src/styles/signal/settings.css": [
        ".settings-segmented",
        ".settings-segmented button",
        ".settings-savebar",
    ],
    "src/styles/signal/ticketing.css": [
        ".sg-zh-subtabs",
        ".sg-zh-subtab",
        ".sg-zh-hookurl",
        ".sg-zh-savebar",
    ],
}

CHIP = {
    "src/index.css": [
        ".status-pill",
        ".pill",
        ".status-badge",
        ".routing-status-badge",
        ".recommended-badge",
        ".log-viewer__live-badge",
        ".chip",
        ".role-badge",
        ".template-card__badge",
        ".schema-source-chip",
        ".schema-pill",
        ".receiver-type-badge",
        ".key-multiselect__chip",
        ".wizard-template-badge",
        ".helm-chart-card__meta span",
        ".topo-edge-label",
    ],
    "src/styles/premium.css": [".hermes-badge"],
    "src/styles/signal/alerts.css": [
        ".alerts-page .al-scope-chip",
        ".alerts-page .al-type",
    ],
    "src/styles/signal/ci.css": [".sg-ci-stage-kind", ".sg-mx-pill"],
    "src/styles/signal/ciAssist.css": [".sg-ci-profile-badge", ".sg-ci-gen-label"],
    "src/styles/signal/clusterBuilder.css": [
        ".sg-cb-pill",
        ".sg-cb-chip",
        ".sg-cb-tierchip",
        ".sg-cb-srcchip",
        ".sg-cb-fchip",
        ".sg-cb-wl-tag",
        ".sg-cb-kindtag",
    ],
    "src/styles/signal/mobileApps.css": [
        ".sg-ma-plat",
        ".sg-ma-unsigned",
        ".sg-ma-cfg-flag",
    ],
    "src/styles/signal/screens.css": [".sg-tag", ".sg-dchip", ".sg-delta"],
    "src/styles/signal/settings.css": [".scope-chip"],
    "src/styles/signal/ticketing.css": [".sg-zh-fpill"],
    "src/styles/ui-polish.css": [".ops-pill"],
}


def selector_for(lines, i):
    for j in range(i - 1, max(-1, i - 60), -1):
        t = lines[j].strip()
        if t.endswith("{"):
            return t[:-1].strip()
    return ""


def apply(path, control, chip):
    raw = io.open(path, encoding="utf-8").read()
    lines = raw.split("\n")
    changed = 0
    unmatched = set(control) | set(chip)
    for i, line in enumerate(lines):
        if "radius-full" not in line or "border-radius" not in line:
            continue
        sel = selector_for(lines, i)
        if sel in control:
            token = "--radius-control"
        elif sel in chip:
            token = "--radius-chip"
        else:
            continue
        unmatched.discard(sel)
        lines[i] = line.replace("var(--radius-full)", "var(%s)" % token) \
                       .replace("var(--radius-full, 999px)", "var(%s, 8px)" % token)
        changed += 1
    if changed:
        io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
    return changed, sorted(unmatched)


def main():
    total, problems = 0, []
    for path in sorted(set(list(CONTROL) + list(CHIP))):
        n, miss = apply(path, CONTROL.get(path, []), CHIP.get(path, []))
        total += n
        print("%-46s %2d" % (path, n))
        for m in miss:
            problems.append("%s -> %s" % (path, m))
    print("total %d declarations retuned" % total)
    for p in problems:
        print("  NOT FOUND", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
