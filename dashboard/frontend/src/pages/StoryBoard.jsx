import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../api";

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

  const refresh = () => {
    api.getStorypack(packId).then(setPack).catch(setError).finally(() => setLoading(false));
  };

  useEffect(refresh, [packId]);

  useEffect(() => {
    if (pack?.stories?.length) {
      setSelectedIds(new Set(pack.stories.map((s) => s.id)));
    }
  }, [pack?.id]);

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
    setSelectedIds(new Set(pack.stories.map((s) => s.id)));
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

  if (loading) return <p className="text-sm text-slate-500">Loading...</p>;
  if (!pack && error) return <p className="text-sm text-rose-400">{String(error)}</p>;
  if (!pack) return <p className="text-sm text-slate-500">StoryPack not found.</p>;

  const storiesByStatus = {};
  STATUS_COLUMNS.forEach((col) => (storiesByStatus[col.key] = []));
  pack.stories.forEach((s) => {
    const bucket = storiesByStatus[s.status] ? s.status : "pending_review";
    storiesByStatus[bucket].push(s);
  });

  return (
    <div className="space-y-8">
      {error && (
        <p className="text-sm text-rose-400 bg-rose-950/40 border border-rose-500/25 rounded-lg px-4 py-2">
          {String(error)}
        </p>
      )}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold text-white">Story Board</h2>
          <p className="text-sm text-slate-500 mt-1">
            <span className="font-mono text-slate-400">{packId}</span>
            <span className="mx-2 text-slate-600">/</span>
            <span className={
              pack.status === "approved" ? "text-emerald-400" :
              pack.status === "rejected" ? "text-rose-400" :
              "text-amber-400"
            }>{pack.status.replace("_", " ")}</span>
          </p>
        </div>
        {pack.status === "pending_review" && (
          <div className="flex gap-2">
            <button
              onClick={handleReject}
              disabled={actionLoading}
              className="px-4 py-2.5 text-sm font-medium text-rose-400 rounded-lg border border-rose-500/30 hover:bg-rose-500/10 disabled:opacity-40 transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-rose-500/30"
            >
              Reject
            </button>
            <button
              onClick={() => handleApprove(false)}
              disabled={actionLoading}
              className="px-4 py-2.5 text-sm font-semibold bg-emerald-600 text-white rounded-lg hover:bg-emerald-500 disabled:opacity-40 transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:ring-offset-2 focus:ring-offset-surface-0"
            >
              {actionLoading ? "Processing..." : "Approve & Run Agents"}
            </button>
            <button
              type="button"
              onClick={() => handleApprove(true)}
              disabled={actionLoading}
              title="Skips test agent phase and smoke phase"
              className="px-4 py-2.5 text-sm font-medium text-amber-300 rounded-lg border border-amber-500/40 hover:bg-amber-500/10 disabled:opacity-40 transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-amber-500/30"
            >
              {actionLoading ? "…" : "Fast track"}
            </button>
          </div>
        )}
      </div>

      <div className="bg-surface-1 rounded-xl border border-border p-5">
        <p className="text-[11px] font-medium text-slate-500 uppercase tracking-wider mb-2">Requirement</p>
        <p className="text-sm text-slate-300 whitespace-pre-wrap max-h-36 overflow-y-auto leading-relaxed">
          {pack.requirement_text}
        </p>
      </div>

      {pack.status === "pending_review" && (
        <div className="flex flex-wrap items-center gap-3 text-sm text-slate-400">
          <span>
            Run{" "}
            <span className="text-slate-200 font-medium">{selectedIds.size}</span>
            {" / "}
            {pack.stories.length} stories (prerequisite stories are added automatically).
          </span>
          <button
            type="button"
            onClick={selectAllStories}
            className="text-xs font-medium text-accent hover:underline"
          >
            Select all
          </button>
          <button
            type="button"
            onClick={clearStorySelection}
            className="text-xs font-medium text-slate-500 hover:text-slate-300"
          >
            Clear
          </button>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
        {STATUS_COLUMNS.map((col) => (
          <div key={col.key} className="bg-surface-1 rounded-xl border border-border overflow-hidden">
            <div className="px-4 py-3 border-b border-border-subtle flex items-center gap-2.5">
              <div className={`w-2 h-2 rounded-full ${col.accent}`} />
              <h3 className="text-[13px] font-semibold text-slate-300">
                {col.label}
              </h3>
              <span className="text-xs text-slate-600 font-mono ml-auto">
                {storiesByStatus[col.key].length}
              </span>
            </div>
            <div className="p-3 space-y-2.5 min-h-[120px]">
              {storiesByStatus[col.key].map((story) => (
                <StoryCard
                  key={story.id}
                  story={story}
                  selectable={pack.status === "pending_review"}
                  selected={selectedIds.has(story.id)}
                  onToggleSelect={() => toggleStory(story.id)}
                />
              ))}
              {storiesByStatus[col.key].length === 0 && (
                <p className="text-xs text-slate-600 text-center py-8">No stories</p>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function StoryCard({ story, selectable, selected, onToggleSelect }) {
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
            <p className="text-[13px] font-medium text-slate-200">{story.title}</p>
            <span
              className={`inline-flex px-1.5 py-0.5 text-[10px] font-semibold rounded ring-1 ring-inset whitespace-nowrap ${
                OWNERSHIP_STYLES[story.ownership] || "bg-slate-500/15 text-slate-400 ring-slate-500/20"
              }`}
            >
              {story.ownership}
            </span>
          </div>
          <p className="text-xs text-slate-500 mt-1.5 leading-relaxed">{story.description}</p>
          {expanded && (
            <div className="mt-3 pt-3 border-t border-border-subtle space-y-2.5 text-xs">
              <div>
                <p className="font-medium text-slate-400 mb-1">Acceptance Criteria</p>
                <ul className="space-y-1 text-slate-500">
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
                  <p className="font-medium text-slate-400 mb-1">Notes</p>
                  <ul className="space-y-1 text-slate-500">
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
                <p className="text-slate-600">
                  Depends on: <span className="font-mono text-slate-500">{story.dependencies.join(", ")}</span>
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
