import { useEffect, useRef, useState } from "react";
import { api } from "../api";

function formatEvent(ev) {
  switch (ev.type) {
    case "meta":
      return `Ready — project ${ev.project_id}`;
    case "iteration":
      return `Step ${ev.n}`;
    case "assistant":
      return ev.content;
    case "tool_start":
      return `→ ${ev.name}(${JSON.stringify(ev.arguments)})`;
    case "tool_result":
      return `← ${ev.name}: ${ev.truncated ? "(truncated) " : ""}${String(ev.content).slice(0, 600)}${String(ev.content).length > 600 ? "…" : ""}`;
    case "error":
      return `Error: ${ev.message}`;
    case "done":
      return `Done (${ev.reason})`;
    default:
      return JSON.stringify(ev);
  }
}

export default function WorkspaceChat() {
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState("default");
  const [sessionId, setSessionId] = useState(null);
  const [allowWrites, setAllowWrites] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [lines, setLines] = useState([]);
  const [error, setError] = useState(null);
  const bottomRef = useRef(null);

  useEffect(() => {
    api.listProjects().then((r) => setProjects(r.projects || [])).catch(console.error);
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [lines]);

  const bootstrapSession = async (pid) => {
    setError(null);
    try {
      const data = await api.createWorkspaceChatSession(pid || "default");
      setSessionId(data.session_id);
      setLines([
        { role: "system", text: `Session ${data.session_id} — project "${data.project_id}"` },
      ]);
    } catch (e) {
      setError(String(e.message || e));
    }
  };

  useEffect(() => {
    bootstrapSession("default");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onProjectChange = (pid) => {
    setProjectId(pid);
    bootstrapSession(pid);
  };

  const newSession = () => bootstrapSession(projectId);

  const send = async () => {
    const msg = input.trim();
    if (!msg || !sessionId || busy) return;
    setInput("");
    setBusy(true);
    setError(null);
    setLines((prev) => [...prev, { role: "user", text: msg }]);

    try {
      const res = await api.postWorkspaceChatStream({
        session_id: sessionId,
        project_id: projectId,
        message: msg,
        allow_writes: allowWrites,
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split("\n");
        buf = parts.pop() || "";
        for (const line of parts) {
          if (!line.trim()) continue;
          let ev;
          try {
            ev = JSON.parse(line);
          } catch {
            continue;
          }
          if (ev.type === "assistant" && ev.content) {
            setLines((prev) => [...prev, { role: "assistant", text: ev.content }]);
          } else if (ev.type !== "assistant") {
            setLines((prev) => [...prev, { role: "event", text: formatEvent(ev), raw: ev }]);
          }
        }
      }
      if (buf.trim()) {
        try {
          const ev = JSON.parse(buf);
          if (ev.type === "assistant" && ev.content) {
            setLines((prev) => [...prev, { role: "assistant", text: ev.content }]);
          } else {
            setLines((prev) => [...prev, { role: "event", text: formatEvent(ev), raw: ev }]);
          }
        } catch {
          /* ignore */
        }
      }
    } catch (e) {
      setError(String(e.message || e));
      setLines((prev) => [...prev, { role: "error", text: String(e.message || e) }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-white">Workspace chat</h2>
        <p className="text-sm text-slate-500 mt-1">
          Per-project assistant: read and search the generated workspace; optional file writes (bypasses PM/Supervisor).
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-4">
        <label className="text-xs text-slate-500 uppercase tracking-wider">Project</label>
        <select
          value={projectId}
          onChange={(e) => onProjectChange(e.target.value)}
          className="bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-slate-200 min-w-[220px]"
        >
          {(projects.length ? projects : [{ id: "default" }]).map((p) => (
            <option key={p.id} value={p.id}>
              {p.id === "default" ? "default (workspace/)" : p.id}
            </option>
          ))}
        </select>

        <label className="flex items-center gap-2 text-sm text-slate-400 cursor-pointer">
          <input
            type="checkbox"
            checked={allowWrites}
            onChange={(e) => setAllowWrites(e.target.checked)}
            className="rounded border-border"
          />
          Allow file writes
        </label>

        <button
          type="button"
          onClick={newSession}
          className="text-sm px-3 py-2 rounded-lg border border-border text-slate-300 hover:bg-surface-2"
        >
          New session
        </button>

        {sessionId && (
          <span className="text-[11px] font-mono text-slate-600 truncate max-w-[200px]">{sessionId}</span>
        )}
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          {error}
        </div>
      )}

      <div className="bg-surface-1 rounded-xl border border-border min-h-[420px] max-h-[55vh] overflow-y-auto p-4 space-y-3">
        {lines.length === 0 ? (
          <p className="text-sm text-slate-600 text-center py-12">Starting session…</p>
        ) : (
          lines.map((row, i) => (
            <div
              key={i}
              className={`text-sm rounded-lg px-3 py-2 ${
                row.role === "user"
                  ? "bg-accent-muted/30 text-slate-100 ml-8"
                  : row.role === "assistant"
                    ? "bg-surface-2 text-slate-200 mr-8 whitespace-pre-wrap"
                    : row.role === "error"
                      ? "bg-red-500/10 text-red-200"
                      : "bg-surface-3/50 text-slate-500 font-mono text-xs"
              }`}
            >
              {row.text}
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>

      <div className="flex gap-2">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          placeholder="Ask about the codebase, request a fix, or describe a missing UI…"
          rows={3}
          disabled={busy || !sessionId}
          className="flex-1 bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-slate-200 placeholder:text-slate-600 focus:outline-none focus:ring-2 focus:ring-accent/40 resize-y min-h-[80px]"
        />
        <button
          type="button"
          onClick={send}
          disabled={busy || !sessionId || !input.trim()}
          className="self-end px-5 py-2 rounded-lg bg-accent text-slate-950 font-semibold text-sm disabled:opacity-40"
        >
          {busy ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}
