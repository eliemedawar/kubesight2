import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "../../styles/signal/pipelineWorkspace.css";
import BuildParameters from "./BuildParameters.jsx";
import JenkinsfileImportModal from "./JenkinsfileImportModal.jsx";
import {
  applyCiPipelineTemplate,
  createCiPipeline,
  lintCiPipeline,
  listCiPipelines,
  listCiSecrets,
  updateCiPipeline,
} from "../../api/ciApi.js";
import LoadingState from "../common/LoadingState.jsx";
import { useRouter } from "../../routes/RouterContext.jsx";
import { applicationTypeLabel, formatRelative } from "./ciShared.jsx";
import { PlIcon } from "./pipeline/icons.jsx";
import StageFlow from "./pipeline/StageFlow.jsx";
import StagePicker from "./pipeline/StagePicker.jsx";
import StageSheet from "./pipeline/StageSheet.jsx";
import PostActionsBlock from "./pipeline/PostActionsBlock.jsx";
import PostActionSheet, { PostActionPicker } from "./pipeline/PostActionSheet.jsx";
import {
  blankPostAction,
  changePostType,
  MAX_POST_ACTIONS,
  moveAction,
  postActionChange,
  postActionProblems,
  postActionsChanged,
  postActionsForApi,
  postActionTitle,
  withPostKey,
} from "./pipeline/postActionModel.js";
import {
  blankStage,
  changeKindPatch,
  defaultStageName,
  describeDiff,
  fieldsLostOnKindChange,
  forApi,
  kindOf,
  lintByStage,
  MAX_STAGES,
  moveItem,
  parameterProblems,
  pipelineDiff,
  stageProblems,
  stagesForLint,
  uniqueStageName,
  withKey,
} from "./pipeline/stageModel.js";
import {
  joinPrevious,
  leaveGroup,
  normalizeGroups,
  parallelGroups,
  regroupAround,
  renameGroup,
  setGroupFailFast,
} from "./pipeline/parallelModel.js";

const clone = (value) => (value == null ? value : structuredClone(value));

/**
 * Pipeline tab: the stages a build runs, and the inputs it asks for.
 *
 * Left, the pipeline as a flow in run order; right, the one stage being
 * looked at. Everything is a local draft until Save — the whole pipeline
 * saves in one request, which is what makes reordering a local array move
 * rather than a sequence of API calls that can half-apply. "Unsaved" is
 * derived from the saved copy, so the save bar can say what changed and the
 * flow can mark which stages.
 */
