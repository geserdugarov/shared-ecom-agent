# Architecture

This document explains how the BitGN ECOM agent is put together, how a single
task flows through it end to end, and why the key design decisions were made.

---

## 1. The benchmark contract

The BitGN harness drives everything:

1. A **benchmark** (`BENCH_ID`, e.g. `bitgn/ecom1-dev`) is a fixed set of
   **tasks**. Each task is a natural-language instruction (shopper, checkout,
   merchant, support, fraud, …).
2. You **start a run**, which yields one **trial** per task.
3. For each trial you call `start_trial`, which returns the task instruction
   and a per-task **`harness_url`** — a gRPC endpoint for that task's isolated
   **ECOM VM** (the simulated e-commerce OS: warehouse data, customer records,
   policy book, and a `/bin/sql` database).
4. Your agent does its work against that VM and submits an **outcome** (an
   `Outcome` enum plus a final answer and a set of grounding references).
5. You `end_trial`, then `submit_run`. Scores are produced by a batch
   evaluator and become available only once the run reaches
   `RUN_STATE_EVALUATED`.

The agent never sees ground-truth scores during a run — it must be correct
blind. This shapes a lot of the prompt engineering (see §6).

---

## 2. Components

| File | Responsibility |
|------|----------------|
| `agent/main.py` | Benchmark runner. Connects to the harness, starts the run, fans tasks out across worker threads, routes each task to a model, calls `run_agent`, submits the run, and polls for evaluated scores. |
| `agent/hermes_agent.py` | The agent core. Bootstraps task context directly from the VM, assembles the prompt, renders the Hermes config, spawns `hermes -z`, then parses Hermes' final answer and the grounding refs and submits the outcome. Exports `run_agent(...)`. |
| `agent/ecom_mcp_server.py` | The **bitgn-ecom MCP server** — the single tool channel exposed to the model. Wraps the ECOM VM's gRPC API as MCP tools (`ecom_tree`, `ecom_read`, `ecom_exec`, …) and tracks grounding references. |
| `agent/formatters.py` | Renders the VM's protobuf responses into shell-shaped text (`tree -L 2 /`, `cat -n file`, …) for the bootstrap section of the prompt. |
| `agent/prompts/` | `codex_preamble.md` (workspace framing + tool rules) and `instructions.md` (the full operations playbook). |
| `agent/hermes_home/` | An isolated Hermes home directory: `config.template.yaml` (rendered at runtime) and `skills/`. |
| `agent/http_sync_client.py` | A small sync HTTP client adapter used by connectrpc. |
| `agent/debug_logger.py` | Thread-safe JSONL trace writer (one record per event, atomic under a lock). |
| `agent/env_loader.py` | Minimal `.env` loader (`KEY=VALUE`, no dependencies). |

---

## 3. End-to-end launch sequence

```
run.sh
  └─ python -m main  (CWD = agent/)
       ├─ load_dotenv(.env)                      # OPENROUTER_API_KEY etc. into os.environ
       ├─ HarnessServiceClientSync(BITGN_HOST)
       ├─ start_run(name, BENCH_ID, BITGN_API_KEY)
       └─ for each trial  (ThreadPoolExecutor, WORKERS threads):
            ├─ start_trial → (task_id, instruction, harness_url)
            ├─ _route_model(instruction, MODEL_ID) → flash | pro
            └─ run_agent(model, harness_url, instruction, …)        [hermes_agent.py]
                 ├─ EcomRuntimeClientSync(harness_url)              # direct gRPC to the VM
                 ├─ _bootstrap(vm): tree /, tree /docs, AGENTS.MD, /bin/date, /bin/id
                 ├─ _build_full_prompt(preamble + instructions + bootstrap + task)
                 ├─ _render_hermes_config()                         # fill config.template.yaml
                 ├─ subprocess: hermes -z PROMPT
                 │     └─ hermes LLM loop (model via OpenRouter)
                 │          └─ MCP stdio → ecom_mcp_server.py → gRPC → the VM
                 ├─ parse hermes stdout → final answer + Outcome
                 ├─ read VAULT_MCP_REFS → grounding refs
                 └─ vm.answer(AnswerRequest(outcome, answer, refs))
            (end_trial)
       ├─ submit_run(force=True)
       └─ _collect_scores_after_submit(): poll get_run until RUN_STATE_EVALUATED
```

