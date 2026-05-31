# BitGN ECOM Agent

An AI agent for the **BitGN Agent Challenge: E-commerce (ECOM)** benchmark — a
simulated commercial environment where an agent must handle the full customer
journey: product discovery, cart & checkout, payment-failure recovery, fraud
defense, merchant operations, shipping, returns, and support — all while
staying inside business and security constraints.

The agent is built on the [Hermes](https://github.com/NousResearch) CLI agent
loop, talks to each task's sandboxed ECOM VM through a single custom **MCP
server**, and is steered by a carefully engineered system prompt plus three
domain **skills** (fraud forensics, OCR receipt re-pricing, catalogue
reporting). Inference runs through [OpenRouter](https://openrouter.ai); the
agent uses a cheap default model and routes only a hard cluster of tasks to a
stronger model.

> **Security posture.** The agent is sandboxed to a single channel — the
> bitgn-ecom MCP server. Every built-in Hermes toolset (terminal, filesystem,
> web, browser, code execution, …) is disabled, so the model cannot touch the
> host, run code, or reach the network. See [Isolation model](#isolation-model).

---

## How it works (60-second tour)

```
                 ┌──────────────────────────────────────────────┐
                 │            BitGN Harness (cloud)             │
                 │   benchmark → trials → per-task ECOM VM       │
                 └───────────────▲───────────────┬──────────────┘
       start_run / submit_run    │               │  per-task harness_url (gRPC)
                                 │               │
        ┌────────────────────────┴───────┐       │
        │           main.py              │       │
        │  • drives the run loop         │       │
        │  • per-task model routing      │       │
        │  • parallel workers + scoring  │       │
        └────────────┬───────────────────┘       │
                     │ run_agent(...)             │
        ┌────────────▼───────────────────┐       │
        │        hermes_agent.py         │       │
        │  • bootstraps task context     │───────┤ (direct gRPC: tree, /bin/id…)
        │  • builds the prompt           │       │
        │  • spawns `hermes -z`          │       │
        │  • parses answer + refs        │       │
        └────────────┬───────────────────┘       │
                     │ subprocess (isolated HERMES_HOME)
        ┌────────────▼───────────────────┐       │
        │        hermes (LLM loop)       │       │
        │  model via OpenRouter          │       │
        │  ONLY tool channel: ▼          │       │
        └────────────┬───────────────────┘       │
                     │ MCP (stdio)                │
        ┌────────────▼───────────────────┐       │
        │      ecom_mcp_server.py        │───────┘ (gRPC to the ECOM VM)
        │  ecom_tree / read / exec / …   │
        └────────────────────────────────┘
```

A full architecture write-up is in **[ARCHITECTURE.md](ARCHITECTURE.md)**.
Every configuration knob is documented in **[CONFIGURATION.md](CONFIGURATION.md)**.

---

## Repository layout

```
bitgn-ecom-agent/
├── README.md                 # this file
├── ARCHITECTURE.md           # detailed design & data flow
├── CONFIGURATION.md          # every env var, config.yaml, skills
├── .env.example              # copy to .env and fill in your keys
├── .gitignore
├── requirements.txt
├── run.sh                    # portable launcher
└── agent/
    ├── main.py               # benchmark runner: run loop, model routing, scoring
    ├── hermes_agent.py       # bootstrap → prompt → hermes -z → parse answer/refs
    ├── ecom_mcp_server.py    # the bitgn-ecom MCP server (the ONLY tool channel)
    ├── formatters.py         # protobuf → shell-shaped text for the prompt
    ├── http_sync_client.py   # sync HTTP adapter for connectrpc
    ├── debug_logger.py       # thread-safe JSONL trace writer
    ├── env_loader.py         # minimal .env loader
    ├── prompts/
    │   ├── codex_preamble.md # "you live in a virtual workspace, use ecom_* tools"
    │   └── instructions.md   # the full operations playbook (system prompt)
    └── hermes_home/          # isolated Hermes home (no ~/.hermes fallback)
        ├── config.template.yaml  # rendered to config.yaml at runtime (no host paths)
        └── skills/
            ├── ecom-fraud-forensic/      # behavioral fraud detection
            ├── ecom-ocr-receipt/         # OCR receipt re-pricing
            └── ecom/ecom-catalogue-reporting/  # policy-driven product counts
```

---

## Prerequisites

- **Python 3.11+** (developed and tested on 3.14).
- An **OpenRouter** API key (for model inference via Hermes).
- A **BitGN** API key and access to the ECOM benchmark.
- The **`hermes`** CLI binary (installed via `pip install hermes-agent`).

---

## Installation

```bash
# 1. Clone and enter the project
git clone <your-fork-url> bitgn-ecom-agent
cd bitgn-ecom-agent

# 2. Create and activate a virtualenv
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install the BitGN SDK packages (published on buf.build, NOT PyPI)
pip install --extra-index-url https://buf.build/gen/python \
    bitgn-api-connectrpc-python \
    bitgn-api-grpc-python \
    bitgn-api-protocolbuffers-python

# 4. Install the runtime libraries + the Hermes CLI
pip install -r requirements.txt

# 5. Sanity-check that the hermes binary is on PATH
hermes --version
```

> The `hermes` binary must live in the **same environment** as the Python you
> run the agent with — `hermes_agent.py` prefers the `hermes` shipped next to
> the active interpreter. If it isn't found, set `HERMES_BIN` in `.env`.

---

## Configuration

```bash
cp .env.example .env
# then edit .env and set at least:
#   OPENROUTER_API_KEY=sk-or-v1-...
#   BITGN_API_KEY=...
#   BENCH_ID=bitgn/ecom1-dev
```

`main.py` loads `.env` from the project root at startup; Hermes inherits the
process environment, so `OPENROUTER_API_KEY` reaches the model provider with
no extra wiring. **Never commit `.env`** — it is git-ignored.

The full list of variables (models, workers, timeouts, advanced overrides) is
in **[CONFIGURATION.md](CONFIGURATION.md)**.

---

## Running

```bash
./run.sh                 # run every task in the benchmark
./run.sh t01             # run a single task
./run.sh t01 t04 t07     # run a subset

# inline overrides work too:
WORKERS=8 BENCH_ID=bitgn/ecom1-prod ./run.sh
```

Or invoke the runner directly:

```bash
source venv/bin/activate
cd agent
python -m main            # all tasks
python -m main t01        # one task
```

When the run finishes, `main.py` submits it for batch evaluation, polls until
the harness reports `RUN_STATE_EVALUATED`, and prints per-task scores plus a
`FINAL: NN.NN%` line. A full JSONL trace of every tool call, prompt, and model
response is written to `agent/DD-MM-YY-N.jsonl` (the primary debugging channel).

---

## Tuning the agent

You don't need to touch Python to change most behaviour:

| Want to…                                   | Where                                                |
|--------------------------------------------|------------------------------------------------------|
| Change the default / strong model          | `MODEL_ID`, `PRO_MODEL_ID` in `.env`                 |
| Change which tasks get the strong model     | `_PRO_ROUTE_RE` / `_route_model` in `agent/main.py`  |
| Change the agent's operating rules          | `agent/prompts/instructions.md`                      |
| Add or edit domain expertise                | `agent/hermes_home/skills/<skill>/SKILL.md`          |
| Enable/disable a Hermes toolset             | `agent/hermes_home/config.template.yaml`             |
| Change parallelism / timeouts               | `WORKERS`, `HERMES_TIMEOUT_SEC` in `.env`            |

See [CONFIGURATION.md](CONFIGURATION.md) and [ARCHITECTURE.md](ARCHITECTURE.md)
for details.

---

## Security & privacy notes for contributors

- **No secrets in the repo.** All credentials are read from `.env` (git-ignored).
  `config.template.yaml` contains placeholders only; the rendered `config.yaml`,
  Hermes' `auth.json`, session DBs, and logs are all git-ignored runtime
  artifacts — do not commit them.
- The agent is intentionally constrained to one MCP channel and cannot reach
  the host or the network outside the benchmark VM. If you re-enable a Hermes
  toolset in the config, you are widening that boundary — do so deliberately.

---

## License

Released under the [MIT License](LICENSE) — © 2026 Ivan. Note that **Hermes**,
the **BitGN SDK**, and the model providers are third-party components governed
by their own licenses and terms.
