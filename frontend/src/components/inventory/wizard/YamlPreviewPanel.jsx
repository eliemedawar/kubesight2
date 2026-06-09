import { useState } from "react";

export default function YamlPreviewPanel({
  yaml = "",
  readOnly = true,
  onChange,
  onCopy,
  onDownload,
  previousYaml = "",
  showCompare = false,
}) {
  const [compareMode, setCompareMode] = useState(false);
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(yaml);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
      onCopy?.();
    } catch {
      /* ignore */
    }
  };

  const handleDownload = () => {
    const blob = new Blob([yaml], { type: "text/yaml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "manifest.yaml";
    a.click();
    URL.revokeObjectURL(url);
    onDownload?.();
  };

  return (
    <div className="wizard-yaml-panel">
      <div className="wizard-yaml-panel__toolbar">
        <span className="muted">Generated Manifest</span>
        <div className="wizard-yaml-panel__actions">
          {showCompare && previousYaml ? (
            <button type="button" className="btn-outline btn-sm" onClick={() => setCompareMode((v) => !v)}>
              {compareMode ? "Hide diff" : "Compare versions"}
            </button>
          ) : null}
          <button type="button" className="btn-outline btn-sm" onClick={handleCopy}>
            {copied ? "Copied!" : "Copy YAML"}
          </button>
          <button type="button" className="btn-outline btn-sm" onClick={handleDownload}>
            Download
          </button>
        </div>
      </div>
      {compareMode && previousYaml ? (
        <pre className="wizard-yaml-panel__diff">{buildSimpleDiff(previousYaml, yaml)}</pre>
      ) : (
        <textarea
          className="wizard-yaml-panel__editor"
          value={yaml}
          readOnly={readOnly}
          onChange={(e) => onChange?.(e.target.value)}
          spellCheck={false}
          rows={18}
        />
      )}
    </div>
  );
}

function buildSimpleDiff(oldText, newText) {
  const oldLines = oldText.split("\n");
  const newLines = newText.split("\n");
  const max = Math.max(oldLines.length, newLines.length);
  const lines = [];
  for (let i = 0; i < max; i++) {
    const a = oldLines[i];
    const b = newLines[i];
    if (a === b) lines.push(`  ${b ?? ""}`);
    else if (a == null) lines.push(`+ ${b}`);
    else if (b == null) lines.push(`- ${a}`);
    else {
      lines.push(`- ${a}`);
      lines.push(`+ ${b}`);
    }
  }
  return lines.join("\n");
}