Two distinct connections reach each VM:

- **`hermes_agent.py` → VM (direct gRPC):** only for the read-only bootstrap and
  for submitting the final answer. This is *the agent runner's* connection.
- **`hermes` → MCP server → VM (gRPC):** every tool action the *model* takes
  during the loop. The model has no other way to touch the VM.

---

## 4. Bootstrap: priming the model

Before invoking the LLM, `_bootstrap` makes a handful of direct, read-only VM
calls and renders them as shell-shaped text (via `formatters.py`):

- `tree /` and `tree /docs` — the workspace and policy-book layout,
- `cat /AGENTS.MD` — environment-provided agent notes,
- `/bin/date` and `/bin/id` — the VM's notion of "now" and the current customer.

This `<bootstrap-output>` block is embedded in the prompt so the model starts
oriented instead of spending turns rediscovering the filesystem. Truncation is
always explicitly marked so the model never silently goes blind on a large
directory or file.

---

## 5. Prompt construction

The full prompt handed to `hermes -z` is, in order:

1. **`codex_preamble.md`** — frames the world: "you are inside a virtual ECOM
   workspace reachable ONLY through `ecom_*` MCP tools; shell commands do not
   affect the workspace; SQL goes through `ecom_exec(path="/bin/sql", …)`;
   identity comes from `/bin/id`, time from `/bin/date`." It also explains the
   crucial `ecom_read` vs `ecom_read_silent` distinction (see §6).
2. **`instructions.md`** — the operations playbook: a security fast-path
   (prompt-injection / privilege-escalation detection → `OUTCOME_DENIED_SECURITY`),
   date arithmetic rules, aggregation/counting discipline, outcome selection,
   and the grounding-refs protocol.
3. **`<bootstrap-output>`** — the rendered VM context from §4.
4. The **task instruction** and any optional `HINT`.

Hermes is run with `--ignore-rules` so it does **not** auto-inject its own
`AGENTS.md` / `SOUL.md`; our prompt is the sole source of behaviour.

---

## 6. Grounding references (the core scoring mechanism)

The evaluator scores not just the *answer* but the **set of files the agent
cited as evidence** ("grounding refs"). Over- or under-citing both hurt. The
MCP server makes this ergonomic:

- **`ecom_read(path)`** reads a file **and tracks** `path` as a grounding ref.
- **`ecom_read_silent(path)`** reads the same file but does **not** track it.

Use `ecom_read` for files that justify the answer; use `ecom_read_silent` for
files you only inspect for computation/comparison — e.g. "except X" entities
you read just to know what to exclude, or an attack-target basket you verify
ownership of before denying a request.

The server flushes the tracked set to a per-task JSON file (`VAULT_MCP_REFS`).
After Hermes exits, `hermes_agent.py` reads that file, applies a few
sanitisers, and passes the refs into `vm.answer(...)`. Trusting the model's
*own* `ecom_read` choices — rather than citing every file the server happened
to touch — was the single biggest accuracy lever in development (it eliminated
rampant over-citation).

---

## 7. Model routing

The default model (`MODEL_ID`, e.g. `deepseek/deepseek-v4-flash`) is cheap and
handles the bulk of tasks — shopper, checkout, support, OCR — well. But it hits
a capability wall on a specific cluster: counting / resolving SKUs, stock
availability, and fraud-archive tasks, where it tends to ignore the
"track only the qualifying SKU" refs protocol and miscount.

`main.py:_route_model` therefore routes **only those task types** to a stronger
model (`PRO_MODEL_ID`, e.g. `deepseek/deepseek-v4-pro`), matched by an
instruction-pattern regex (`_PRO_ROUTE_RE`): phrases like "how many", "resolve
this product request", "in stock", "Risk Ops", "fraud", etc.

The routing is by **task type**, never by `task_id`, to avoid overfitting to a
specific benchmark split. Roughly a quarter of tasks route up, so cost lands
between full-cheap and full-strong. Every routing decision is logged
(`model_routed` event) so you can audit it after a run.

---

## 8. The MCP server and its tools

`ecom_mcp_server.py` is a `FastMCP("bitgn-ecom")` stdio server. It connects to
the VM identified by `VAULT_HARNESS_URL` and exposes:

