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

  const refresh = () => {
    api.getStorypack(packId).then(setPack).catch(setError).finally(() => setLoading(false));
  };

  useEffect(refresh, [packId]);

  const handleApprove = async () => {
    setActionLoading(true);
    try {
      await api.approveStorypack(packId);
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
  if (error) return <p className="text-sm text-rose-400">{String(error)}</p>;
  if (!pack) return <p className="text-sm text-slate-500">StoryPack not found.</p>;

  const storiesByStatus = {};
  STATUS_COLUMNS.forEach((col) => (storiesByStatus[col.key] = []));
  pack.stories.forEach((s) => {
    const bucket = storiesByStatus[s.status] ? s.status : "pending_review";
    storiesByStatus[bucket].push(s);
  });

  return (
    <div className="space-y-8">
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
              onClick={handleApprove}
              disabled={actionLoading}
              className="px-4 py-2.5 text-sm font-semibold bg-emerald-600 text-white rounded-lg hover:bg-emerald-500 disabled:opacity-40 transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:ring-offset-2 focus:ring-offset-surface-0"
            >
              {actionLoading ? "Processing..." : "Approve & Run Agents"}
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
                <StoryCard key={story.id} story={story} />
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

function StoryCard({ story }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div
      className="bg-surface-2 border border-border-subtle rounded-lg p-3 cursor-pointer hover:border-border-strong transition-all duration-150"
      onClick={() => setExpanded(!expanded)}
    >
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
  );
}
