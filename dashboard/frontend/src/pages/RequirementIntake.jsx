import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import { api } from "../api";
import { PageHeader, Card, Button, Spinner, Textarea, Badge } from "../components/ui";

const STEPS = ["Refine", "Review stories", "Approve", "Run"];

function StepBar({ active = 0 }) {
  return (
    <div className="flex items-center gap-2 text-[11px] font-medium">
      {STEPS.map((s, i) => (
        <div key={s} className="flex items-center gap-2">
          <span
            className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 ${
              i === active
                ? "bg-accent-muted text-accent-hover"
                : i < active
                  ? "text-emerald-400"
                  : "text-fg-faint"
            }`}
          >
            <span
              className={`grid place-items-center w-4 h-4 rounded-full text-[9px] ${
                i === active ? "bg-accent text-white" : "bg-surface-3 text-fg-faint"
              }`}
            >
              {i + 1}
            </span>
            {s}
          </span>
          {i < STEPS.length - 1 && <span className="text-fg-faint/40">→</span>}
        </div>
      ))}
    </div>
  );
}

function fmt(n) {
  return Number(n || 0).toLocaleString();
}

export default function RequirementIntake() {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const initialText = location.state?.initialText || "";

  const [lines, setLines] = useState([]); // {role, text}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [booting, setBooting] = useState(true);

  const [spec, setSpec] = useState("");
  const [specLoading, setSpecLoading] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [turnTokens, setTurnTokens] = useState(0);
  const [sessionTokens, setSessionTokens] = useState(0);

  const bottomRef = useRef(null);
  const startedRef = useRef(false);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [lines]);

  // Boot: rehydrate transcript; if empty, auto-send the raw requirement as turn 1.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await api.getIntakeMessages(sessionId);
        if (!alive) return;
        const msgs = (data.messages || []).map((m) => ({ role: m.role, text: m.content }));
        setLines(msgs);
        if (data.session?.refined_text) setSpec(data.session.refined_text);
        setBooting(false);
        if (msgs.length === 0 && initialText && !startedRef.current) {
          startedRef.current = true;
          runTurn(initialText);
        }
      } catch (e) {
        if (alive) {
          setError(String(e.message || e));
          setBooting(false);
        }
      }
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const runTurn = async (msg) => {
    if (!msg || busy) return;
    setBusy(true);
    setError(null);
    setTurnTokens(0);
    setLines((prev) => [...prev, { role: "user", text: msg }]);

    const handleEvent = (ev) => {
      if (ev.type === "usage" && ev.scope === "turn") {
        setTurnTokens(ev.total_tokens || 0);
        setSessionTokens((s) => s + (ev.total_tokens || 0));
      } else if (ev.type === "assistant" && ev.content) {
        setLines((prev) => [...prev, { role: "assistant", text: ev.content }]);
      } else if (ev.type === "error") {
        setError(ev.message || "error");
        setLines((prev) => [...prev, { role: "error", text: ev.message || "error" }]);
      }
    };

    try {
      const res = await api.postIntakeStream({ session_id: sessionId, message: msg });
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
          try {
            handleEvent(JSON.parse(line));
          } catch {
            /* ignore partial */
          }
        }
      }
      if (buf.trim()) {
        try {
          handleEvent(JSON.parse(buf));
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

  const send = () => {
    const msg = input.trim();
    if (!msg) return;
    setInput("");
    runTurn(msg);
  };

  const generateSpec = async () => {
    setSpecLoading(true);
    setError(null);
    try {
      const res = await api.synthesizeIntake(sessionId);
      setSpec(res.refined_text || "");
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setSpecLoading(false);
    }
  };

  const createStories = async () => {
    setFinalizing(true);
    setError(null);
    try {
      // Pass the (possibly edited) spec if the user generated one; else let the
      // server synthesize from the conversation.
      const res = await api.finalizeIntake(sessionId, spec.trim() ? spec : null);
      navigate(`/stories/${res.storypack_id}`);
    } catch (e) {
      setError(String(e.message || e));
      setFinalizing(false);
    }
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="Refine requirement"
        subtitle="The PM asks clarifying questions until the requirement is clear. Nothing is built until you create stories."
        actions={<StepBar active={0} />}
      />

      {error && (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-2.5 text-sm text-rose-300">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-5">
        {/* Conversation */}
        <div className="lg:col-span-3 flex flex-col gap-3">
          <Card
            padded={false}
            className="flex-1 min-h-[440px] max-h-[58vh] overflow-y-auto p-4 space-y-3"
          >
            {booting ? (
              <div className="grid place-items-center h-40">
                <Spinner size="md" />
              </div>
            ) : (
              lines.map((row, i) => (
                <div
                  key={i}
                  className={`text-sm rounded-lg px-3 py-2 ${
                    row.role === "user"
                      ? "bg-accent-muted/30 text-fg ml-10"
                      : row.role === "assistant"
                        ? "bg-surface-2 text-fg-secondary mr-10 whitespace-pre-wrap"
                        : "bg-rose-500/10 text-rose-300"
                  }`}
                >
                  {row.text}
                </div>
              ))
            )}
            {busy && (
              <div className="flex items-center gap-2 text-xs text-fg-faint mr-10 px-3">
                <Spinner size="sm" /> PM is thinking…
              </div>
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
              placeholder="Answer the PM's questions, or add detail / corrections…"
              rows={3}
              disabled={busy || booting}
              className="flex-1 min-h-[76px]"
            />
            <Button
              variant="primary"
              size="lg"
              onClick={send}
              loading={busy}
              disabled={busy || booting || !input.trim()}
              className="self-end"
            >
              {busy ? "" : "Send"}
            </Button>
          </div>
          <p className="text-[11px] text-fg-faint">
            {sessionTokens > 0 && <>Session ~{fmt(sessionTokens)} tok{turnTokens ? ` · +${fmt(turnTokens)} last turn` : ""} · </>}
            Enter to send · Shift+Enter for newline
          </p>
        </div>

        {/* Refined spec + confirm gate */}
        <div className="lg:col-span-2 flex flex-col gap-3">
          <Card padded={false} className="flex flex-col overflow-hidden">
            <div className="flex items-center justify-between px-4 py-3 border-b border-border-subtle/60">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-fg">Refined requirement</span>
                {spec && <Badge className="border-transparent bg-emerald-500/15 text-emerald-400">draft</Badge>}
              </div>
              <Button variant="secondary" size="sm" onClick={generateSpec} loading={specLoading} disabled={specLoading || busy}>
                {spec ? "Refresh" : "Generate"}
              </Button>
            </div>
            <div className="p-4 space-y-3">
              {spec ? (
                <Textarea
                  value={spec}
                  onChange={(e) => setSpec(e.target.value)}
                  rows={16}
                  className="w-full text-xs font-mono leading-relaxed"
                />
              ) : (
                <p className="text-xs text-fg-faint py-6 text-center">
                  Once the conversation has enough detail, click <span className="text-fg-muted">Generate</span> to
                  build a consolidated spec you can edit, or just{" "}
                  <span className="text-fg-muted">Create stories</span> and the PM will synthesize it for you.
                </p>
              )}
            </div>
          </Card>

          <Card className="space-y-3">
            <p className="text-[11px] text-fg-faint">
              When you're happy with the requirement, generate the stories. You'll review and approve them next — nothing is
              implemented yet.
            </p>
            <Button
              variant="primary"
              size="md"
              onClick={createStories}
              loading={finalizing}
              disabled={finalizing || busy || booting}
              className="w-full"
            >
              {finalizing ? "Creating stories…" : "Create stories →"}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => navigate("/")}
              disabled={finalizing}
              className="w-full"
            >
              Back to requirements
            </Button>
          </Card>
        </div>
      </div>
    </div>
  );
}
