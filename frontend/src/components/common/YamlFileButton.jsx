import { useRef } from "react";

// Same cap as the backend: a values.yaml is a person's file, not a chart.
export const MAX_YAML_FILE_BYTES = 1024 * 1024;

/** A button that reads one .yaml/.yml file and hands over its text. */
export default function YamlFileButton({
  children = "Import values.yaml",
  className = "btn-outline btn-sm",
  disabled = false,
  onLoad,
  onError,
}) {
  const input = useRef(null);

  const pick = async (event) => {
    const file = event.target.files?.[0];
    // Cleared so choosing the same file again still fires a change.
    event.target.value = "";
    if (!file) return;
    if (file.size > MAX_YAML_FILE_BYTES) {
      onError?.(`${file.name} is larger than 1 MiB.`);
      return;
    }
    try {
      onLoad(await file.text(), file.name);
    } catch {
      onError?.(`${file.name} could not be read.`);
    }
  };

  return (
    <>
      <button
        type="button"
        className={className}
        disabled={disabled}
        onClick={() => input.current?.click()}
      >
        {children}
      </button>
      <input
        ref={input}
        type="file"
        accept=".yaml,.yml,application/yaml,text/yaml"
        hidden
        onChange={pick}
      />
    </>
  );
}
