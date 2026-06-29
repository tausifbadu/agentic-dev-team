# Frontend Design System & Quality Standard

## Mandate

You are building a **distinctive, production-grade interface** — not a template.
A UI that merely "works" is a failure here. Generic is the enemy: if this app
could be swapped for any other app and look the same, you have not done the job.
Apply this process and these craft standards on every frontend story.

## Default register: professional / enterprise (unless the brief explicitly says otherwise)

Build a serious, business-grade interface — an admin console / internal tool — NOT a
consumer landing page. Only depart from this when the requirement explicitly asks for a
consumer, marketing, playful, or branded experience.

- **Theme:** LIGHT by default — near-white / light-gray background, white cards. Use a
  dark theme only when the brief asks for it.
- **Palette:** a neutral gray scale (backgrounds, borders, text) + ONE restrained brand
  accent (a calm blue / indigo / navy is a safe professional default) + functional
  status colors (green / amber / red) used ONLY for status. No neon, no aqua/teal/
  vibrant pastels, no two-color brand gradients.
- **Surfaces:** solid cards with a subtle 1px border + a soft shadow. NOT glassmorphism,
  NOT `backdrop-blur`, NOT translucent panels.
- **Decoration:** essentially none. No glow shadows, no gradient text, no decorative
  background orbs / gradient washes / animated grids. Polish comes from spacing,
  alignment, and typography — never from effects.
- **Density:** information-dense and legibility-first. Tables and lists are first-class
  (comfortable row height, hairline separators, tabular figures, sortable headers,
  clear column alignment).
- **Motion:** minimal and functional (subtle hover/focus, quick fades). No flourishes.

This register + the Scale & density rules below are what make a UI read "professional"
instead of "trendy." If it looks like a crypto/SaaS landing page, it's wrong.

## 1. Derive an identity from the app's subject (do this at the shell/scaffold story, then reuse)

Before writing components, commit a **token system tailored to THIS app's domain**.
A finance tool, a location registry, and a music app should not look alike.

- **Pick the subject's mood in one line.** e.g. "calm, trustworthy, map-like" for a
  location app. Let that drive every choice below.
- **Palette — 4–6 named hex values:** `--ground` (background), `--surface` (raised
  cards/inputs), `--text` + a muted text, **one** `--accent` (interactive/brand), and
  optionally one supporting status hue. The accent must not vibrate or mud against
  the ground; shift saturation rather than swapping it.
- **Typography — 2 roles minimum:** a **characterful display face** (headings, used
  with restraint) + a **clean body face**. Pair them deliberately — do not set the
  whole app in one default sans. Define a type scale (e.g. 12/14/16/20/28/40) with
  intentional weights. Type carries the personality of the page.
- **Define tokens as CSS variables / Tailwind theme** and derive EVERY color, space,
  and radius from them. Consistency across components is most of what reads as
  "polished." Never hardcode ad-hoc hexes in components.

**Avoid the three machine-generated default looks** unless the subject genuinely
calls for one: (a) cream background + high-contrast serif + terracotta accent;
(b) near-black + a single acid-green/vermilion accent; (c) broadsheet hairline rules
with zero radius. They are defaults, not choices.

---

## 2. Craft standards (non-negotiable on every screen)

- **Spacing:** use a 4/8px scale (4,8,12,16,24,32,48). Consistent, breathing-room
  whitespace — but *consistent rhythm*, NOT oversized everything. Cramped is the #1
  amateur tell; over-inflated is the #2 tell (it screams "landing page").
- **Scale & density — match it to the product type (this is commonly gotten WRONG).**
  A tool / productivity / dashboard / CRUD app is **compact-comfortable**, not a
  spacious marketing page. Reserve big type and large hero spacing for actual
  landing/marketing pages.
  - **Type:** body 14–16px; section headings ~`text-lg`/`text-xl`; the app's own title
    ~`text-xl`/`text-2xl`. Do **NOT** use `text-4xl`/`text-5xl` for an app header —
    that is marketing-hero scale.
  - **Controls** (buttons, inputs): visually **~36–40px tall** (`h-9`/`h-10`), padding
    ~`px-3`/`px-4 py-2`. Hit the 44px touch-target minimum via padding / hit-area —
    do NOT make every control 48px+ tall just to reach it.
  - **Radius:** 8–12px (`rounded-lg`/`rounded-xl`) for most elements. Avoid pill-radius
    (>16px / `rounded-2xl`+) on every card and button — it reads chunky/bubbly.
  - **Density:** default sections to `gap-4`/`gap-6`, card padding `p-4`/`p-5` — reserve
    larger gaps for top-level section breaks only.
  - Rule of thumb: if a screenshot of one screen looks like a product *hero/landing*
    rather than a working tool, the scale is too big — tighten it.
