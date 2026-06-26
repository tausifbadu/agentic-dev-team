import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../api";
import {
  PageHeader,
  Card,
  Button,
  Badge,
  EmptyState,
  Toggle,
  Select,
} from "../components/ui";

const STATUS_COLUMNS = [
  { key: "pending_review", label: "Pending Review", accent: "bg-amber-400" },
  { key: "in_progress", label: "In Progress", accent: "bg-sky-400" },
  { key: "done", label: "Done", accent: "bg-emerald-400" },
];

const OWNERSHIP_STYLES = {
  backend: "bg-violet-500/15 text-violet-400 ring-violet-500/20",
  frontend: "bg-cyan-500/15 text-cyan-400 ring-cyan-500/20",
  testing: "bg-amber-500/15 text-amber-400 ring-amber-500/20",
};

export default function StoryBoard() {
  const { packId } = useParams();
  const navigate = useNavigate();
  const [pack, setPack] = useState(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState(null);

  const [selectedIds, setSelectedIds] = useState(() => new Set());
  // When on, run EXACTLY the selected stories without auto-adding prerequisites.
  const [noDeps, setNoDeps] = useState(false);
  // When on, re-run selected stories even if they already completed (overwrites).
  const [force, setForce] = useState(false);
  // Per-story model overrides {story_id: model}; "" / absent = agent default.
  const [models, setModels] = useState([]);
  const [storyModels, setStoryModels] = useState({});

  const refresh = () => {
    api.getStorypack(packId).then(setPack).catch(setError).finally(() => setLoading(false));
  };

  useEffect(refresh, [packId]);

  // While a run is in flight, poll so story cards move across the columns live.
  useEffect(() => {
    if (pack?.status !== "in_progress" && pack?.status !== "approved") return;
    const id = setInterval(() => {
      api.getStorypack(packId).then(setPack).catch(() => {});
    }, 3000);
    return () => clearInterval(id);
  }, [pack?.status, packId]);

  useEffect(() => {
    api.listModels().then((r) => setModels(r.models || [])).catch(() => {});
  }, []);

  useEffect(() => {
    if (pack?.stories?.length) {
      // Pre-select everything not already completed: on the first run that's all
      // stories; on a resume it's just the pending/failed remainder.
      setSelectedIds(new Set(pack.stories.filter((s) => s.status !== "done").map((s) => s.id)));
      // Seed model overrides from any previously-persisted per-story choices.
      const m = {};
      pack.stories.forEach((s) => { if (s.model) m[s.id] = s.model; });
      setStoryModels(m);
    }
  }, [pack?.id]);

  const setStoryModel = (id, model) =>
    setStoryModels((prev) => ({ ...prev, [id]: model }));

  const collectStoryModels = () =>
    Object.fromEntries(Object.entries(storyModels).filter(([, v]) => v));

  const toggleStory = (id) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAllStories = () => {
    if (!pack?.stories) return;
    // Already-completed stories are skipped by the runtime, so "select all" means
    // all the still-runnable ones.
    setSelectedIds(new Set(pack.stories.filter((s) => s.status !== "done").map((s) => s.id)));
  };

  const clearStorySelection = () => {
    setSelectedIds(new Set());
  };

  const handleApprove = async (fastTrack = false) => {
    if (!pack?.stories?.length) return;
    const allIds = new Set(pack.stories.map((s) => s.id));
    const fullSelection =
      selectedIds.size === allIds.size && [...allIds].every((id) => selectedIds.has(id));
    if (!fullSelection && selectedIds.size === 0) {
      setError("Select at least one story to implement.");
      return;
    }
    setActionLoading(true);
    setError(null);
    try {
      const opts = {};
      if (fastTrack) opts.fast_track = true;
      if (!fullSelection) opts.story_ids = Array.from(selectedIds);
      if (noDeps) opts.include_dependencies = false;
      const sm = collectStoryModels();
      if (Object.keys(sm).length) opts.story_models = sm;
      await api.approveStorypack(packId, opts);
      refresh();
      navigate("/agents");
    } catch (err) {
      setError(err.message);
    } finally {
      setActionLoading(false);
    }
  };

  const handleReject = async () => {
    setActionLoading(true);
    try {
      await api.rejectStorypack(packId);
      refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setActionLoading(false);
    }
  };

  const handleResume = async (storyIds = null) => {
    setActionLoading(true);
    setError(null);
    try {
      // storyIds set => run only those (completed ones still skipped server-side).
      // null/empty => run every story that hasn't completed yet (and retry failed).
      const body = storyIds && storyIds.length ? { story_ids: storyIds } : {};
      if (noDeps && body.story_ids) body.include_dependencies = false;
      if (force) body.force = true;
      const sm = collectStoryModels();
      if (Object.keys(sm).length) body.story_models = sm;
      const res = await api.resumeStorypack(packId, body);
      if (res?.status === "noop") {
        setError("All selected stories are already completed — nothing to resume.");
      } else {
        refresh();
        navigate("/agents");
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setActionLoading(false);
    }
  };

  if (loading) return <p className="text-sm text-fg-faint">Loading...</p>;
  if (!pack && error) return <p className="text-sm text-status-danger-fg">{String(error)}</p>;
  if (!pack) return <p className="text-sm text-fg-faint">StoryPack not found.</p>;

  const isResumable = pack.status === "completed" || pack.status === "failed";
  // Stories can be picked before the first run (pending_review) AND on every resume
  // run after — only the not-yet-completed ones are selectable.
  const canSelect = pack.status === "pending_review" || isResumable;

  const storiesByStatus = {};
  STATUS_COLUMNS.forEach((col) => (storiesByStatus[col.key] = []));
  pack.stories.forEach((s) => {
    const bucket = storiesByStatus[s.status] ? s.status : "pending_review";
    storiesByStatus[bucket].push(s);
  });

  return (
    <div className="space-y-8">
      {error && (
        <p className="text-sm text-status-danger-fg bg-rose-950/40 border border-rose-500/25 rounded-lg px-4 py-2">
          {String(error)}
        </p>
      )}
      <PageHeader
        title="Story Board"
        subtitle={
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-mono text-fg-muted">{packId}</span>
            <span className="text-fg-faint">/</span>
            <span className={
              pack.status === "approved" ? "text-status-success-fg" :
              pack.status === "rejected" ? "text-status-danger-fg" :
              "text-status-warning-fg"
            }>{pack.status.replace("_", " ")}</span>
            <Badge
              className="ml-1 bg-accent-muted text-accent-hover ring-1 ring-inset ring-accent/30 font-mono"
              title="Filesystem workspace this storypack builds into"
            >
              <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor"
                strokeWidth="1.5" className="mr-1 -ml-0.5">
                <path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0 1 14 6v5.5A1.5 1.5 0 0 1 12.5 13h-9A1.5 1.5 0 0 1 2 11.5v-7z" />
              </svg>
              {(pack.project_id && pack.project_id !== "default")
                ? `workspace/projects/${pack.project_id}`
                : "workspace/ (default)"}
            </Badge>
          </span>
        }
        actions={
          <>
            {pack.status === "pending_review" && (
              <>
                <Button variant="danger" size="lg" onClick={handleReject} disabled={actionLoading}>
                  Reject
                </Button>
                <Button
                  size="lg"
                  onClick={() => handleApprove(false)}
                  loading={actionLoading}
                  disabled={actionLoading}
                >
                  {actionLoading ? "Processing..." : "Approve & Run Agents"}
                </Button>
                <Button
                  variant="secondary"
                  size="lg"
                  onClick={() => handleApprove(true)}
                  disabled={actionLoading}
                  title="Skips test agent phase and smoke phase"
                >
                  {actionLoading ? "…" : "Fast track"}
                </Button>
              </>
            )}
            {isResumable && (
              <>
                <Button
                  size="lg"
                  onClick={() => handleResume(Array.from(selectedIds))}
                  loading={actionLoading}
                  disabled={actionLoading || selectedIds.size === 0}
                  title="Run only the stories you've selected (already-completed ones are skipped; prerequisites are added automatically)."
                >
                  {actionLoading ? "Processing..." : `Run selected (${selectedIds.size})`}
                </Button>
                <Button
                  variant="secondary"
                  size="lg"
                  onClick={() => handleResume(null)}
                  disabled={actionLoading}
                  title="Run every story that hasn't completed yet (and retry failed)."
                >
                  Run all remaining
                </Button>
              </>
            )}
          </>
        }
      />

      <Card className="p-5">
        <p className="text-[11px] font-medium text-fg-faint uppercase tracking-wider mb-2">Requirement</p>
        <p className="text-sm text-fg-secondary whitespace-pre-wrap max-h-36 overflow-y-auto leading-relaxed">
          {pack.requirement_text}
        </p>
      </Card>

      {canSelect && (
        <div className="flex flex-wrap items-center gap-3 text-sm text-fg-muted">
          <span>
            Selected{" "}
            <span className="text-fg font-medium">{selectedIds.size}</span>
            {" / "}
            {pack.stories.filter((s) => s.status !== "done").length} runnable stories
            {isResumable ? " (completed stories are skipped)." : " (prerequisite stories are added automatically)."}
          </span>
          <Button variant="ghost" size="sm" onClick={selectAllStories} className="text-accent hover:text-accent">
            Select all
          </Button>
          <Button variant="ghost" size="sm" onClick={clearStorySelection}>
            Clear
          </Button>
          {isResumable && (
            <Toggle
              checked={force}
              onChange={(e) => setForce(e.target.checked)}
              label="Re-run even if completed"
              className="ml-auto"
            />
          )}
          <Toggle
            checked={noDeps}
            onChange={(e) => setNoDeps(e.target.checked)}
            label="Run without dependencies"
            className={isResumable ? "" : "ml-auto"}
          />
        </div>
      )}
      {force && (
        <p className="text-xs text-status-warning-fg -mt-4">
          Completed stories you select will be re-run and their previous result overwritten.
        </p>
      )}
      {noDeps && (
        <p className="text-xs text-status-warning-fg -mt-4">
          Prerequisites will NOT be added — only the stories you tick run. Use for a quick
          partial build (e.g. the UI shell). Selected stories may fail if code they import isn't present.
        </p>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
        {STATUS_COLUMNS.map((col) => (
          <Card key={col.key} padded={false} className="overflow-hidden">
            <div className="px-4 py-3 border-b border-border-subtle flex items-center gap-2.5">
              <div className={`w-2 h-2 rounded-full ${col.accent}`} />
              <h3 className="text-[13px] font-semibold text-fg-secondary">
                {col.label}
              </h3>
              <span className="text-xs text-fg-faint font-mono ml-auto">
                {storiesByStatus[col.key].length}
              </span>
            </div>
            <div className="p-3 space-y-2.5 min-h-[120px]">
              {storiesByStatus[col.key].map((story) => (
                <StoryCard
                  key={story.id}
                  story={story}
                  selectable={canSelect && (force || story.status !== "done")}
                  selected={selectedIds.has(story.id)}
                  onToggleSelect={() => toggleStory(story.id)}
                  models={models}
                  model={storyModels[story.id] || ""}
                  onModelChange={(m) => setStoryModel(story.id, m)}
                />
              ))}
              {storiesByStatus[col.key].length === 0 && (
                <EmptyState title="No stories" className="px-3 py-8" />
              )}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

function StoryCard({ story, selectable, selected, onToggleSelect, models = [], model = "", onModelChange }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div
      className="bg-surface-2 border border-border-subtle rounded-lg p-3 cursor-pointer hover:border-border-strong transition-all duration-150"
      onClick={() => setExpanded(!expanded)}
    >
      <div className="flex items-start gap-2.5">
        {selectable && (
          <input
            type="checkbox"
            checked={selected}
            onChange={(e) => {
              e.stopPropagation();
              onToggleSelect();
            }}
            onClick={(e) => e.stopPropagation()}
            className="mt-1 rounded border-border-strong text-emerald-600 focus:ring-emerald-500/40"
            aria-label={`Include ${story.title} in run`}
          />
        )}
        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between gap-2">
            <p className="text-[13px] font-medium text-fg">{story.title}</p>
            <Badge
              className={`ring-1 ring-inset whitespace-nowrap ${
                OWNERSHIP_STYLES[story.ownership] || "bg-slate-500/15 text-slate-400 ring-slate-500/20"
              }`}
            >
              {story.ownership}
            </Badge>
          </div>
          <p className="text-xs text-fg-faint mt-1.5 leading-relaxed">{story.description}</p>
          {selectable && models.length > 0 && (
            <div className="mt-2 flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
              <span className="text-[10px] uppercase tracking-wider text-fg-faint shrink-0">Model</span>
              <Select
                value={model}
                onChange={(e) => onModelChange?.(e.target.value)}
                className="text-xs py-1"
              >
                <option value="">Default</option>
                {models.map((m) => (
                  <option key={m} value={m}>{m.split("/").pop()}</option>
                ))}
              </Select>
            </div>
          )}
          {model && !selectable && (
            <p className="text-[10px] text-fg-faint mt-1.5">model: {model.split("/").pop()}</p>
          )}
          {expanded && (
            <div className="mt-3 pt-3 border-t border-border-subtle space-y-2.5 text-xs">
              <div>
                <p className="font-medium text-fg-muted mb-1">Acceptance Criteria</p>
                <ul className="space-y-1 text-fg-faint">
                  {story.acceptance_criteria.map((ac, i) => (
                    <li key={i} className="flex gap-1.5">
                      <span className="text-accent mt-0.5 shrink-0">-</span>
                      {ac}
                    </li>
                  ))}
                </ul>
              </div>
              {story.implementation_notes?.length > 0 && (
                <div>
                  <p className="font-medium text-fg-muted mb-1">Notes</p>
                  <ul className="space-y-1 text-fg-faint">
                    {story.implementation_notes.map((n, i) => (
                      <li key={i} className="flex gap-1.5">
                        <span className="text-violet-400 mt-0.5 shrink-0">-</span>
                        {n}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {story.dependencies?.length > 0 && (
                <p className="text-fg-faint">
                  Depends on: <span className="font-mono text-fg-faint">{story.dependencies.join(", ")}</span>
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
