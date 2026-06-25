// PmThinking — a hero "the PM agent is working" overlay shown while the
// (blocking) submit-requirement request is in flight.
//
// Design concept: dramatize the PM's actual job as a CHOREOGRAPHED SEQUENCE —
// one high-level requirement is read, broken into features, then decomposed into
// user stories routed to the three agent disciplines (Frontend / Backend / Tests).
// The hero builds up stage by stage in lock-step with a labelled stepper:
//
//   1 Reading the requirement      → scan-line sweeps the requirement node
//   2 Identifying core features    → the requirement node breathes / is studied
//   3 Drafting user stories        → connectors draw, light pulses fan out, story
//                                     chips cascade in
//   4 Defining acceptance criteria → the story lines shimmer into place
//   5 Assigning to FE · BE · QA    → each chip locks to its owner colour
//
// Honesty: the PM call is one opaque LLM step with no real sub-progress, so the
// sequence is illustrative/ambient — it advances then HOLDS on the final stage
// (never a fake "all done" state) until the real story pack returns, then resolves
// to the true count.
//
// Type: headings use the `display` face (Space Grotesk); body uses Inter. Tokens
// from tailwind.config.js. The bespoke choreography lives in the scoped <style>
// block; the global prefers-reduced-motion rule in index.css neutralises it.

import { useEffect, useState } from "react";

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

const LANES = [
  { key: "fe", label: "Frontend", color: "#4ade80", dim: "rgba(74,222,128,0.16)", left: 12 },
  { key: "be", label: "Backend", color: "#60a5fa", dim: "rgba(96,165,250,0.16)", left: 152 },
  { key: "qa", label: "Tests", color: "#fbbf24", dim: "rgba(251,191,36,0.16)", left: 292 },
];

const STAGES = [
  "Reading the requirement",
  "Identifying core features",
  "Drafting user stories",
  "Defining acceptance criteria",
  "Assigning to FE · BE · QA",
];

const STAGE_MS = 3000;
const ASSIGN_MS = 1500;

