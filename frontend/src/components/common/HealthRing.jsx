// Signal pod-health ring — donut of ok/warn/danger segments with the total in
// the center. Colors come from CSS tokens via currentColor-free explicit vars
// (SVG in DOM can read var()). Segments with zero values are skipped; the ring
// falls back to a muted track when the total is zero.
export default function HealthRing({
  ok = 0,
  warn = 0,
  danger = 0,
  size = 76,
  label = "pods",
  ariaLabel,
}) {
  const total = ok + warn + danger;
  const stroke = 9;
  const c = size / 2;
  const radius = c - stroke / 2 - 3;
  const circumference = 2 * Math.PI * radius;
  const segments = [
    { value: ok, color: "var(--ok)" },
    { value: warn, color: "var(--warn)" },
    { value: danger, color: "var(--danger)" },
  ];

  let offset = 0;
  const arcs = [];
  if (total > 0) {
    segments.forEach((segment, index) => {
      if (!segment.value) return;
      const length = (segment.value / total) * circumference;
      arcs.push(
        <circle
          key={index}
          cx={c}
          cy={c}
          r={radius}
          fill="none"
          stroke={segment.color}
          strokeWidth={stroke}
          strokeDasharray={`${Math.max(length - 2.5, 1.5)} ${circumference}`}
          strokeDashoffset={-offset}
          strokeLinecap="round"
          transform={`rotate(-90 ${c} ${c})`}
        />,
      );
      offset += length;
    });
  }

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={ariaLabel || `${total} ${label}: ${ok} healthy, ${warn} degraded, ${danger} failing`}
      style={{ flex: "none" }}
    >
      <circle cx={c} cy={c} r={radius} fill="none" stroke="var(--bg-interactive)" strokeWidth={stroke} />
      {arcs}
      <text
        x={c}
        y={c - 1}
        textAnchor="middle"
        fontSize={size * 0.2}
        fontWeight="700"
        fill="var(--text-strong)"
        style={{ fontVariantNumeric: "tabular-nums" }}
      >
        {total}
      </text>
      <text
        x={c}
        y={c + size * 0.17}
        textAnchor="middle"
        fontSize={size * 0.105}
        letterSpacing="1"
        fill="var(--text-muted)"
      >
        {label.toUpperCase()}
      </text>
    </svg>
  );
}
