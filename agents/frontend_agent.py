"""Frontend Agent. Planner-first React patching with isolated validation."""

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from agents.reasoning import (
    create_scratch_copy,
    load_json,
    load_skill_guidelines,
    promote_scratch_copy,
    render_context,
    save_json,
    select_relevant_files,
    update_scope,
    workspace_export_map,
    workspace_file_tree,
)
from schemas import Story

ProgressCallback = Callable[[str, str | None], None] | None


def _format_files_detail(patch: dict) -> str:
    parts: list[str] = []
    for f in patch.get("files", []):
        path = f.get("path", "unknown")
        content = f.get("content", "")
        parts.append(f"--- {path} ---\n{content}")
    return "\n\n".join(parts)

WORKSPACE = Path(__file__).parent.parent / "workspace"
FRONTEND = WORKSPACE / "frontend"
SCOPE_FILE = WORKSPACE / "scope.json"
DEBUG_DIR = WORKSPACE / "debug"

MODEL_NAME = os.getenv("FRONTEND_AGENT_MODEL", "codex/gpt-5.5")
MAX_RETRIES = 2
MAX_JSON_PARSE_RETRIES = 2

SKILL_UI_PATH = Path(__file__).parent.parent / ".cursor" / "skills" / "agentic-dev-team" / "SKILL_UI.md"
SKILL_REACT_PATH = Path(__file__).parent.parent / ".cursor" / "skills" / "agentic-dev-team" / "SKILL_REACT.md"
UI_GUIDELINES = load_skill_guidelines(SKILL_UI_PATH)
REACT_GUIDELINES = load_skill_guidelines(SKILL_REACT_PATH)

_PLANNER_BASE = """You are a senior frontend implementation planner for a premium React app (Vite + React + Tailwind CSS).
Return JSON only:
{
  "summary": "short plan summary",
  "files_to_touch": ["src/App.jsx", "src/styles.css"],
  "acceptance_map": [{"criterion": "AC", "implementation": "UI behavior"}],
  "design_notes": "visual approach: color usage, layout, animations, component styling",
  "validation_steps": ["build", "runtime states"],
  "risk_checks": ["regressions to avoid"]
}

Rules:
- Prefer additive changes.
- Map every acceptance criterion to a concrete UI state or interaction.
- If backend data is needed, use the backend API contract rather than assuming direct webhook access in the browser.
- The project uses Tailwind CSS with brand color tokens (brand-primary, brand-secondary, brand-accent).
- Plan for a visually polished, premium dark-theme UI. Include design_notes for visual approach:
  glassmorphism cards, gradient accents, smooth animations, generous spacing, icon usage.
- Every component should have loading, error, empty, and data states that look professional.
- CRITICAL FILE AWARENESS: Only reference files that exist in the workspace file listing provided. Do NOT assume modules or components exist unless they appear in the listing. If new files are needed, include them in your plan.
- CRITICAL DEPENDENCY PRESERVATION: An export map shows each file's exported symbols. When planning changes to an existing file, NEVER plan to remove or rename any existing exported function, component, or constant — other files depend on them. Only plan to ADD or EXTEND code.
"""

_PLANNER_WITH_SKILLS = _PLANNER_BASE
if UI_GUIDELINES:
    _PLANNER_WITH_SKILLS += f"\n\nUI/UX Design Guidelines:\n{UI_GUIDELINES}"
if REACT_GUIDELINES:
    _PLANNER_WITH_SKILLS += f"\n\nReact.js Engineering Guidelines:\n{REACT_GUIDELINES}"
PLANNER_PROMPT = _PLANNER_WITH_SKILLS

