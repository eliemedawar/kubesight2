import { TIME_RANGE_OPTIONS } from "../../utils/logTimeRange.js";
import { LOG_LEVELS } from "../../utils/logFormat.js";
import SearchableSelect from "../common/SearchableSelect.jsx";

export default function LogFilters({
  filters,
  onChange,
  clusters,
  namespaces,
  pods,
  containers,
  podsLoading,
  containersLoading,
  liveEnabled,
  onLiveToggle,
  showTimestamps,
  onTimestampsToggle,
  onPreviousToggle,
  onClusterChange,
  onNamespaceChange,
  onPodChange,
}) {
  return (
    <section className="card log-filters">
      <div className="log-filters-primary">
        <label>
          Cluster
          <SearchableSelect
            value={filters.cluster}
            onChange={(event) => onClusterChange(event.target.value)}
            disabled={!clusters.length}
          >
            {clusters.map((cluster) => (
              <option key={cluster.id} value={cluster.id}>
                {cluster.name}
              </option>
            ))}
            {!clusters.length ? <option value="">No clusters connected</option> : null}
          </SearchableSelect>
        </label>
        <label>
          Namespace
          <SearchableSelect
            value={filters.namespace}
            onChange={(event) => onNamespaceChange(event.target.value)}
            disabled={!namespaces.length}
          >
            {namespaces.map((namespace) => (
              <option key={namespace.name} value={namespace.name}>
                {namespace.name}
              </option>
            ))}
            {!namespaces.length ? <option value="">No namespaces available</option> : null}
          </SearchableSelect>
        </label>
        <label>
          Pod
          <SearchableSelect
            value={filters.pod}
            onChange={(event) => onPodChange(event.target.value)}
            disabled={podsLoading || !pods.length}
          >
            {podsLoading ? <option value="">Loading pods…</option> : null}
            {!podsLoading
              ? pods.map((pod) => (
                  <option key={pod.name} value={pod.name}>
                    {pod.name}
                  </option>
                ))
              : null}
            {!podsLoading && !pods.length ? <option value="">No pods in namespace</option> : null}
          </SearchableSelect>
        </label>
        <label>
          Container
          <SearchableSelect
            value={filters.container}
            onChange={(event) => onChange({ container: event.target.value })}
            disabled={containersLoading || !containers.length || !filters.pod}
          >
            {containersLoading ? <option value="">Loading containers…</option> : null}
            {!containersLoading
              ? containers.map((container) => (
                  <option key={container.name} value={container.name}>
                    {container.name}
                  </option>
                ))
              : null}
            {!containersLoading && !containers.length ? (
              <option value="">Select a pod first</option>
            ) : null}
          </SearchableSelect>
        </label>
        <label>
          Time range
          <SearchableSelect
            value={filters.timeRange}
            onChange={(event) =>
              onChange({
                timeRange: event.target.value,
                customFrom: event.target.value === "custom" ? filters.customFrom : "",
                customTo: event.target.value === "custom" ? filters.customTo : "",
              })
            }
            disabled={liveEnabled}
          >
            {TIME_RANGE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </SearchableSelect>
        </label>
        <label>
          Search
          <input
            type="search"
            placeholder="Filter log text…"
            value={filters.searchText}
            onChange={(event) => onChange({ searchText: event.target.value })}
          />
        </label>
        <label>
          Log level
          <SearchableSelect
            value={filters.levelFilter}
            onChange={(event) => onChange({ levelFilter: event.target.value })}
          >
            <option value="all">All levels</option>
            {LOG_LEVELS.map((level) => (
              <option key={level} value={level}>
                {level}
              </option>
            ))}
            <option value="OTHER">Other / no level</option>
          </SearchableSelect>
        </label>
      </div>
      <div className="log-filters-toggles">
        <button
          type="button"
          className={liveEnabled ? "btn-primary btn-sm" : "btn-outline btn-sm"}
          onClick={onLiveToggle}
          aria-pressed={liveEnabled}
        >
          {liveEnabled ? "Live logs on" : "Live logs off"}
        </button>
        <label className="checkbox-label">
          <input type="checkbox" checked={showTimestamps} onChange={onTimestampsToggle} />
          Show timestamps
        </label>
        <label className="checkbox-label">
          <input type="checkbox" checked={filters.previous} onChange={onPreviousToggle} />
          Previous container logs
        </label>
      </div>
      {filters.timeRange === "custom" && !liveEnabled ? (
        <div className="log-filters-custom-range">
          <label>
            From
            <input
              type="datetime-local"
              value={filters.customFrom}
              onChange={(event) => onChange({ customFrom: event.target.value })}
            />
          </label>
          <label>
            To
            <input
              type="datetime-local"
              value={filters.customTo}
              onChange={(event) => onChange({ customTo: event.target.value })}
            />
          </label>
        </div>
      ) : null}
    </section>
  );
}
