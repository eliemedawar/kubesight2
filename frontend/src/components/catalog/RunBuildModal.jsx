import { useEffect, useMemo, useRef, useState } from "react";
import { listCiBranches, runCiBuild } from "../../api/ciApi.js";
import { BranchIcon, PlayIcon, TagIcon } from "./ciShared.jsx";

/**
 * Jenkins-style "build with parameters", reduced to the one parameter that
 * matters: WHAT to build. Pick a branch or a release tag — fetched live from
 * the repository — or type one that is not listed yet.
 *
 * A tag build's container image is tagged with the git tag itself (v1.2.3 →
 * image :v1.2.3); branch builds keep <branch>-<number> because branch heads
 * move. The last choice is remembered per service, so a team that always
 * builds tags lands on the tag field with their previous tag prefilled.
 */

const remememberKey = (serviceId) => `ks.ci.runref.${serviceId}`;

function loadRemembered(serviceId) {
  try {
    const raw = window.localStorage.getItem(remememberKey(serviceId));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && (parsed.refType === "tag" || parsed.refType === "branch")) return parsed;
  } catch {
    /* localStorage unavailable or corrupted — fall back to defaults */
  }
  return null;
}

function remember(serviceId, refType, value) {
  try {
    window.localStorage.setItem(remememberKey(serviceId), JSON.stringify({ refType, value }));
  } catch {
    /* best effort */
  }
}

// Newest-looking versions first: numeric-aware descending, so v1.10.0
// sorts above v1.9.0 and above v1.2.3.
const byVersionDesc = (a, b) =>
  b.value.localeCompare(a.value, undefined, { numeric: true, sensitivity: "base" });

export default function RunBuildModal({ service, onClose, onStarted }) {
  const remembered = useMemo(() => loadRemembered(service.id), [service.id]);
  const [refType, setRefType] = useState(remembered?.refType || "branch");
  const [value, setValue] = useState(
    remembered?.value || (remembered?.refType === "tag" ? "" : service.defaultBranch || "main")
  );
  const [branches, setBranches] = useState([]);
  const [tags, setTags] = useState([]);
  const [loadingRefs, setLoadingRefs] = useState(true);
  const [refsError, setRefsError] = useState("");
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listCiBranches(service.id);
        if (cancelled) return;
        const items = data.items || [];
        setBranches(items.filter((item) => item.type === "branch"));
        setTags(items.filter((item) => item.type === "tag").sort(byVersionDesc));
        setRefsError("");
      } catch (err) {
        if (!cancelled) {
          // Not fatal: the build only needs a name, the list is a convenience.
          setRefsError(err.message || "Could not list refs from the repository.");
        }
      } finally {
        if (!cancelled) setLoadingRefs(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [service.id]);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [refType]);

  const switchType = (next) => {
    if (next === refType) return;
    setRefType(next);
    setError("");
    // Each mode gets its natural starting value rather than carrying the
    // other mode's text across.
    if (next === "branch") {
      setValue(remembered?.refType === "branch" ? remembered.value : service.defaultBranch || "main");
    } else {
      setValue(remembered?.refType === "tag" ? remembered.value : "");
    }
  };

  const pool = refType === "tag" ? tags : branches;
  const term = value.trim().toLowerCase();
  const matches = useMemo(() => {
    const filtered = term
      ? pool.filter((item) => item.value.toLowerCase().includes(term))
      : pool;
    return filtered.slice(0, 50);
  }, [pool, term]);
  const exact = pool.some((item) => item.value === value.trim());

  const submit = async () => {
    const ref = value.trim();
    if (!ref || starting) return;
    setStarting(true);
    setError("");
    try {
      const build = await runCiBuild(service.id, { branch: ref, refType });
      remember(service.id, refType, ref);
      onStarted(build);
    } catch (err) {
      setError(err.message || "Could not start the build.");
      setStarting(false);
    }
  };

  const hint =
    refType === "tag"
      ? value.trim()
        ? `Checks out tag ${value.trim()} — a container image (if the pipeline builds one) is pushed as :${value.trim()}.`
        : "Pick a release tag from the repository, or type one."
      : value.trim()
      ? `Builds the head of ${value.trim()} — images are tagged ${value.trim()}-<build number>.`
      : "Pick a branch, or type one.";

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-card sg-ci-run-modal"
        role="dialog"
        aria-label={`Run a build of ${service.name}`}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3>Run build — {service.name}</h3>
          <p className="muted">What should this build check out?</p>
        </div>

        {error && <p className="banner-message error">{error}</p>}

        <div className="sg-cat-tabs sg-ci-run-modes" role="group" aria-label="Ref kind">
          <button
            type="button"
            className={`sg-cat-tab${refType === "branch" ? " is-on" : ""}`}
            aria-pressed={refType === "branch"}
            onClick={() => switchType("branch")}
          >
            <BranchIcon /> Branch
          </button>
          <button
            type="button"
            className={`sg-cat-tab${refType === "tag" ? " is-on" : ""}`}
            aria-pressed={refType === "tag"}
            onClick={() => switchType("tag")}
          >
            <TagIcon /> Tag
          </button>
        </div>

        <label className="sg-ci-run-field">
          {refType === "tag" ? "Repository tag" : "Branch"}
          <input
            ref={inputRef}
            value={value}
            maxLength={255}
            placeholder={refType === "tag" ? "e.g. v1.72.1" : service.defaultBranch || "main"}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                submit();
              }
            }}
          />
          <span className="field-hint">{hint}</span>
        </label>

        <div className="sg-ci-run-options" role="listbox" aria-label="Matching refs">
          {loadingRefs ? (
            <p className="muted sg-ci-run-note">Reading refs from the repository…</p>
          ) : refsError ? (
            <p className="muted sg-ci-run-note">
              {refsError} You can still type the exact name and run.
            </p>
          ) : matches.length === 0 ? (
            <p className="muted sg-ci-run-note">
              {pool.length === 0
                ? refType === "tag"
                  ? "The repository has no tags yet — type one anyway, or build a branch."
                  : "No branches visible."
                : "Nothing matches — the build will use exactly what you typed."}
            </p>
          ) : (
            matches.map((item) => (
              <button
                key={item.value}
                type="button"
                role="option"
                aria-selected={item.value === value.trim()}
                className={`sg-ci-run-option${item.value === value.trim() ? " is-on" : ""}`}
                onClick={() => setValue(item.value)}
                onDoubleClick={() => {
                  setValue(item.value);
                  submit();
                }}
              >
                {refType === "tag" ? <TagIcon /> : <BranchIcon />}
                <span className="sg-ci-run-option-name">{item.value}</span>
                {item.value === service.defaultBranch && refType === "branch" && (
                  <span className="chip">default</span>
                )}
                {item.commit && <code className="muted">{item.commit.slice(0, 8)}</code>}
              </button>
            ))
          )}
        </div>

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose} disabled={starting}>
            Cancel
          </button>
          <button
            type="button"
            className="primary sg-cat-new"
            onClick={submit}
            disabled={starting || !value.trim()}
            title={
              !exact && value.trim() && !loadingRefs && !refsError
                ? `"${value.trim()}" is not in the list — it will be used as typed`
                : undefined
            }
          >
            <PlayIcon />
            {starting ? "Starting…" : "Run build"}
          </button>
        </div>
      </div>
    </div>
  );
}
