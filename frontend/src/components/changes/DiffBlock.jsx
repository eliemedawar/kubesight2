const PRE_STYLE = {
  marginTop: 8,
  maxHeight: 280,
  overflow: "auto",
  background: "var(--bg-inset)",
  color: "var(--text-main)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: 10,
  fontSize: "0.72rem",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

function lineStyle(line) {
  if (line.startsWith("+++") || line.startsWith("---")) return { fontWeight: 600 };
  if (line.startsWith("diff ")) return { color: "var(--text-muted)", fontWeight: 600 };
  if (line.startsWith("@@")) return { color: "var(--info)" };
  if (line.startsWith("+")) return { color: "var(--ok)", background: "var(--ok-soft)" };
  if (line.startsWith("-")) return { color: "var(--danger)", background: "var(--danger-soft)" };
  return null;
}

/** Unified-diff text (kubectl diff output) with +/- line coloring. */
export default function DiffBlock({ text, style }) {
  const lines = (text || "").replace(/\r\n/g, "\n").split("\n");
  return (
    <pre style={{ ...PRE_STYLE, ...style }}>
      {lines.map((line, i) => (
        <div key={i} style={lineStyle(line) || undefined}>
          {line || " "}
        </div>
      ))}
    </pre>
  );
}