function Stepper({ active, complete }) {
  return (
    <div className="flex items-center justify-center gap-0 mt-3.5 mb-0.5" aria-hidden="true">
      {STAGES.map((_, i) => {
        const state = complete || i < active ? "done" : i === active ? "active" : "pending";
        return (
          <div key={i} className="flex items-center">
            <span
              className={cx(
                "block rounded-full transition-all duration-300",
                state === "active" ? "pm-step w-2.5 h-2.5 bg-accent" : "w-2 h-2",
                state === "done" && "bg-accent/70",
                state === "pending" && "bg-fg-faint/40"
              )}
            />
            {i < STAGES.length - 1 && (
              <span className="relative block w-7 h-px mx-1 bg-fg-faint/25 overflow-hidden">
                <span
                  className="absolute inset-y-0 left-0 bg-accent/70 transition-all duration-500"
                  style={{ width: complete || i < active ? "100%" : "0%" }}
                />
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function PmThinking({ active, storyCount = null }) {
  const [stage, setStage] = useState(0);
  const [assignIdx, setAssignIdx] = useState(0);
  const done = typeof storyCount === "number";

  useEffect(() => {
    if (!active || done) return;
    setStage(0);
    setAssignIdx(0);
    const id = setInterval(
      () => setStage((s) => Math.min(s + 1, STAGES.length - 1)),
      STAGE_MS
    );
    return () => clearInterval(id);
  }, [active, done]);

  // The final stage ("Assigning to owners") sub-cycles through the three agent
  // disciplines so the long wait for the PM call shows continuous, meaningful
  // activity — and the matching chip highlights — instead of freezing on one label.
  useEffect(() => {
    if (!active || done || stage < STAGES.length - 1) return;
    const id = setInterval(() => setAssignIdx((i) => (i + 1) % LANES.length), ASSIGN_MS);
    return () => clearInterval(id);
  }, [active, done, stage]);

  if (!active) return null;

  const assigning = !done && stage >= STAGES.length - 1;
  const activeAssign = assigning ? LANES[assignIdx].key : null;
  const stageLabel = assigning ? `Assigning to ${LANES[assignIdx].label}` : STAGES[stage];
  const showWires = done || stage >= 2;
  const showChips = done || stage >= 2;
  const showPulses = !done && stage >= 2;
  const settled = done || stage >= 4;

  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={done ? `${storyCount} stories ready` : `PM agent: ${stageLabel}`}
      className="fixed inset-0 z-50 flex items-center justify-center bg-surface-0/80 backdrop-blur-sm animate-fade-in"
    >
      <style>{PM_CSS}</style>
      <div className="w-[min(94vw,470px)] bg-surface-1 border border-border rounded-2xl shadow-raised px-6 pt-6 pb-6 animate-scale-in overflow-hidden">
        {/* eyebrow */}
        <div className="flex items-center justify-center gap-2 mb-1">
          <span className={cx("w-1.5 h-1.5 rounded-full bg-accent", !done && "pm-live")} />
          <span className="text-[10.5px] font-semibold tracking-[0.18em] text-fg-faint uppercase">
            Product Manager
          </span>
        </div>

        {/* hero stage: 420×270 so the SVG connectors line up with the chips */}
        <div className="relative mx-auto" style={{ width: 420, height: 270 }}>
          {/* central requirement node + rotating aura */}
          <div className="absolute" style={{ left: 178, top: 4, width: 64, height: 64 }}>
            {!done && <span className="pm-aura absolute -inset-3 rounded-full" />}
            <span
              className={cx("absolute inset-0 rounded-2xl", !done && "pm-breathe")}
              style={{ background: "linear-gradient(150deg,#1e293b,#273349)", border: "1px solid rgba(74,222,128,0.35)", boxShadow: "0 0 18px rgba(34,197,94,0.35)" }}
            />
            <div className="relative w-16 h-16 rounded-2xl flex items-center justify-center overflow-hidden text-accent-hover">
              {done ? (
                <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" className="animate-pop">
                  <polyline points="5 12.5 10 17.5 19 7" />
                </svg>
              ) : (
                <>
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M7 3.5h7L18 7.5v11a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 18.5v-13A1.5 1.5 0 0 1 7 3.5z" />
                    <path d="M13.5 3.5V8H18" />
                    <path d="M8.6 12h6.8M8.6 15h4.4" opacity="0.7" />
                  </svg>
                  {stage === 0 && <span className="pm-scan absolute inset-x-0 h-6" />}
                </>
              )}
            </div>
          </div>

          {/* connectors + traveling pulses */}
          {showWires && (
            <svg viewBox="0 0 420 270" className="absolute inset-0 w-full h-full" fill="none">
              {LANES.map((lane, i) => (
                <path
                  key={lane.key}
                  d={PATHS[lane.key]}
                  className="pm-wire"
                  stroke={settled ? lane.color : "rgba(148,163,184,0.30)"}
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeDasharray="300"
                  style={{ animationDelay: `${i * 90}ms` }}
                />
              ))}
              {showPulses &&
                LANES.map((lane, i) => (
                  <circle
                    key={lane.key}
                    r="3"
                    fill={lane.color}
                    className={`pm-dot pm-dot-${lane.key}`}
                    style={{ filter: `drop-shadow(0 0 5px ${lane.color})`, animationDelay: `${i * 240}ms` }}
                  />
                ))}
            </svg>
          )}

          {/* owner chips */}
          {showChips &&
            LANES.map((lane, i) => {
              const isAssigning = lane.key === activeAssign;
              return (
              <div
                key={lane.key}
                className="pm-chip absolute w-[116px] rounded-xl border bg-surface-2/90 backdrop-blur-sm overflow-hidden"
                style={{
                  left: lane.left,
                  top: 200,
                  borderColor: isAssigning || settled ? lane.color : "rgba(148,163,184,0.18)",
                  boxShadow: isAssigning
                    ? `0 0 0 1.5px ${lane.color}, 0 0 22px ${lane.dim}`
                    : settled
                    ? `0 0 16px ${lane.dim}`
                    : "0 4px 16px -2px rgba(0,0,0,0.5)",
                  animationDelay: `${i * 130}ms`,
                  transition: "border-color 300ms, box-shadow 300ms",
                }}
              >
                <span className="absolute left-0 top-0 bottom-0 w-1" style={{ background: lane.color }} />
                <div className="pl-3.5 pr-3 py-2.5">
                  <div className="flex items-center gap-1.5 mb-2">
                    <span className="w-1.5 h-1.5 rounded-full" style={{ background: lane.color, boxShadow: `0 0 6px ${lane.color}` }} />
                    <span className="text-[11px] font-semibold tracking-wide" style={{ color: lane.color }}>{lane.label}</span>
                  </div>
                  <div className="space-y-1.5">
                    <div className="pm-shimmer h-1.5 w-full rounded-full" style={{ animationDelay: `${i * 130 + 200}ms` }} />
                    <div className="pm-shimmer h-1.5 w-3/4 rounded-full" style={{ animationDelay: `${i * 130 + 360}ms` }} />
                  </div>
                </div>
              </div>
              );
            })}
        </div>

        {/* copy + stepper */}
        <div className="text-center mt-1">
          {done ? (
            <>
              <h3 className="font-display text-[19px] font-semibold text-fg animate-pop tracking-tight">
                Story pack ready
              </h3>
              <p className="text-[13px] text-fg-muted mt-1">
                {storyCount} {storyCount === 1 ? "story" : "stories"} across Frontend, Backend &amp; Tests
              </p>
            </>
          ) : (
            <>
              <h3 key={assigning ? `a${assignIdx}` : stage} className="font-display text-[18px] font-medium text-fg-secondary animate-fade-in tracking-tight">
                {stageLabel}
                <span className="pm-ell text-accent-hover">…</span>
              </h3>
              <p className="text-[12px] text-fg-faint mt-0.5 tabular-nums">
                {assigning ? `Assigning · ${assignIdx + 1} of ${LANES.length}` : `Step ${stage + 1} of ${STAGES.length}`}
              </p>
            </>
          )}
          <Stepper active={done ? STAGES.length : stage} complete={done} />
        </div>
      </div>
    </div>
  );
}

const PATHS = {
  fe: "M210,68 C 168,134 96,150 70,200",
  be: "M210,68 C 210,130 210,150 210,200",
  qa: "M210,68 C 252,134 324,150 350,200",
};

const PM_CSS = `
.pm-aura {
  background: conic-gradient(from 0deg, transparent 0%, transparent 55%, rgba(34,197,94,0.55) 80%, rgba(74,222,128,0.95) 92%, transparent 100%);
  filter: blur(6px);
  animation: pm-aura-spin 4.5s linear infinite;
}
@keyframes pm-aura-spin { to { transform: rotate(360deg); } }

.pm-live { animation: pm-live 1.6s ease-in-out infinite; }
@keyframes pm-live { 0%,100% { opacity: 1; box-shadow: 0 0 0 0 rgba(34,197,94,0.5); } 50% { opacity: 0.5; box-shadow: 0 0 0 4px rgba(34,197,94,0); } }

.pm-breathe { animation: pm-breathe 3s ease-in-out infinite; }
@keyframes pm-breathe {
  0%,100% { transform: scale(1); box-shadow: 0 0 16px rgba(34,197,94,0.30); }
  50%     { transform: scale(1.045); box-shadow: 0 0 26px rgba(34,197,94,0.55); }
}

.pm-scan {
  background: linear-gradient(to bottom, transparent, rgba(74,222,128,0.55), transparent);
  animation: pm-scan 1.9s ease-in-out infinite;
}
@keyframes pm-scan {
  0%   { transform: translateY(-130%); opacity: 0; }
  35%  { opacity: 1; } 65% { opacity: 1; }
  100% { transform: translateY(330%); opacity: 0; }
}

.pm-wire { animation: pm-draw 0.7s ease-out both; }
@keyframes pm-draw { from { stroke-dashoffset: 300; opacity: 0; } to { stroke-dashoffset: 0; opacity: 1; } }

.pm-dot { animation: pm-flow 1.7s cubic-bezier(0.45,0,0.35,1) infinite; }
.pm-dot-fe { offset-path: path("M210,68 C 168,134 96,150 70,200"); }
.pm-dot-be { offset-path: path("M210,68 C 210,130 210,150 210,200"); }
.pm-dot-qa { offset-path: path("M210,68 C 252,134 324,150 350,200"); }
@keyframes pm-flow {
  0%   { offset-distance: 0%;   opacity: 0; }
  12%  { opacity: 1; } 88% { opacity: 1; }
  100% { offset-distance: 100%; opacity: 0; }
}

.pm-chip { animation: pm-cascade 0.5s cubic-bezier(0.34,1.56,0.64,1) both; }
@keyframes pm-cascade {
  from { opacity: 0; transform: translateY(18px) scale(0.95); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}

.pm-shimmer {
  background: linear-gradient(90deg, rgba(148,163,184,0.18) 25%, rgba(148,163,184,0.42) 50%, rgba(148,163,184,0.18) 75%);
  background-size: 200% 100%;
  animation: pm-shimmer 1.5s ease-in-out infinite;
}
@keyframes pm-shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

.pm-step { animation: pm-step 1.3s ease-in-out infinite; }
@keyframes pm-step { 0%,100% { box-shadow: 0 0 0 0 rgba(34,197,94,0.5); } 50% { box-shadow: 0 0 0 5px rgba(34,197,94,0); } }

.pm-ell { animation: pm-ell 1.4s steps(4,end) infinite; }
@keyframes pm-ell { 0% { opacity: 0.2; } 50% { opacity: 1; } 100% { opacity: 0.2; } }
`;