_CODER_BASE = """You are a Senior Frontend Developer building a premium, modern React app (Vite + React + Tailwind CSS).

Hard constraints:
- Preserve existing functionality.
- Use focused additive patches only.
- Provide visible loading, error, and data states when data fetching is involved.
- The project already has index.html, vite.config.js, src/main.jsx, src/App.jsx, tailwind.config.js, and postcss.config.js.
  Only include them in your output if you need to modify them. Do NOT remove the
  <div id="root"></div> from index.html or the ReactDOM.createRoot call in src/main.jsx.
- If you add new pages or components, import them inside src/App.jsx.
- The project uses Tailwind CSS. Use Tailwind utility classes for styling. Brand colors are available as `brand-primary`, `brand-secondary`, `brand-accent` tokens in the Tailwind config.
- Output JSON only:
{
  "files": [{"path": "relative/path/from/frontend", "content": "full file content"}],
  "dependencies_add": ["package1"],
  "dev_dependencies_add": ["package2"]
}

VISUAL QUALITY REQUIREMENTS (Critical — your UI must look professional):
- Dark theme: deep backgrounds (bg-gray-950, bg-gray-900), elevated cards (bg-gray-800/50 with backdrop-blur), subtle borders (border-white/5 or border-white/10).
- Use gradient accents: bg-gradient-to-r from brand colors for headers, hero sections, and primary buttons.
- Glassmorphism cards: bg-white/5 backdrop-blur-xl border border-white/10 rounded-2xl.
- Subtle shadows: shadow-xl shadow-black/20 on elevated surfaces.
- Typography: font-bold text-white for headings, text-gray-300 for body, text-gray-500 for muted. Use tracking-tight on headings.
- Smooth transitions: transition-all duration-200 on interactive elements. Hover states should shift opacity, scale, or background.
- Animations: use animate-fadeIn or animate-slideUp (defined in tailwind.config.js) for page/card entrance. Stagger list items.
- Buttons: rounded-xl px-6 py-3 font-semibold with hover:brightness-110 and active:scale-[0.98] transform.
- Inputs: bg-white/5 border-white/10 focus:border-brand-primary focus:ring-2 focus:ring-brand-primary/30 rounded-xl.
- Status indicators: colored dots, badges with bg-opacity patterns, not plain text.
- Empty states: centered illustration/icon with helpful text, not just "No data."
- Loading: skeleton pulse animations or spinner with brand color, not plain "Loading..." text.
- Toast/notifications: fixed positioned, glassmorphism style, with enter/exit animations.
- Spacing: generous padding (p-6, p-8 on cards), consistent gap-4 or gap-6 between elements.
- Icons: use inline SVG icons (Heroicons style) for buttons and status indicators. DO NOT use emoji as icons.
- Overall feel: the UI should feel like a premium SaaS dashboard — polished, spacious, with visual depth.
- CRITICAL FILE AWARENESS: A listing of ALL files in the workspace is provided below the context. You MUST ONLY import from files/modules that actually exist in that listing. Do NOT invent or hallucinate imports to files that do not appear in the listing. If you need a new file, create it yourself and include it in your "files" array.
- CRITICAL DEPENDENCY PRESERVATION: An export map showing every file's exported symbols is provided. When you rewrite an existing file, you MUST keep ALL existing exported functions, components, and constants — other files import them. NEVER remove or rename an export unless the story explicitly requires it. Add new code alongside the existing exports.
"""

_CODER_WITH_SKILLS = _CODER_BASE
if UI_GUIDELINES:
    _CODER_WITH_SKILLS += f"\n\nUI/UX Design Guidelines:\n{UI_GUIDELINES}"
if REACT_GUIDELINES:
    _CODER_WITH_SKILLS += f"\n\nReact.js Engineering Guidelines:\n{REACT_GUIDELINES}"
CODER_PROMPT = _CODER_WITH_SKILLS

JSON_RETRY_PROMPT = """Your previous response was not valid JSON. Return ONLY valid JSON matching the requested schema. No markdown fences. No commentary."""
BUILD_RETRY_PROMPT = """Frontend checks failed. First determine whether the issue is in build config, runtime assumptions, or component code. Fix the smallest correct layer and return corrected JSON only."""


def _store_raw_response(raw: str, *, debug_dir: Path) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    out = debug_dir / f"frontend_raw_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.txt"
    out.write_text(raw, encoding="utf-8")


def _call_llm_json(client: OpenAI, prompt: str, system_prompt: str, debug_dir: Path) -> dict:
    from agents.llm_client import call_llm_json

    def on_raw(raw: str) -> None:
        _store_raw_response(raw, debug_dir=debug_dir)

    return call_llm_json(
        client, MODEL_NAME, system_prompt, prompt,
        max_retries=MAX_JSON_PARSE_RETRIES,
        retry_prompt=JSON_RETRY_PROMPT,
        on_raw_response=on_raw,
    )