- **Hierarchy:** establish it with size, weight, and spacing — not color alone. One
  clear focal point per view.
- **Depth & surface:** separate layers with subtle elevation (soft shadow + a 1px
  border or a lighter surface), not heavy drop shadows.
- **States — the most-skipped quality bar. Every screen must handle all of these:**
  - Interactive elements: `hover`, **visible `focus-visible`**, `active`, `disabled`.
  - Data views: **loading** (skeleton/shimmer, not a bare spinner for >300ms),
    **empty** (a helpful message + a clear next action — never a blank box), and
    **error** (what went wrong + how to recover).
- **Motion:** 150–300ms, **ease-out for enter / ease-in for exit**, animate
  `transform`/`opacity` only (never width/height/top/left). Stagger lists ~30–50ms.
  **One orchestrated moment** beats scattered effects. Always respect
  `prefers-reduced-motion`.
- **Icons:** inline SVG only (Lucide/Heroicons style, consistent stroke width).
  **NEVER emoji** as UI icons.
- **Accessibility:** body text ≥ 4.5:1 contrast; a visible keyboard focus ring on
  every control; real `<label>`s; logical tab order; don't convey meaning by color
  alone.
- **Responsive:** mobile-first; no horizontal scroll; a consistent `max-w` container;
  fluid type/spacing.

---

## 3. Component quality bars (recipes, not fixed styles)

- **Buttons:** clear primary vs secondary vs ghost hierarchy; pressed feedback
  (subtle scale/opacity); loading state disables + shows a spinner; ≥44px tap target.
- **Cards / panels:** consistent padding from the spacing scale, token-driven surface
  + border, subtle hover lift only if interactive.
- **Forms:** visible label above each field (not placeholder-as-label); helper text;
  **inline error directly below the field**; validate on blur, not per keystroke;
  semantic input types.
- **Tables / lists:** comfortable row height; zebra or hairline separation; sortable
  affordance if sortable; a real empty state; tabular figures for numeric columns.
- **Nav:** the current location is visually highlighted; consistent placement across
  pages.
- **Modals / sheets:** animate from their trigger; a scrim strong enough to isolate
  (40–60% black); clear close affordance + Escape to dismiss.
- **Toasts:** auto-dismiss 3–5s; never steal focus; `aria-live="polite"`.

---

## 4. Copy is design material — write it deliberately

Generic copy makes a UI feel as templated as a generic layout.
- **Active voice, sentence case.** A control says what it does: "Save changes," not
  "Submit." Keep an action's name consistent through the flow ("Publish" → toast
  "Published").
- **Name things by what the user controls/recognizes**, never by system internals.
- **Errors** explain the cause + the fix, in the product's voice — no bare "Invalid
  input," no apologies.
- **Empty states** invite an action, they are not dead ends.

---

## 5. Anti-patterns (instant quality kills — never ship these)

- Emoji used as icons.
- Gray text on a gray background; insufficient contrast.
- Cramped, inconsistent spacing; everything center-aligned by default.
- A single loud accent color smeared across the whole page (spend boldness in ONE
  place; keep the rest quiet).
- Missing loading/empty/error states.
- Unstyled default browser controls (raw `<select>`, default checkbox) in a themed app.
- Hardcoded colors instead of design tokens.
- Lorem ipsum or placeholder copy left in.
- **For professional/enterprise apps specifically (the default register), these read as
  "trendy/consumer" and are anti-patterns:** glassmorphism / `backdrop-blur` panels,
  glow shadows, gradient text (`bg-clip-text`), decorative background orbs / gradient
  washes / animated grids, neon or aqua/teal vibrant palettes, dark-mode-by-default,
  oversized pill radii on everything, and big marketing-hero headlines on a tool screen.

---

## 6. Pre-ship checklist — verify ALL before calling finish_story

```
[ ] Design tokens defined and used consistently (no ad-hoc hexes)
[ ] A deliberate display+body type pairing and a type scale
[ ] hover / focus-visible / active / disabled on every interactive element
[ ] loading + empty + error states for every data view
[ ] Contrast ≥ 4.5:1; visible focus ring; real labels
[ ] Responsive, no horizontal scroll, consistent container width
[ ] Inline SVG icons only — zero emoji
[ ] Exactly one deliberate motion moment; prefers-reduced-motion respected
[ ] Copy is specific, active-voice, sentence case
[ ] The screen looks like THIS app, not a generic template
```

Spend your boldness in one place, keep everything around it disciplined, and cut any
decoration that does not serve the subject. Polish is consistency + restraint +
finished states — not more effects.
