import { useCallback, useEffect, useState } from "react";
import { getZohoOptionSources } from "../../api/zohoApi.js";

/**
 * The option-source catalogue + every current binding.
 *
 * Both are needed together to pick a cascade parent: a parent field is only
 * valid if it is itself bound to the source kind the child's options are
 * grouped by (deployments group by namespace, env vars group by deployment).
 */
export default function useZohoOptionSources() {
  const [sources, setSources] = useState([]);
  const [bindings, setBindings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getZohoOptionSources();
      setSources(data?.sources || []);
      setBindings(data?.bindings || []);
    } catch (err) {
      setError(err.message || "Could not read the option sources.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /** Fields that may parent a child bound to `sourceKind`. */
  const parentsFor = useCallback(
    (sourceKind) => {
      const kind = sources.find((s) => s.key === sourceKind);
      if (!kind?.parentKind) return [];
      return bindings.filter((b) => b.sourceKind === kind.parentKind);
    },
    [sources, bindings]
  );

  return { sources, bindings, loading, error, reload: load, parentsFor };
}