MAX_REQ_CHARS = 2000


def _load_api_contract(contracts_dir: Path) -> dict | None:
    path = contracts_dir / "api_contract.json"
    if path.exists():
        return load_json(path, {})
    return None


def _story_bundle(
    story: Story,
    requirement_text: str,
    sibling_stories: list[Story],
    scope_file: Path,
    contracts_dir: Path,
) -> str:
    truncated = requirement_text[:MAX_REQ_CHARS] + ("..." if len(requirement_text) > MAX_REQ_CHARS else "")
    siblings = [
        {
            "id": item.id,
            "title": item.title,
            "ownership": item.ownership,
            "acceptance_criteria": item.acceptance_criteria,
            "implementation_notes": item.implementation_notes,
            "test_focus": item.test_focus,
        }
        for item in sibling_stories
        if item.id != story.id
    ]
    bundle: dict = {
        "requirement": truncated,
        "story": story.model_dump(),
        "sibling_stories": siblings,
        "implemented_scope": load_json(scope_file, {"implemented_stories": []}),
    }
    contract = _load_api_contract(contracts_dir)
    if contract:
        bundle["api_contract"] = contract
    return json.dumps(bundle, indent=2)


def _read_existing_frontend(story: Story, requirement_text: str, frontend_dir: Path) -> str:
    text = " ".join(
        [
            requirement_text,
            story.title,
            story.description,
            *story.acceptance_criteria,
            *story.implementation_notes,
            *story.test_focus,
        ]
    )
    files = select_relevant_files(
        frontend_dir, text, {".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".json"}, max_files=10,
    )
    return render_context(frontend_dir, files)


def _plan_frontend_change(
    client: OpenAI,
    story: Story,
    requirement_text: str,
    sibling_stories: list[Story],
    fix_context: str,
    frontend_dir: Path,
    scope_file: Path,
    contracts_dir: Path,
    debug_dir: Path,
) -> dict:
    fix_block = f"\n\nPM Fix / Replan Guidance (incorporate into your plan):\n{fix_context}\n" if fix_context else ""
    fe_suffixes = {".js", ".jsx", ".ts", ".tsx", ".css"}
    file_tree = workspace_file_tree(frontend_dir, fe_suffixes)
    export_map = workspace_export_map(frontend_dir, fe_suffixes)
    export_section = f"\n\nEXPORT MAP (each file's exported symbols — you MUST NOT remove any of these):\n{export_map}" if export_map else ""
    prompt = f"""Planning payload:
{_story_bundle(story, requirement_text, sibling_stories, scope_file, contracts_dir)}

Focused frontend context:
{_read_existing_frontend(story, requirement_text, frontend_dir)}

ALL files currently in the frontend workspace (only import from these):
{file_tree}
{export_section}
{fix_block}"""
    return _call_llm_json(client, prompt, PLANNER_PROMPT, debug_dir)


def _generate_frontend_patch(
    client: OpenAI,
    story: Story,
    requirement_text: str,
    sibling_stories: list[Story],
    plan: dict,
    error_output: str,
    frontend_dir: Path,
    scope_file: Path,
    contracts_dir: Path,
    debug_dir: Path,
) -> dict:
    retry_block = f"\nPrevious validation failure:\n{error_output}\n" if error_output else ""
    fe_suffixes = {".js", ".jsx", ".ts", ".tsx", ".css"}
    file_tree = workspace_file_tree(frontend_dir, fe_suffixes)
    export_map = workspace_export_map(frontend_dir, fe_suffixes)
    export_section = f"\n\nEXPORT MAP (each file's exported symbols — PRESERVE ALL of these when rewriting files):\n{export_map}" if export_map else ""
    prompt = f"""Implementation payload:
{_story_bundle(story, requirement_text, sibling_stories, scope_file, contracts_dir)}

Approved plan:
{json.dumps(plan, indent=2)}

Focused frontend context:
{_read_existing_frontend(story, requirement_text, frontend_dir)}

ALL files currently in the frontend workspace (only import from these — do NOT invent modules):
{file_tree}
{export_section}
{retry_block}
Return only changed files and dependency additions.
When rewriting an existing file, you MUST include ALL its existing exported symbols (see export map above) in your output — even if your story does not modify them. Dropping them will break other files that import them.
"""
    if error_output:
        return _call_llm_json(client, prompt + "\n" + BUILD_RETRY_PROMPT, CODER_PROMPT, debug_dir)
    return _call_llm_json(client, prompt, CODER_PROMPT, debug_dir)


_TAILWIND_CONFIG = """\
/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          primary: '#14BCD9',
          secondary: '#005986',
          accent: '#50BB40',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(20px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        slideDown: {
          '0%': { opacity: '0', transform: 'translateY(-10px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        pulse: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.5' },
        },
      },
      animation: {
        fadeIn: 'fadeIn 0.4s ease-out forwards',
        slideUp: 'slideUp 0.5s ease-out forwards',
        slideDown: 'slideDown 0.3s ease-out forwards',
        shimmer: 'shimmer 1.5s ease-in-out infinite',
        pulse: 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
    },
  },
  plugins: [],
};
"""

_POSTCSS_CONFIG = """\
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
"""

_BASE_STYLES_CSS = """\
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    @apply bg-gray-950 text-gray-200 antialiased;
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
  }
  ::selection { @apply bg-brand-primary/30 text-white; }
  * { scrollbar-width: thin; scrollbar-color: rgba(255,255,255,0.08) transparent; }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { @apply bg-white/10 rounded-full; }
  ::-webkit-scrollbar-thumb:hover { @apply bg-white/20; }
}

@layer components {
  .glass-card {
    @apply bg-white/[0.04] backdrop-blur-xl border border-white/[0.08] rounded-2xl;
  }
  .glass-card-hover {
    @apply glass-card transition-all duration-300 hover:bg-white/[0.07] hover:border-white/[0.12] hover:shadow-xl hover:shadow-black/20;
  }
  .btn-primary {
    @apply inline-flex items-center justify-center gap-2 px-6 py-3 rounded-xl font-semibold
           bg-brand-primary text-gray-950 shadow-lg shadow-brand-primary/20
           transition-all duration-200 hover:brightness-110 hover:shadow-brand-primary/30
           active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:brightness-100;
  }
  .btn-secondary {
    @apply inline-flex items-center justify-center gap-2 px-6 py-3 rounded-xl font-semibold
           bg-brand-secondary text-white shadow-lg shadow-brand-secondary/20
           transition-all duration-200 hover:brightness-125
           active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed;
  }
  .btn-ghost {
    @apply inline-flex items-center justify-center gap-2 px-4 py-2 rounded-xl font-medium
           text-gray-400 transition-all duration-200 hover:bg-white/5 hover:text-white;
  }
  .input-field {
    @apply w-full bg-white/5 border border-white/10 rounded-xl px-4 py-3 text-white
           placeholder:text-gray-500 transition-all duration-200
           focus:outline-none focus:border-brand-primary focus:ring-2 focus:ring-brand-primary/20
           hover:border-white/20;
  }
  .badge {
    @apply inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium;
  }
  .badge-success { @apply bg-green-500/10 text-green-400 border border-green-500/20; }
  .badge-error { @apply bg-red-500/10 text-red-400 border border-red-500/20; }
  .badge-info { @apply bg-brand-primary/10 text-brand-primary border border-brand-primary/20; }
  .badge-warning { @apply bg-amber-500/10 text-amber-400 border border-amber-500/20; }
  .toast {
    @apply fixed top-6 right-6 z-50 max-w-sm glass-card p-4 animate-slideDown shadow-2xl shadow-black/40;
  }
  .skeleton {
    @apply bg-gradient-to-r from-white/5 via-white/10 to-white/5 bg-[length:200%_100%] animate-shimmer rounded-lg;
  }
  .section-header {
    @apply text-2xl font-bold text-white tracking-tight;
  }
  .gradient-text {
    @apply bg-gradient-to-r from-brand-primary to-brand-accent bg-clip-text text-transparent;
  }
}
"""


def _ensure_project_scaffold(root: Path) -> None:
    """Create the Vite + React + Tailwind scaffold if it doesn't exist."""
    root.mkdir(parents=True, exist_ok=True)

    pkg = root / "package.json"
    if not pkg.exists():
        pkg.write_text(
            json.dumps(
                {
                    "name": "frontend",
                    "private": True,
                    "version": "0.1.0",
                    "type": "module",
                    "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
                    "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
                    "devDependencies": {
                        "@vitejs/plugin-react": "^4.3.0",
                        "vite": "^5.4.0",
                        "tailwindcss": "^3.4.0",
                        "postcss": "^8.4.0",
                        "autoprefixer": "^10.4.0",
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    index_html = root / "index.html"
    if not index_html.exists():
        index_html.write_text(
            '<!DOCTYPE html>\n<html lang="en" class="dark">\n<head>\n  <meta charset="UTF-8" />\n'
            '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
            '  <link rel="preconnect" href="https://fonts.googleapis.com" />\n'
            '  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />\n'
            '  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" />\n'
            "  <title>App</title>\n</head>\n<body>\n"
            '  <div id="root"></div>\n'
            '  <script type="module" src="/src/main.jsx"></script>\n'
            "</body>\n</html>\n",
            encoding="utf-8",
        )

    vite_cfg = root / "vite.config.js"
    if not vite_cfg.exists():
        vite_cfg.write_text(
            "import { defineConfig } from 'vite';\n"
            "import react from '@vitejs/plugin-react';\n\n"
            "export default defineConfig({\n"
            "  plugins: [react()],\n"
            "  server: {\n"
            "    proxy: {\n"
            "      '/api': 'http://127.0.0.1:8001',\n"
            "    },\n"
            "  },\n"
            "});\n",
            encoding="utf-8",
        )

    tw_cfg = root / "tailwind.config.js"
    if not tw_cfg.exists():
        tw_cfg.write_text(_TAILWIND_CONFIG, encoding="utf-8")

    pc_cfg = root / "postcss.config.js"
    if not pc_cfg.exists():
        pc_cfg.write_text(_POSTCSS_CONFIG, encoding="utf-8")

    src = root / "src"
    src.mkdir(exist_ok=True)
    main_jsx = src / "main.jsx"
    if not main_jsx.exists():
        main_jsx.write_text(
            'import React from "react";\n'
            'import ReactDOM from "react-dom/client";\n'
            'import App from "./App";\n'
            'import "./styles.css";\n\n'
            'ReactDOM.createRoot(document.getElementById("root")).render(\n'
            "  <React.StrictMode><App /></React.StrictMode>\n);\n",
            encoding="utf-8",
        )
    styles_css = src / "styles.css"
    if not styles_css.exists():
        styles_css.write_text(_BASE_STYLES_CSS, encoding="utf-8")

    app_jsx = src / "App.jsx"
    if not app_jsx.exists():
        app_jsx.write_text(
            'export default function App() {\n  return <div className="min-h-screen p-8">App</div>;\n}\n',
            encoding="utf-8",
        )


def _merge_deps(pkg_path: Path, deps: list[str], dev_deps: list[str]) -> None:
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    pkg.setdefault("dependencies", {})
    pkg.setdefault("devDependencies", {})

    for item in deps:
        if "@" in item and not item.startswith("@"):
            name, version = item.split("@", 1)
            pkg["dependencies"][name] = version
        else:
            pkg["dependencies"].setdefault(item, "latest")

    for item in dev_deps:
        if "@" in item and not item.startswith("@"):
            name, version = item.split("@", 1)
            pkg["devDependencies"][name] = version
        else:
            pkg["devDependencies"].setdefault(item, "latest")

    pkg_path.write_text(json.dumps(pkg, indent=2), encoding="utf-8")


_SCAFFOLD_ONLY_FILES = {"package.json", "vite.config.js", "tailwind.config.js", "postcss.config.js"}


def _apply_changes(data: dict, root: Path) -> None:
    _ensure_project_scaffold(root)
    for file_change in data.get("files", []):
        rel = file_change["path"]
        if rel in _SCAFFOLD_ONLY_FILES:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_change["content"], encoding="utf-8")
    _merge_deps(root / "package.json", data.get("dependencies_add", []), data.get("dev_dependencies_add", []))
    _sanitize_package_json(root / "package.json")


def _sanitize_package_json(pkg_path: Path) -> None:
    """Ensure package.json has the fields Vite needs and nothing that breaks it."""
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    pkg.pop("bin", None)
    pkg["type"] = "module"
    pkg.setdefault("scripts", {})["build"] = "vite build"
    pkg.setdefault("scripts", {})["dev"] = "vite"
    pkg.setdefault("devDependencies", {}).setdefault("vite", "^5.4.0")
    pkg.setdefault("devDependencies", {}).setdefault("@vitejs/plugin-react", "^4.3.0")
    pkg_path.write_text(json.dumps(pkg, indent=2), encoding="utf-8")


def _npm_command() -> list[str] | None:
    npm_exe = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm_exe:
        return None
    if npm_exe.lower().endswith(".cmd"):
        return ["cmd", "/c", npm_exe]
    return [npm_exe]


def _run_frontend_checks(root: Path) -> tuple[bool, str]:
    npm_cmd = _npm_command()
    if not npm_cmd:
        return False, "npm not found. Install Node.js and ensure npm is available in PATH for this Python process."

    npx_exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx_exe:
        return False, "npx not found. Install Node.js >= 16."

    install = subprocess.run([*npm_cmd, "install"], cwd=root, capture_output=True, text=True, check=False)
    if install.returncode != 0:
        return False, f"npm install failed:\n{install.stderr}\n{install.stdout}"

    build = subprocess.run(
        [npx_exe, "vite", "build"], cwd=root, capture_output=True, text=True, check=False,
    )
    if build.returncode != 0:
        return False, f"npx vite build failed:\n{build.stderr}\n{build.stdout}"

    dist_index = root / "dist" / "index.html"
    if not dist_index.exists():
        return False, "vite build succeeded but dist/index.html was not created."
    return True, ""


def implement_frontend(
    story: Story,
    requirement_text: str = "",
    sibling_stories: list[Story] | None = None,
    on_progress: ProgressCallback = None,
    fix_context: str = "",
    replan: bool = False,
    workspace_root: Path | None = None,
) -> tuple[bool, str]:
    """Plan, patch, validate in scratch, and promote only on success."""
    ws = (workspace_root or WORKSPACE).resolve()
    frontend_dir = ws / "frontend"
    scope_file = ws / "scope.json"
    debug_dir = ws / "debug"
    contracts_dir = ws / "contracts"

    client = OpenAI(timeout=180)
    siblings = sibling_stories or []
    last_error = fix_context
    emit = on_progress or (lambda msg, detail=None: None)

    if fix_context:
        emit("Fix mode: injecting error context from previous failure")
    if replan:
        emit("Replan mode: PM has provided a revised implementation strategy")

    for attempt in range(MAX_RETRIES + 1):
        emit(f"Planning implementation (attempt {attempt + 1})...")
        try:
            plan = _plan_frontend_change(
                client, story, requirement_text, siblings,
                fix_context if (replan or attempt == 0) else "",
                frontend_dir, scope_file, contracts_dir, debug_dir,
            )
            emit("Plan ready", json.dumps(plan, indent=2))

            emit("Generating code...")
            patch = _generate_frontend_patch(
                client, story, requirement_text, siblings, plan, last_error,
                frontend_dir, scope_file, contracts_dir, debug_dir,
            )
            file_paths = [f.get("path", "?") for f in patch.get("files", [])]
            emit(f"Code generated: {', '.join(file_paths)}", _format_files_detail(patch))
        except ValueError as exc:
            return False, str(exc)

        emit("Validating with npm build...")
        scratch = create_scratch_copy(frontend_dir, "frontend_attempt")
        _apply_changes(patch, scratch)
        ok, output = _run_frontend_checks(scratch)
        if ok:
            emit("Validation passed")
            promote_scratch_copy(scratch, frontend_dir)
            scope = update_scope(
                scope_file,
                {
                    "id": story.id,
                    "title": story.title,
                    "owner": "frontend",
                    "summary": plan.get("summary", story.description),
                    "applied_at": datetime.utcnow().isoformat(),
                },
            )
            save_json(scope_file, scope)
            retry_note = f" (attempt {attempt + 1})" if attempt > 0 else ""
            return True, f"Patched {frontend_dir}. build passed{retry_note}. Scope entries: {len(scope['implemented_stories'])}"
        emit("Validation failed, retrying...", output)
        last_error = output

    return False, f"frontend checks failed after {MAX_RETRIES + 1} attempts:\n{last_error}"




