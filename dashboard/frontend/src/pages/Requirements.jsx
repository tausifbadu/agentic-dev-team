import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";

export default function Requirements() {
  const [requirements, setRequirements] = useState([]);
  const [promptFiles, setPromptFiles] = useState([]);
  const [text, setText] = useState("");
  const [autoApprove, setAutoApprove] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const navigate = useNavigate();

  const refresh = () => {
    api.listRequirements().then(setRequirements).catch(console.error);
    api.listPromptFiles().then(setPromptFiles).catch(console.error);
  };

  useEffect(refresh, []);

  const submit = async (e) => {
    e.preventDefault();
    if (!text.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.submitRequirement(text.trim(), autoApprove);
      setText("");
      refresh();
      navigate(`/stories/${result.storypack_id}`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const submitFromFile = async (filename) => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.submitFromFile(filename, autoApprove);
      refresh();
      navigate(`/stories/${result.storypack_id}`);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-xl font-semibold text-white">Requirements</h2>
        <p className="text-sm text-slate-500 mt-1">
          Submit a new requirement or load from a prompt file
        </p>
      </div>

      <div className="bg-surface-1 rounded-xl border border-border p-6">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label htmlFor="req-input" className="block text-xs font-medium text-slate-400 uppercase tracking-wider mb-2">
              Describe what you want to build
            </label>
            <textarea
              id="req-input"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Build a REST API that..."
              rows={5}
              className="w-full bg-surface-2 border border-border rounded-lg px-4 py-3 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:ring-2 focus:ring-accent/40 focus:border-accent/50 resize-y transition-all duration-150"
            />
          </div>
          <div className="flex items-center gap-4 flex-wrap">
            <button
              type="submit"
              disabled={loading || !text.trim()}
              className="px-5 py-2.5 bg-accent text-white text-sm font-semibold rounded-lg hover:bg-accent-hover disabled:opacity-40 disabled:cursor-not-allowed transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-accent/50 focus:ring-offset-2 focus:ring-offset-surface-1"
            >
              {loading ? (
                <span className="flex items-center gap-2">
                  <span className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  Creating stories...
                </span>
              ) : (
                "Submit to PM Agent"
              )}
            </button>
            <label className="flex items-center gap-2 cursor-pointer select-none group">
              <div className="relative">
                <input
                  type="checkbox"
                  checked={autoApprove}
                  onChange={(e) => setAutoApprove(e.target.checked)}
                  className="sr-only peer"
                />
                <div className="w-9 h-5 bg-surface-3 rounded-full peer-checked:bg-accent transition-colors duration-200 border border-border" />
                <div className="absolute top-0.5 left-0.5 w-4 h-4 bg-slate-400 rounded-full peer-checked:translate-x-4 peer-checked:bg-white transition-all duration-200" />
              </div>
              <span className="text-sm text-slate-400 group-hover:text-slate-300 transition-colors">
                Auto-approve & run
              </span>
            </label>
            {error && (
              <span className="text-sm text-rose-400">{error}</span>
            )}
          </div>
        </form>

        {promptFiles.length > 0 && (
          <div className="mt-5 pt-5 border-t border-border-subtle">
            <p className="text-[11px] text-slate-500 mb-3 font-medium uppercase tracking-wider">
              Or load from prompt file
            </p>
            <div className="flex flex-wrap gap-2">
              {promptFiles.map((f) => (
                <button
                  key={f.name}
                  onClick={() => submitFromFile(f.filename)}
                  disabled={loading}
                  className="px-3.5 py-1.5 bg-surface-3 text-sm text-slate-300 rounded-lg hover:bg-surface-4 hover:text-white disabled:opacity-40 transition-all duration-150 border border-border-subtle"
                  title={f.first_line}
                >
                  {f.name}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="bg-surface-1 rounded-xl border border-border overflow-hidden">
        <div className="px-6 py-4 border-b border-border-subtle">
          <h3 className="text-sm font-semibold text-slate-300">History</h3>
        </div>
        {requirements.length === 0 ? (
          <div className="px-6 py-12 text-center">
            <p className="text-sm text-slate-600">No requirements submitted yet.</p>
          </div>
        ) : (
          <div className="divide-y divide-border-subtle">
            {requirements.map((req) => (
              <div
                key={req.id}
                className="px-6 py-4 hover:bg-surface-2/50 cursor-pointer transition-colors duration-150"
                onClick={() =>
                  req.storypack_id && navigate(`/stories/${req.storypack_id}`)
                }
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-mono text-slate-500">{req.id}</p>
                    <p className="text-sm text-slate-300 mt-1 line-clamp-2">
                      {req.text.slice(0, 200)}
                      {req.text.length > 200 && "..."}
                    </p>
                  </div>
                  <StatusBadge status={req.storypack_status} />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status }) {
  const styles = {
    pending_review: "bg-amber-500/15 text-amber-400 ring-amber-500/20",
    approved: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/20",
    in_progress: "bg-blue-500/15 text-blue-400 ring-blue-500/20",
    completed: "bg-cyan-500/15 text-cyan-400 ring-cyan-500/20",
    failed: "bg-rose-500/15 text-rose-400 ring-rose-500/20",
    rejected: "bg-rose-500/15 text-rose-400 ring-rose-500/20",
  };
  if (!status) return null;
  return (
    <span
      className={`inline-flex px-2.5 py-1 text-[11px] font-semibold rounded-md ring-1 ring-inset ${
        styles[status] || "bg-slate-500/15 text-slate-400 ring-slate-500/20"
      }`}
    >
      {status.replace("_", " ")}
    </span>
  );
}
