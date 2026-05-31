# Configuration

Everything you can tune, where it lives, and what it does. Most behaviour is
configurable without editing Python.

---

## 1. Environment variables (`.env`)

`main.py` loads `.env` from the project root at startup (override the location
with `ECOM_ENV_FILE`). Copy `.env.example` to `.env` and fill it in. Variables
already present in the real environment win over `.env` (it uses `setdefault`).

### Required

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | — | OpenRouter key used by Hermes for inference. Flows from `.env` → `os.environ` → the hermes subprocess automatically. Get one at <https://openrouter.ai/keys>. |
| `BITGN_API_KEY` | — | BitGN key, passed to `start_run` so the run can be submitted for scoring. |
| `BENCH_ID` | `bitgn/ecom1-dev` | Which benchmark to run (e.g. `bitgn/ecom1-dev`, `bitgn/ecom1-prod`). Also accepts the legacy `BENCHMARK_ID`. |

### Model selection

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_ID` | `deepseek/deepseek-v4-pro` (in code) — set to `deepseek/deepseek-v4-flash` in `.env.example` | Default model for most tasks. |
| `PRO_MODEL_ID` | `deepseek/deepseek-v4-pro` | Stronger model used only for the count/resolve/availability/fraud cluster (see `_route_model`). |

Use any model slug your OpenRouter account can access. The default `MODEL_ID`
in code is `deepseek-v4-pro`; the shipped `.env.example` sets it to the cheaper
`deepseek-v4-flash` with `pro` reserved for the hard cluster — the recommended
cost/accuracy setup.

### Run tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKERS` | `4` | Parallel trial workers. `1` = sequential. |
| `RUN_NAME` | `bitgn-ecom-agent` | Label shown for your run in the BitGN dashboard. |
| `HERMES_TIMEOUT_SEC` | `900` | Wall-clock limit for a single `hermes -z` invocation. |
| `HINT` | _(empty)_ | Optional extra guidance appended to the prompt (benchmark convention). |

### Hermes / inference internals

| Variable | Default | Description |
|----------|---------|-------------|
| `HERMES_BIN` | resolved from venv, then PATH | Explicit path to the `hermes` binary. |
| `HERMES_PROVIDER` | `openrouter` | Inference provider Hermes uses. |
| `HERMES_MAX_TURNS` | `90` | Max agent turns inside one Hermes run. |
| `HERMES_HOME` | `agent/hermes_home` | Isolated Hermes home. Override only if you relocate it. |

### Advanced overrides (rarely needed)

| Variable | Default | Description |
|----------|---------|-------------|
| `ECOM_ENV_FILE` | `<project-root>/.env` | Path to the `.env` file to load. |
| `ECOM_MCP_PYTHON` | the running interpreter (`sys.executable`) | Interpreter used to launch the MCP server. |
| `ECOM_MCP_SERVER` | `agent/ecom_mcp_server.py` | Path to the MCP server script. |
| `BITGN_HOST` | `https://api.bitgn.com` | Harness host. Also accepts `BENCHMARK_HOST`. |
| `AUTO_DISCOVERY` | `1` | Run the read-only bootstrap before the LLM (`0` to skip). |
| `GROUNDING_REFS` | `1` | Enable grounding-ref tracking/submission. |
| `CODEX_STRIP_EXCLUSIONS` | `0` | Strip "except X" paths from the refs set (off by default). |
| `VAULT_MCP_DIAG` | _(unset)_ | Set to enable verbose MCP server diagnostics in the log. |

> **`VAULT_HARNESS_URL`, `VAULT_MCP_REFS`, `VAULT_MCP_LOG`** are set
> automatically per task by `hermes_agent.py` and forwarded to the MCP server.
> You never set these by hand.

---

## 2. Model routing (`agent/main.py`)

Which tasks get `PRO_MODEL_ID` is decided by a single regex over the task
instruction:

```python
_PRO_ROUTE_RE = re.compile(
    r"how many|resolve this product request|matching sku|# of matching|"
    r"such product exist|does .{0,40}\bexist\b|do you have .{0,80}in stock|"
    r"\bin stock in\b|answer only with sku|\bsku only\b|number only|...|"
    r"impossible travel|\bfraud\b|suspicious",
    re.IGNORECASE,
)
```

To change the routing policy, edit `_PRO_ROUTE_RE` / `_route_model`. Keep
patterns matched on **task type**, not specific task IDs, to avoid overfitting.
Inspect the `model_routed` events in the JSONL log to verify routing after a run.

---

## 3. Hermes config (`agent/hermes_home/config.template.yaml`)

This template is rendered to `config.yaml` at runtime (the two `__ECOM_MCP_*__`
placeholders are filled with the interpreter and server paths). Edit the
**template**, not the generated `config.yaml` (which is git-ignored and
overwritten every run).

Key sections:

- **`model.default` / `model.provider`** — fallback model & provider. Per-task
  model is overridden by `main.py` via `HERMES_INFERENCE_MODEL`, so this
  mostly matters as a backstop.
- **`agent.max_turns`** — turn budget inside Hermes.
- **`agent.disabled_toolsets`** — every built-in Hermes toolset to switch off.
  Remove an entry to re-enable that toolset (this widens the agent's access —
  do it deliberately).
- **`platform_toolsets.cli`** — the toolsets actually wired in. Currently
  `[bitgn-ecom, skills]`. The MCP server name (`bitgn-ecom`) must appear here.
- **`mcp_servers.bitgn-ecom`** — how the MCP server is launched and which
  per-task env vars are forwarded to it.
- **`auxiliary.*`** — blanked so Hermes makes no auxiliary LLM calls.
- **`model_catalog.enabled: false`** — no network catalog fetch during runs.

---

## 4. Prompts (`agent/prompts/`)

- **`codex_preamble.md`** — workspace framing and tool rules (`ecom_*` only,
  `ecom_read` vs `ecom_read_silent`, SQL via `/bin/sql`). Loaded as
  `CODEX_PREAMBLE`.
- **`instructions.md`** — the full operations playbook: security fast-path,
  date arithmetic, aggregation discipline, outcome selection, and the
  grounding-refs protocol. Loaded as `INSTRUCTIONS`.

Both are plain Markdown read by `prompts/__init__.py`. Edit them to change the
agent's operating rules — no code change required.

---

## 5. Skills (`agent/hermes_home/skills/`)

Each skill is a directory with a `SKILL.md` (YAML frontmatter + body) plus any
helper scripts. The frontmatter `description` is what the model uses to decide
whether to activate the skill, so it must state both **when to activate** and
**when not to** in concrete terms.

```
skills/
├── ecom-fraud-forensic/
│   ├── SKILL.md
│   └── scripts/fraud_compute.py
├── ecom-ocr-receipt/
│   └── SKILL.md
└── ecom/ecom-catalogue-reporting/
    └── SKILL.md
```

To add a skill: create `skills/<name>/SKILL.md` with a tightly scoped
`description`. To disable one: remove its directory (or narrow its
`description` so it never matches). Keep activation rules narrow — a skill that
fires on the wrong task type hurts more than it helps.

---

## 6. What is and isn't committed

**Tracked** (safe to share): all of `agent/*.py`, `prompts/`,
`hermes_home/config.template.yaml`, `hermes_home/skills/`, the docs, and
`.env.example`.

**Git-ignored** (runtime/secret — never commit): `.env`, the rendered
`hermes_home/config.yaml`, Hermes' `auth.json` (it caches your live API keys!),
`state.db*`, `sessions/`, `logs/`, model caches, the per-run `*.jsonl` traces,
and `ecom_mcp.log`. See `.gitignore` for the full list.
