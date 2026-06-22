import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import {
  PageHeader,
  Card,
  Button,
  Spinner,
  EmptyState,
  Label,
  Select,
  Textarea,
  Toggle,
} from "../components/ui";

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
      <PageHeader
        title="Workspace chat"
        subtitle="Per-project assistant: read and search the generated workspace; optional file writes (bypasses PM/Supervisor)."
      />

      <div className="flex flex-wrap items-center gap-4">
        <Label className="mb-0">Project</Label>
        <Select
          value={projectId}
          onChange={(e) => onProjectChange(e.target.value)}
          className="min-w-[220px]"
        >
          {(projects.length ? projects : [{ id: "default" }]).map((p) => (
            <option key={p.id} value={p.id}>
              {p.id === "default" ? "default (workspace/)" : p.id}
            </option>
          ))}
        </Select>

        <Toggle
          checked={allowWrites}
          onChange={(e) => setAllowWrites(e.target.checked)}
          label="Allow file writes"
        />

        <Button variant="secondary" onClick={newSession}>
          New session
        </Button>

        {sessionId && (
          <span className="text-[11px] font-mono text-fg-faint truncate max-w-[200px]">{sessionId}</span>
        )}
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          {error}
        </div>
      )}

      <Card padded={false} className="min-h-[420px] max-h-[55vh] overflow-y-auto p-4 space-y-3">
        {lines.length === 0 ? (
          <EmptyState icon={<Spinner size="md" />} title="Starting session…" />
        ) : (
          lines.map((row, i) => (
            <div
              key={i}
              className={`text-sm rounded-lg px-3 py-2 ${
                row.role === "user"
                  ? "bg-accent-muted/30 text-fg ml-8"
                  : row.role === "assistant"
                    ? "bg-surface-2 text-fg-secondary mr-8 whitespace-pre-wrap"
                    : row.role === "error"
                      ? "bg-red-500/10 text-red-200"
                      : "bg-surface-3/50 text-fg-faint font-mono text-xs"
              }`}
            >
              {row.text}
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </Card>

      <div className="flex gap-2">
        <Textarea
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
          className="flex-1 min-h-[80px]"
        />
        <Button
          variant="primary"
          size="lg"
          onClick={send}
          loading={busy}
          disabled={busy || !sessionId || !input.trim()}
          className="self-end"
        >
          {busy ? "" : "Send"}
        </Button>
      </div>
    </div>
  );
}