| Tool | Purpose |
|------|---------|
| `ecom_tree(root, level)` | Directory tree of the workspace. |
| `ecom_list(path)` | List a directory. |
| `ecom_read(path, …)` | Read a file **and track it** as a grounding ref. |
| `ecom_read_silent(path, …)` | Read a file **without** tracking it. |
| `ecom_write(path, content)` | Write a file in the VM. |
| `ecom_delete(path)` | Delete a file in the VM. |
| `ecom_find(...)` | Find files by name/glob. |
| `ecom_search(pattern, root, limit)` | Content search (ripgrep-style). |
| `ecom_stat(path)` | File metadata. |
| `ecom_exec(path, args, stdin)` | Run a VM binary — notably `/bin/sql` (catalogue/inventory/orders DB), `/bin/date`, `/bin/id`. |
| `ecom_context()` | The VM's current time/identity context. |

The server receives its per-task wiring through environment variables that
Hermes forwards to the stdio subprocess:

- `VAULT_HARNESS_URL` — the task's VM gRPC endpoint,
- `VAULT_MCP_REFS` — path to the JSON file where grounding refs are flushed,
- `VAULT_MCP_LOG` — optional stderr-mirroring log file,
- `VAULT_MCP_DIAG` — optional verbose diagnostics.

---

## 9. Isolation model

The agent is deliberately confined to **one channel** — the bitgn-ecom MCP
server. `config.template.yaml`:

- sets `platform_toolsets.cli` to just `[bitgn-ecom, skills]`,
- lists every other built-in Hermes toolset under `disabled_toolsets`
  (terminal, file, web, browser, code_execution, vision/video/image/tts,
  memory, todo, delegation, messaging, …),
- blanks the `auxiliary` LLM providers (vision/web_extract/compression) so the
  agent can't fan out to other models,
- disables the model-catalog HTTP fetch (`model_catalog.enabled: false`).

`hermes_agent.py` pins `HERMES_HOME` to the bundled directory so Hermes never
falls back to the user's `~/.hermes` (which could carry unrelated MCP servers,
skills, or rules). The net effect: the model cannot read/write the host
filesystem, shell out, run code, or reach the network outside the task VM.

The MCP server command/args are **rendered at runtime** from
`config.template.yaml` into `config.yaml` (`_render_hermes_config`), so the
committed repo contains no machine-specific absolute paths and the agent is
portable across machines.

---

## 10. Skills

Skills are on-demand domain playbooks the model can load mid-task (it's the one
built-in Hermes toolset left enabled). Each `SKILL.md` has a tightly scoped
`description` that tells the model exactly when to activate it — and, just as
importantly, when **not** to:

- **`ecom-fraud-forensic`** — behavioral fraud detection over archived payment
  records. Activates only on literal "fraud" / "Risk Ops" / "chargeback"
  triggers. Ships a helper script (`scripts/fraud_compute.py`).
- **`ecom-ocr-receipt`** — parse an OCR'd paper receipt from `/uploads/` and
  re-price its line items against today's catalog. The real trick is
  glob-recovering OCR-corrupted SKUs, not reading the text.
- **`ecom-catalogue-reporting`** — count products by kind following dated
  policy docs in `/docs/policy-updates/`.

The narrow activation rules matter: a skill firing on the wrong task type is a
net negative, so the descriptions err heavily toward non-activation.

---

## 11. Observability

`debug_logger.py` writes a JSONL trace to `agent/DD-MM-YY-N.jsonl`, one record
per event: `run_started`, `trial_started`, `model_routed`, `agent_started`,
`hermes_prompt` (the full prompt), `tool_result`, `agent_completed`,
`trial_finished`, `eval_*`, `run_finished`. This is the primary channel for
debugging failures and auditing model-routing and refs decisions. The MCP
server additionally appends to `agent/ecom_mcp.log`. Both are git-ignored.

---

## 12. Design lineage (for the curious)

The agent evolved through a long series of experiments. The headline lessons
baked into this release:

- **Trust the model's own `ecom_read` refs**, not the server's full read-set —
  over-citation was the dominant failure mode.
- **Hybrid model routing** beats single-model on cost-adjusted accuracy: cheap
  by default, strong only on the count/resolve/fraud cluster.
- **Tight skill activation rules** prevent skills from misfiring.
- **Strict isolation** (one MCP channel) is both a safety property and a
  scoring aid — it keeps the model from "cheating" with host tools that don't
  actually affect the VM.