export default function PipelineEditor({ service, onChanged, canEdit, onDirtyChange, onGoToTab }) {
  const { route, getRoute, navigate } = useRouter();
  const [pipeline, setPipeline] = useState(null);
  // The pipeline as last loaded or saved — the baseline every change is
  // measured against, and what Discard restores.
  const [saved, setSaved] = useState(null);
  const [stages, setStages] = useState([]);
  const [parameters, setParameters] = useState([]);
  // What happens when a build ends (notifications + cleanup), saved with the
  // pipeline in the same request. Selected in the flow's "When the build
  // ends" block; mode "post" shows one, "post-add" the kind picker.
  const [postActions, setPostActions] = useState([]);
  const [selectedPost, setSelectedPost] = useState(null);
  const [secretKeys, setSecretKeys] = useState([]);
  // The Scan stage's tool cards (image, configured, default rules) - sent with
  // the pipeline listing so they describe THIS installation and service.
  const [scanTools, setScanTools] = useState([]);
  const [selectedIndex, setSelectedIndex] = useState(null);
  // "stage" shows the selected stage; "add" shows the kind picker for a stage
  // about to be inserted at insertAt.
  const [mode, setMode] = useState("stage");
  const [insertAt, setInsertAt] = useState(0);
  const [focusToken, setFocusToken] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  // A change that is real but not visible as a difference in the stages:
  // taking ownership of the generated pipeline, or an imported draft.
  const [forcedDirty, setForcedDirty] = useState(false);
  const [removed, setRemoved] = useState(null);
  const [menuOpen, setMenuOpen] = useState(false);
  // What these stages assume about the runner they land on. Checked as they are
  // edited, because the failure it catches — an absolute /workspace path on an
  // agent — is only visible when a build has already burned.
  const [lint, setLint] = useState(null);
  const [importing, setImporting] = useState(false);
  // What the Jenkinsfile could not carry over, kept after the dialog closes:
  // the list is the to-do for finishing the port, and it is only actionable
  // next to the stages it is about.
  const [importNotes, setImportNotes] = useState(null);
  const menuRef = useRef(null);
  const rootRef = useRef(null);

  const view = route.query?.view === "inputs" ? "inputs" : "stages";
  const setQuery = useCallback(
    (patch) => {
      const active = getRoute();
      const query = { ...active.query };
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === undefined || value === "") delete query[key];
        else query[key] = String(value);
      }
      navigate({ key: active.key, params: active.params, query }, { replace: true });
    },
    [getRoute, navigate]
  );
  const setView = (next) => setQuery({ view: next === "inputs" ? "inputs" : null });

  const generated = Boolean(pipeline?.isGeneratedDefault);
  const editable = canEdit && !generated && !saving;

  const diff = useMemo(() => pipelineDiff(stages, parameters, saved), [stages, parameters, saved]);
  const postDirty = useMemo(
    () => postActionsChanged(saved?.postActions || [], postActions),
    [saved, postActions]
  );
  const dirty = forcedDirty || diff.count > 0 || postDirty;
  const postProblems = useMemo(
    () => postActions.map((action, index) => postActionProblems(action, index, postActions, secretKeys)),
    [postActions, secretKeys]
  );
  const postChanges = useMemo(
    () => postActions.map((action) => postActionChange(action, saved?.postActions || [])),
    [postActions, saved]
  );
  const blockingPost = postProblems.findIndex((list) => list.length > 0);

  // Every problem a stage has, from three places, in the order they matter:
  // what Save would refuse, what would fail on a runner, what the import left.
  const lintMap = useMemo(() => lintByStage(lint), [lint]);
  const problems = useMemo(
    () =>
      stages.map((stage, index) => [
        ...stageProblems(stage, index, stages, parameters).map((item) => ({
          ...item,
          source: "save",
          level: item.level || "error",
        })),
        ...(lintMap.get(index) || []).map((finding) => ({
          source: "lint",
          level: finding.level,
          message: finding.message,
          fix: finding.fix,
          breaksOn: finding.breaksOn,
        })),
        ...(importNotes?.notes || [])
          .filter((note) => note.stage && note.stage === stage.name && note.level !== "info")
          .map((note) => ({ source: "import", level: "warning", message: note.message })),
      ]),
    [stages, parameters, lintMap, importNotes]
  );
  const blockingStages = problems
    .map((list, index) => ({ index, count: list.filter((item) => item.source === "save" && item.level === "error").length }))
    .filter((item) => item.count > 0);
  const warningStages = problems
    .map((list, index) => ({ index, count: list.filter((item) => item.level !== "error" || item.source === "lint").length }))
    .filter((item) => item.count > 0 && !blockingStages.some((blocked) => blocked.index === item.index));
  const inputProblems = parameters.filter((param, index) => parameterProblems(param, index, parameters).length).length;

  // --- Loading ---------------------------------------------------------------

  const adopt = useCallback((data, { keepSelection = false } = {}) => {
    const nextStages = (data?.stages || []).map(withKey);
    const nextPost = (data?.postActions || []).map(withPostKey);
    setPipeline(data);
    setSaved(clone({ stages: nextStages, parameters: data?.parameters || [], postActions: nextPost }));
    setStages(nextStages);
    setParameters((data?.parameters || []).map((item) => ({ ...item })));
    setPostActions(nextPost.map((item) => ({ ...item })));
    setSelectedPost((current) =>
      keepSelection && current !== null && current < nextPost.length ? current : null
    );
    setForcedDirty(false);
    setRemoved(null);
    setSelectedIndex((current) => {
      if (!nextStages.length) return null;
      if (keepSelection && current !== null) return Math.min(current, nextStages.length - 1);
      const fromUrl = Number(getRoute().query?.stage) - 1;
      return Number.isInteger(fromUrl) && fromUrl >= 0 && fromUrl < nextStages.length ? fromUrl : 0;
    });
    // Saving while a post action is open keeps it open.
    setMode((currentMode) =>
      keepSelection && currentMode === "post" && nextPost.length ? "post" : nextStages.length ? "stage" : "add"
    );
    setInsertAt(0);
  }, [getRoute]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listCiPipelines(service.id);
      setScanTools(data.scanTools || []);
      const first =
        data.items?.find((item) => item.isDefault) ||
        data.items?.[0] || { name: "default", id: null, stages: [], parameters: [] };
      adopt(first);
      setError("");
    } catch (err) {
      setError(err.message || "Could not load the pipeline.");
    } finally {
      setLoading(false);
    }
  }, [service.id, adopt]);

  useEffect(() => {
    // A different service is a different draft: nothing said about the last
    // one carries over.
    setNotice("");
    setError("");
    setImportNotes(null);
    load();
    listCiSecrets(service.id)
      // The list is this service's secrets plus every global one, so a service
      // secret shadowing a global of the same name arrives twice. One row per
      // name, and it is the service's own that the build will resolve.
      .then((data) => {
        const byKey = new Map();
        for (const item of data.items || []) {
          if (!byKey.has(item.key) || item.scope === "service") {
            byKey.set(item.key, item.scope);
          }
        }
        setSecretKeys([...byKey].map(([key, scope]) => ({ key, scope })));
      })
      .catch(() => setSecretKeys([]));
  }, [service.id, load]);

  // --- Page-level wiring -----------------------------------------------------

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  // The flow and the preview are sticky inside the page's scroll area, so
  // their height has to come from that area, not the window: on a laptop the
  // top bar and page padding eat a quarter of 100vh.
  useEffect(() => {
    const root = rootRef.current;
    if (!root || typeof ResizeObserver === "undefined") return undefined;
    // .page-content is the app's scroller.
    const scroller = root.closest(".page-content");
    if (!scroller) return undefined;
    const apply = () => root.style.setProperty("--pl-view-h", `${scroller.clientHeight}px`);
    apply();
    const observer = new ResizeObserver(apply);
    observer.observe(scroller);
    return () => observer.disconnect();
  }, [loading]);

  // Leaving the tab clears its own query keys, so ?stage=5 does not ride
  // along to the Builds tab.
  useEffect(
    () => () => {
      const active = getRoute();
      if (active.key === "serviceDetail" && (active.query?.stage || active.query?.view)) {
        const { stage: _stage, view: _view, ...rest } = active.query;
        navigate({ key: active.key, params: active.params, query: rest }, { replace: true });
      }
    },
    [getRoute, navigate]
  );

  useEffect(() => {
    if (!dirty) return undefined;
    const warn = (event) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (!removed) return undefined;
    const timer = window.setTimeout(() => setRemoved(null), 10000);
    return () => window.clearTimeout(timer);
  }, [removed]);

  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(""), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const close = (event) => {
      if (event.type === "keydown" && event.key !== "Escape") return;
      if (event.type === "mousedown" && menuRef.current?.contains(event.target)) return;
      setMenuOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [menuOpen]);

  // Debounced: this runs while somebody types a command, and the answer is
  // only interesting once they stop. A failed check is silent — a linter that
  // shouts about its own outage is worse than no linter.
  useEffect(() => {
    if (!stages.length) {
      setLint(null);
      return undefined;
    }
    let current = true;
    const timer = window.setTimeout(() => {
      lintCiPipeline(stagesForLint(stages.map(forApi)))
        .then((result) => current && setLint(result))
        .catch(() => current && setLint(null));
    }, 600);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [stages]);

  // --- Selection -------------------------------------------------------------

  const select = (index) => {
    setSelectedIndex(index);
    setMode("stage");
    if (view !== "stages" || String(index + 1) !== getRoute().query?.stage) {
      setQuery({ stage: index + 1, view: null });
    }
  };

  const openInserter = (at) => {
    setInsertAt(at);
    setMode("add");
    if (view !== "stages") setView("stages");
  };

  // --- Edits -----------------------------------------------------------------

  const mutate = (index, patch) =>
    setStages((prev) => prev.map((stage, position) => (position === index ? { ...stage, ...patch } : stage)));

  const changeKind = (index, stageType) => {
    const stage = stages[index];
    if (stage.stageType === stageType) return;
    const lost = fieldsLostOnKindChange(stage, stageType);
    if (
      lost.length &&
      !window.confirm(
        `Making this a “${kindOf(stageType)?.label}” stage clears its ${lost.join(", ")} — ` +
          "that kind has nowhere to use them. Continue?"
      )
    ) {
      return;
    }
    const patch = changeKindPatch(stageType);
    // A name that was only ever the default follows the kind.
    if (!stage.name || stage.name === uniqueStageName(defaultStageName(stage.stageType), stages, index)) {
      patch.name = uniqueStageName(defaultStageName(stageType), stages, index);
    }
    mutate(index, patch);
  };

  const insert = (stage, at) => {
    if (stages.length >= MAX_STAGES) {
      setError(`A pipeline can hold at most ${MAX_STAGES} stages.`);
      return;
    }
    // Landing between two members of a parallel group joins it (see
    // parallelModel.regroupAround) — a stage dropped into a group runs with it.
    setStages((prev) => regroupAround([...prev.slice(0, at), stage, ...prev.slice(at)], at));
    setSelectedIndex(at);
    setMode("stage");
    setQuery({ stage: at + 1, view: null });
    setFocusToken((value) => value + 1);
  };

  const addKind = (stageType) =>
    insert(
      withKey({ ...blankStage(stageType), name: uniqueStageName(defaultStageName(stageType), stages) }),
      insertAt
    );

  const copyOf = (index, at) => {
    const copy = withKey(clone(stages[index]));
    delete copy.id;
    delete copy.position;
    copy.name = uniqueStageName(`${stages[index].name || "Stage"} copy`, stages);
    insert(copy, at);
  };

  const move = (from, to) => {
    if (to < 0 || to >= stages.length || from === to) return;
    const selectedStage = selectedIndex !== null ? stages[selectedIndex] : null;
    const moved = moveItem(stages, from, to);
    // Groups follow the move: carried out of its group a stage runs on its
    // own, dropped into another it joins it. Positions do not change.
    const next = regroupAround(moved, to);
    setStages(next);
    if (selectedStage) {
      const position = moved.indexOf(selectedStage);
      setSelectedIndex(position);
      setQuery({ stage: position + 1 });
    }
  };

  // "Run in parallel with the stage before", the group's name, its fail-fast.
  const changeGroup = (index, action, value) =>
    setStages((prev) => {
      if (action === "join") return joinPrevious(prev, index);
      if (action === "leave") return leaveGroup(prev, index);
      if (action === "rename") return renameGroup(prev, index, value);
      if (action === "failFast") return setGroupFailFast(prev, index, value);
      return prev;
    });

  const remove = (index) => {
    const stage = stages[index];
    setRemoved({ stage, index });
    // A group left with one stage is just that stage again.
    const next = normalizeGroups(stages.filter((_, position) => position !== index));
    setStages(next);
    if (!next.length) {
      setSelectedIndex(null);
      openInserter(0);
      setQuery({ stage: null });
    } else {
      const position = Math.min(index, next.length - 1);
      setSelectedIndex(position);
      setQuery({ stage: position + 1 });
    }
  };

  const undoRemove = () => {
    if (!removed) return;
    const at = Math.min(removed.index, stages.length);
    setStages((prev) => regroupAround([...prev.slice(0, at), removed.stage, ...prev.slice(at)], at));
    setSelectedIndex(at);
    setMode("stage");
    setQuery({ stage: at + 1 });
    setRemoved(null);
  };

  /** A renamed input takes the run conditions that named it along. */
  const changeParameters = (next) => {
    const renames = new Map();
    if (next.length === parameters.length) {
      next.forEach((param, index) => {
        const before = parameters[index]?.name;
        if (before && param.name !== before && !next.some((other) => other.name === before)) {
          renames.set(before, param.name);
        }
      });
    }
    setParameters(next);
    if (renames.size) {
      setStages((prev) =>
        prev.map((stage) =>
          stage.runCondition?.variable && renames.has(stage.runCondition.variable)
            ? { ...stage, runCondition: { ...stage.runCondition, variable: renames.get(stage.runCondition.variable) } }
            : stage
        )
      );
    }
  };

  // --- Post actions (when the build ends) -----------------------------------

  const selectPost = (index) => {
    setSelectedPost(index);
    setMode("post");
    if (view !== "stages") setView("stages");
  };

  const openPostPicker = () => {
    if (postActions.length >= MAX_POST_ACTIONS) return;
    setMode("post-add");
    if (view !== "stages") setView("stages");
  };

  const addPost = (type) => {
    if (postActions.length >= MAX_POST_ACTIONS) {
      setError(`A pipeline can have at most ${MAX_POST_ACTIONS} post actions.`);
      return;
    }
    const at = postActions.length;
    setPostActions((prev) => [...prev, withPostKey(blankPostAction(type))]);
    setSelectedPost(at);
    setMode("post");
  };

  const mutatePost = (index, patch) =>
    setPostActions((prev) => prev.map((action, position) => (position === index ? { ...action, ...patch } : action)));

  const changePostKind = (index, type) => {
    const action = postActions[index];
    if (!action || action.type === type) return;
    const hasContent =
      (action.recipients || []).length ||
      action.urlSecret ||
      (action.commands || []).some((line) => String(line).trim()) ||
      action.subject ||
      action.message;
    if (hasContent && !window.confirm("Changing what this post action does clears its settings. Continue?")) return;
    setPostActions((prev) => prev.map((item, position) => (position === index ? changePostType(item, type) : item)));
  };

  const movePost = (from, to) => {
    if (to < 0 || to >= postActions.length) return;
    setPostActions((prev) => moveAction(prev, from, to));
    setSelectedPost(to);
  };

  const removePost = (index) => {
    const action = postActions[index];
    const next = postActions.filter((_, position) => position !== index);
    setPostActions(next);
    if (next.length) {
      setSelectedPost(Math.min(index, next.length - 1));
    } else {
      setSelectedPost(null);
      setMode(stages.length ? "stage" : "add");
    }
    setNotice(`Removed “${postActionTitle(action)}” — Discard brings it back until you save.`);
  };

  // --- Save / discard / template / import ------------------------------------

  const save = async () => {
    if (!pipeline || !dirty || saving || generated) return;
    if (blockingStages.length) {
      setView("stages");
      select(blockingStages[0].index);
      setError(
        `${blockingStages.length === 1 ? "One stage needs" : `${blockingStages.length} stages need`} fixing before this can be saved — marked in the flow.`
      );
      return;
    }
    if (inputProblems) {
      setView("inputs");
      setError(`${inputProblems === 1 ? "One build input needs" : `${inputProblems} build inputs need`} fixing before this can be saved.`);
      return;
    }
    if (blockingPost >= 0) {
      selectPost(blockingPost);
      setError(`The post action “${postActionTitle(postActions[blockingPost])}” needs fixing before this can be saved.`);
      return;
    }
    setSaving(true);
    setError("");
    try {
      const payload = {
        name: pipeline.name || "default",
        isDefault: true,
        parameters,
        stages: stages.map((stage) => ({
          ...forApi(stage),
          timeoutSeconds: Number(stage.timeoutSeconds) || 1800,
        })),
        postActions: postActionsForApi(postActions),
      };
      const result = pipeline.id
        ? await updateCiPipeline(pipeline.id, payload)
        : await createCiPipeline(service.id, payload);
      adopt({ ...result, isGeneratedDefault: false }, { keepSelection: true });
      setImportNotes((current) => (current?.blocking?.length ? current : null));
      setNotice(`Saved as version ${result.version}. The next build runs these stages.`);
      onChanged?.();
    } catch (err) {
      setError(err.message || "Could not save the pipeline.");
    } finally {
      setSaving(false);
    }
  };

  // Ctrl/Cmd+S saves from anywhere on the tab — the save bar can be scrolled
  // out of mind while someone is deep in a command.
  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const onKey = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        saveRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const discard = () => {
    if (!saved) return;
    if (diff.count > 2 && !window.confirm("Throw away every unsaved change to this pipeline?")) return;
    if (forcedDirty) {
      // Taking ownership of the starter pipeline, or an imported draft, is
      // undone by reading the saved pipeline back rather than patching state.
      setImportNotes(null);
      setError("");
      load().then(() => setNotice("Changes discarded — this is the saved pipeline again."));
      return;
    }
    const restored = clone(saved);
    setStages(restored.stages.map((stage) => ({ ...stage })));
    setParameters(restored.parameters.map((item) => ({ ...item })));
    setPostActions((restored.postActions || []).map((item) => ({ ...item })));
    setSelectedPost(null);
    setRemoved(null);
    setImportNotes(null);
    setError("");
    setSelectedIndex(restored.stages.length ? Math.min(selectedIndex ?? 0, restored.stages.length - 1) : null);
    setMode(restored.stages.length ? "stage" : "add");
    setNotice("Changes discarded — this is the saved pipeline again.");
  };

  const customizeDefault = () => {
    setPipeline((current) => ({ ...current, isGeneratedDefault: false }));
    setForcedDirty(true);
    if (!stages.length) openInserter(0);
    setNotice("The starter stages are now yours to edit. Save to make this the service's pipeline.");
  };

  const resetToTemplate = async () => {
    setMenuOpen(false);
    if (
      !window.confirm(
        `Replace every stage and build input with the ${applicationTypeLabel(service.applicationType)} starter pipeline? This saves immediately.`
      )
    ) {
      return;
    }
    setSaving(true);
    setError("");
    try {
      const result = await applyCiPipelineTemplate(service.id, service.applicationType);
      adopt({ ...result, isGeneratedDefault: false });
      setImportNotes(null);
      setNotice("Starter pipeline applied and saved.");
      onChanged?.();
    } catch (err) {
      setError(err.message || "Could not apply the template.");
    } finally {
      setSaving(false);
    }
  };

  /**
   * Take a draft from the Jenkinsfile importer.
   *
   * Local state only, and deliberately: the draft becomes unsaved changes that
   * are reviewed and saved with the same button as any other edit. A working
   * pipeline is never replaced by a translation nobody read.
   */
  const applyDraft = (draft) => {
    setPipeline((current) => ({
      ...(current || { name: "default", id: null }),
      isGeneratedDefault: false,
    }));
    setStages(draft.stages.map(withKey));
    setParameters(draft.parameters.map((item) => ({ ...item })));
    setImportNotes(draft.notes?.length || draft.blocking?.length ? draft : null);
    setForcedDirty(true);
    setSelectedIndex(draft.stages.length ? 0 : null);
    setMode(draft.stages.length ? "stage" : "add");
    setQuery({ stage: draft.stages.length ? 1 : null, view: null });
    setImporting(false);
    setNotice("Jenkinsfile imported as a draft. Review the stages, then save.");
  };

  if (loading) return <LoadingState label="Loading pipeline…" />;

  const current = selectedIndex !== null ? stages[selectedIndex] : null;
  const offCount = stages.filter((stage) => stage.enabled === false).length;
  const runsTogether = parallelGroups(stages);
  const groupCount = runsTogether.length;
  const groupedCount = runsTogether.reduce((sum, run) => sum + run.indices.length, 0);
  const conditionalCount = stages.filter((stage) => stage.runCondition?.variable).length;
  const imageStages = stages.filter((stage) => stage.stageType === "container_image").length;

  return (
    <div className={`pl-root${dirty ? " is-dirty" : ""}`} ref={rootRef}>
      {importing && (
        <JenkinsfileImportModal
          service={service}
          onApply={applyDraft}
          onClose={() => setImporting(false)}
        />
      )}

      {/* ── Identity + what this pipeline does, in a sentence ─────────── */}
      <header className="pl-top">
        <div className="pl-top-id">
          <span className="pl-top-glyph" aria-hidden="true">
            <PlIcon name="stages" />
          </span>
          <div>
            <h3>
              Build pipeline
              {generated && <span className="pl-tag is-accent">Starter · managed by KubeSight</span>}
              {!generated && pipeline?.id == null && stages.length > 0 && <span className="pl-tag">Not saved yet</span>}
              {!canEdit && (
                <span className="pl-tag">
                  <PlIcon name="lock" /> View only
                </span>
              )}
            </h3>
            <p className="pl-top-sentence">
              {stages.length ? (
                <>
                  <b>{stages.length}</b> {stages.length === 1 ? "stage runs" : "stages run"} in order
                  {groupCount > 0 && (
                    <>
                      , <b>{groupedCount}</b> of them side by side in{" "}
                      {groupCount === 1 ? "a parallel group" : <><b>{groupCount}</b> parallel groups</>}
                    </>
                  )}
                  {offCount > 0 && <>, <b>{offCount}</b> turned off</>}
                  {conditionalCount > 0 && <>, <b>{conditionalCount}</b> only for some builds</>}
                  {imageStages > 0 && <> · pushes {imageStages === 1 ? "an image" : `${imageStages} images`}</>}
                  {parameters.length > 0 && (
                    <> · asks <b>{parameters.length}</b> {parameters.length === 1 ? "question" : "questions"} before it starts</>
                  )}
                  {postActions.length > 0 && (
                    <> · <b>{postActions.length}</b> {postActions.length === 1 ? "post action" : "post actions"} when it ends</>
                  )}
                </>
              ) : (
                "No stages yet — a build has nothing to run."
              )}
              {pipeline?.id && !generated && (
                <span className="pl-top-meta">
                  Version {pipeline.version}
                  {pipeline.updatedAt && <> · saved {formatRelative(pipeline.updatedAt)}</>}
                </span>
              )}
            </p>
          </div>
        </div>

        <div className="pl-top-actions">
          <div className="pl-viewswitch" role="tablist" aria-label="Pipeline sections">
            <button
              type="button"
              role="tab"
              aria-selected={view === "stages"}
              className={`btn-ghost${view === "stages" ? " is-on" : ""}`}
              onClick={() => setView("stages")}
            >
              <PlIcon name="stages" /> Stages <span className="pl-count">{stages.length}</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={view === "inputs"}
              className={`btn-ghost${view === "inputs" ? " is-on" : ""}`}
              onClick={() => setView("inputs")}
            >
              <PlIcon name="inputs" /> Build inputs <span className="pl-count">{parameters.length}</span>
              {inputProblems > 0 && <span className="pl-dot is-error" aria-label="needs fixing" />}
            </button>
          </div>
          {canEdit && (
            <div className="pl-menu" ref={menuRef}>
              <button
                type="button"
                className="btn-ghost pl-menu-trigger"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((open) => !open)}
                disabled={saving}
              >
                <PlIcon name="more" />
                <span className="pl-sr">More pipeline actions</span>
              </button>
              {menuOpen && (
                <div className="pl-menu-list" role="menu">
                  <button
                    type="button"
                    role="menuitem"
                    className="btn-ghost"
                    onClick={() => {
                      setMenuOpen(false);
                      setImporting(true);
                    }}
                  >
                    <PlIcon name="upload" />
                    <span>
                      <strong>Import a Jenkinsfile</strong>
                      <small>Translate it into stages and inputs as a draft to review</small>
                    </span>
                  </button>
                  <button type="button" role="menuitem" className="btn-ghost" onClick={resetToTemplate}>
                    <PlIcon name="reset" />
                    <span>
                      <strong>Reset to the starter pipeline</strong>
                      <small>{applicationTypeLabel(service.applicationType)} template · saves immediately</small>
                    </span>
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </header>

      {error && (
        <div className="pl-banner is-error" role="alert">
          <PlIcon name="alert" />
          <p>{error}</p>
          <button type="button" className="btn-ghost pl-banner-close" aria-label="Dismiss" onClick={() => setError("")}>
            <PlIcon name="x" />
          </button>
        </div>
      )}

      {/* ── The starter pipeline, said plainly ─────────────────────────── */}
      {generated && (
        <section className="pl-starter" aria-label="KubeSight starter pipeline">
          <span className="pl-starter-glyph" aria-hidden="true">
            <PlIcon name="sparkle" />
          </span>
          <div>
            <strong>KubeSight builds this service with a starter pipeline</strong>
            <p>
              Picked for{" "}
              {pipeline.defaultMetadata?.applicationTypeLabel || applicationTypeLabel(service.applicationType)}
              {pipeline.defaultMetadata?.detectedCommand && (
                <>
                  {" "}— it runs <code>{pipeline.defaultMetadata.detectedCommand}</code>
                </>
              )}
              {pipeline.defaultMetadata?.detectedFiles?.length > 0 && (
                <> because the repository has {pipeline.defaultMetadata.detectedFiles.join(", ")}</>
              )}
              . It regenerates on every build, so it follows the repository. Customize it to take
              control; the stages below become yours to edit.
            </p>
          </div>
          {canEdit && (
            <button type="button" className="primary" onClick={customizeDefault}>
              Customize pipeline
            </button>
          )}
        </section>
      )}

      {/* ── What the Jenkinsfile left to do ────────────────────────────── */}
      {importNotes && (
        <section className="pl-import" aria-label="Jenkinsfile import notes">
          <header>
            <PlIcon name="upload" />
            <strong>Imported from a Jenkinsfile</strong>
            <span>{importNotes.summary}</span>
            {/* "Dismiss", not a trash icon: the notes are a reminder, and an
                icon that reads as delete makes people keep a list they have
                already dealt with. */}
            <button type="button" className="btn-outline btn-compact" onClick={() => setImportNotes(null)}>
              Dismiss
            </button>
          </header>
          {importNotes.blocking?.length > 0 && (
            <ul className="pl-import-blocking">
              {importNotes.blocking.map((message, index) => (
                <li key={index}>
                  <PlIcon name="alert" /> {message}
                </li>
              ))}
            </ul>
          )}
          {importNotes.notes?.length > 0 && (
            <ul className="pl-import-notes">
              {importNotes.notes.map((note, index) => {
                const position = note.stage ? stages.findIndex((stage) => stage.name === note.stage) : -1;
                return (
                  <li key={index} className={`is-${note.level}`}>
                    {position >= 0 ? (
                      <button type="button" className="btn-ghost pl-link" onClick={() => select(position)}>
                        {note.stage}
                      </button>
                    ) : (
                      <span className="pl-import-scope">{note.stage || "Pipeline"}</span>
                    )}
                    <span>{note.message}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}

      {/* ── Health: what stands between this draft and a good build ───── */}
      {view === "stages" && (blockingStages.length > 0 || warningStages.length > 0) && (
        <div className={`pl-health${blockingStages.length ? " is-error" : ""}`} role="status">
          <PlIcon name="alert" />
          <p>
            {blockingStages.length > 0 ? (
              <>
                <strong>
                  {blockingStages.length} {blockingStages.length === 1 ? "stage needs" : "stages need"} fixing before saving
                </strong>
              </>
            ) : (
              <strong>
                {warningStages.length} {warningStages.length === 1 ? "stage has" : "stages have"} something to check
              </strong>
            )}
          </p>
          <div className="pl-health-links">
            {[...blockingStages, ...warningStages].slice(0, 6).map(({ index }) => (
              <button key={index} type="button" className="btn-ghost pl-health-link" onClick={() => select(index)}>
                <span>{index + 1}</span>
                {stages[index]?.name || "Unnamed stage"}
              </button>
            ))}
            {blockingStages.length + warningStages.length > 6 && (
              <span className="muted">+{blockingStages.length + warningStages.length - 6} more</span>
            )}
          </div>
        </div>
      )}

      {view === "inputs" ? (
        <div className="pl-panel">
          <BuildParameters
            parameters={parameters}
            stages={stages}
            canEdit={editable}
            onChange={changeParameters}
            onOpenStage={(index) => {
              setView("stages");
              select(index);
            }}
          />
        </div>
      ) : (
        <div className="pl-workspace">
          <StageFlow
            stages={stages}
            selectedIndex={selectedIndex}
            mode={mode}
            insertAt={insertAt}
            problems={problems}
            changes={diff.perStage}
            editable={editable}
            branch={service.defaultBranch}
            onSelect={select}
            onMove={move}
            onInsert={openInserter}
            endSlot={
              <PostActionsBlock
                actions={postActions}
                selectedIndex={mode === "post" ? selectedPost : null}
                adding={mode === "post-add"}
                problems={postProblems}
                changes={postChanges}
                editable={editable}
                onSelect={selectPost}
                onAdd={openPostPicker}
              />
            }
          />

          <div className="pl-panel pl-stage-panel">
            {mode === "post-add" && editable ? (
              <PostActionPicker
                onPick={addPost}
                onCancel={() => setMode(postActions.length && selectedPost !== null ? "post" : stages.length ? "stage" : "add")}
              />
            ) : mode === "post" && selectedPost !== null && postActions[selectedPost] ? (
              <PostActionSheet
                key={postActions[selectedPost]._key ?? `post-${selectedPost}`}
                action={postActions[selectedPost]}
                index={selectedPost}
                total={postActions.length}
                actions={postActions}
                secretKeys={secretKeys}
                problems={postProblems[selectedPost] || []}
                change={postChanges[selectedPost]}
                editable={editable}
                onChange={(patch) => mutatePost(selectedPost, patch)}
                onChangeType={(type) => changePostKind(selectedPost, type)}
                onMove={(delta) => movePost(selectedPost, selectedPost + delta)}
                onRemove={() => removePost(selectedPost)}
                onGoToTab={onGoToTab}
              />
            ) : mode === "add" && editable ? (
              <>
                <StagePicker
                  stages={stages}
                  insertAt={insertAt}
                  onPick={addKind}
                  onCopy={(index) => copyOf(index, insertAt)}
                  onCancel={stages.length ? () => setMode("stage") : null}
                />
                {!stages.length && (
                  <div className="pl-starter-options">
                    <span>Or begin from something that already works</span>
                    <button type="button" className="btn-outline btn-compact" onClick={resetToTemplate}>
                      <PlIcon name="reset" /> {applicationTypeLabel(service.applicationType)} starter pipeline
                    </button>
                    <button type="button" className="btn-outline btn-compact" onClick={() => setImporting(true)}>
                      <PlIcon name="upload" /> Import a Jenkinsfile
                    </button>
                  </div>
                )}
              </>
            ) : current ? (
              <StageSheet
                key={current._key ?? current.id ?? `new-${selectedIndex}`}
                service={service}
                stage={current}
                index={selectedIndex}
                total={stages.length}
                stages={stages}
                parameters={parameters}
                secretKeys={secretKeys}
                scanTools={scanTools}
                problems={problems[selectedIndex] || []}
                change={diff.perStage[selectedIndex]}
                editable={editable}
                focusName={focusToken}
                onChange={(patch) => mutate(selectedIndex, patch)}
                onChangeKind={(stageType) => changeKind(selectedIndex, stageType)}
                onDuplicate={() => copyOf(selectedIndex, selectedIndex + 1)}
                onMove={(delta) => move(selectedIndex, selectedIndex + delta)}
                onRemove={() => remove(selectedIndex)}
                onGoToTab={onGoToTab}
                onOpenInputs={() => setView("inputs")}
                onGroupChange={editable ? (action, value) => changeGroup(selectedIndex, action, value) : undefined}
                parallelCapability={lint?.parallel || null}
              />
            ) : (
              <div className="pl-empty">
                <span className="pl-empty-glyph" aria-hidden="true">
                  <PlIcon name="stages" />
                </span>
                <strong>No stages</strong>
                <p>
                  {generated
                    ? "The starter pipeline could not be generated for this repository yet. Customize it to write the stages yourself."
                    : "There is nothing to configure."}
                </p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Save bar: only while there is something to save ────────────── */}
      <div className="pl-dock">
        {(removed || notice) && (
          <div className="pl-toast" role="status">
            <PlIcon name={removed ? "trash" : "check"} />
            <span>{removed ? `Removed “${removed.stage.name || "Unnamed stage"}”` : notice}</span>
            {removed && (
              <button type="button" className="btn-ghost pl-toast-action" onClick={undoRemove}>
                <PlIcon name="undo" /> Undo
              </button>
            )}
            <button
              type="button"
              className="btn-ghost"
              aria-label="Dismiss"
              onClick={() => (removed ? setRemoved(null) : setNotice(""))}
            >
              <PlIcon name="x" />
            </button>
          </div>
        )}
        {canEdit && dirty && (
          <div className="pl-savebar" role="region" aria-label="Unsaved pipeline changes">
            <span
              className={`pl-savebar-dot${blockingStages.length || inputProblems || blockingPost >= 0 ? " is-error" : ""}`}
              aria-hidden="true"
            />
            <div className="pl-savebar-text">
              <strong>{saving ? "Saving…" : "Unsaved changes"}</strong>
              <span>
                {blockingStages.length || inputProblems || blockingPost >= 0 ? (
                  `${
                    blockingStages.length + inputProblems + postProblems.filter((list) => list.length).length
                  } to fix before saving`
                ) : (
                  [describeDiff(diff), postDirty ? "post actions changed" : ""].filter(Boolean).join(", ") ||
                  (generated ? "" : "Ready to save")
                )}
              </span>
            </div>
            <button type="button" className="btn-outline btn-compact" onClick={discard} disabled={saving}>
              Discard
            </button>
            <button type="button" className="primary btn-compact" onClick={save} disabled={saving || generated}>
              <PlIcon name="check" />
              {saving ? "Saving…" : "Save pipeline"}
              <kbd>Ctrl S</kbd>
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
