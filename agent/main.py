"""Benchmark runner for the BitGN ECOM agent.

Connects to the BitGN harness, starts a run, and drives one trial per task.
Each trial hands control to ``run_agent`` (Hermes CLI via OpenRouter), which
talks to the task's isolated ECOM VM through the bitgn-ecom MCP server. The
runner then submits the run and polls for the evaluated scores.

Parallelism is controlled by the ``WORKERS`` env var; per-task model routing
is handled by ``_route_model`` below.
"""

import os
import re
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from env_loader import load_dotenv
from debug_logger import JsonlDebugLogger

# .env lives at the project root (one level above agent/). Allow an explicit
# override via ECOM_ENV_FILE, then fall back to the project root, then CWD.
_ENV_PATH = os.getenv("ECOM_ENV_FILE") or str(
    Path(__file__).resolve().parents[1] / ".env"
)
load_dotenv(_ENV_PATH)

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    EvalPolicy,
    GetBenchmarkRequest,
    GetRunRequest,
    RunState,
    StartRunRequest,
    StartTrialRequest,
    StatusRequest,
    SubmitRunRequest,
)
from connectrpc.errors import ConnectError

from hermes_agent import run_agent
from http_sync_client import HttpxSyncClient


CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"
CLI_YELLOW = "\x1B[33m"


_print_lock = threading.Lock()


def _tprint(tid: str, msg: str) -> None:
    with _print_lock:
        for line in msg.splitlines() or [""]:
            print(f"[{tid}] {line}")


# 037: hybrid model routing. flash is the cheap default and handles
# shopper/checkout/support/OCR well, but on prod (035/036 analysis) it hits a
# capability wall on count / resolve / availability and fraud-archive tasks: it
# ignores the documented "track only qualifying SKU" refs protocol and miscounts.
# v4-pro solves those (dev 95.84%). So route ONLY those task TYPES (by instruction
# pattern) to pro; everything else stays on flash. Cost lands between full-flash
# and full-pro because only ~1/4 of tasks route up. The default model and the pro
# model are both env-overridable.
_PRO_ROUTE_RE = re.compile(
    r"how many|resolve this product request|matching sku|# of matching|"
    r"such product exist|does .{0,40}\bexist\b|do you have .{0,80}in stock|"
    r"\bin stock in\b|answer only with sku|\bsku only\b|number only|count me products|"
    r"same-day units|units available|physically on hand|units physically|"
    r"matching products|risk ops|archive export|two-year-old archive|"
    r"impossible travel|\bfraud\b|suspicious",
    re.IGNORECASE,
)


def _route_model(instruction: str, default_model: str) -> str:
    """Pick pro for the count/resolve/availability/fraud cluster, else default (flash)."""
    pro = os.getenv("PRO_MODEL_ID") or "deepseek/deepseek-v4-pro"
    if _PRO_ROUTE_RE.search(instruction or ""):
        return pro
    return default_model


def _process_trial(
    client: HarnessServiceClientSync,
    trial_id: str,
    model_id: str,
    debug_logger: JsonlDebugLogger,
    task_filter: list[str],
    parallel: bool,
) -> tuple[str, float | None, list[str]]:
    trial = client.start_trial(StartTrialRequest(trial_id=trial_id))

    if task_filter and trial.task_id not in task_filter:
        return (None, None, [])  # signal "skipped" so main loop excludes from FINAL

    header = (
        f"{'=' * 30} Starting task: {trial.task_id} {'=' * 30}\n"
        f"{CLI_BLUE}{trial.instruction}{CLI_CLR}\n{'-' * 80}"
    )
    if parallel:
        _tprint(trial.task_id, header)
    else:
        print(header)

    debug_logger.log(
        "trial_started",
        task_id=trial.task_id,
        trial_id=trial.trial_id,
        harness_url=trial.harness_url,
        instruction=trial.instruction,
    )

    routed_model = _route_model(trial.instruction, model_id)
    debug_logger.log(
        "model_routed",
        task_id=trial.task_id,
        trial_id=trial.trial_id,
        default_model=model_id,
        routed_model=routed_model,
        to_pro=routed_model != model_id,
    )
    try:
        run_agent(
            routed_model,
            trial.harness_url,
            trial.instruction,
            task_id=trial.task_id,
            debug_logger=debug_logger,
        )
    except Exception as exc:
        if parallel:
            _tprint(trial.task_id, f"crashed: {exc}")
        else:
            print(exc)
        debug_logger.log(
            "trial_failed",
            task_id=trial.task_id,
            trial_id=trial.trial_id,
            error=str(exc),
        )

    result = client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
    # New harness API (2026-05-28): per-trial scores aren't available here —
    # they only land after submit_run() and the run reaches RUN_STATE_EVALUATED.
    # We log what end_trial returns for diagnostics; the real scores are
    # fetched via get_run() in _collect_scores_after_submit().
    detail = list(result.score_detail)

    debug_logger.log(
        "trial_finished",
        task_id=trial.task_id,
        trial_id=trial.trial_id,
        state=result.state,
        score=result.score,
        score_detail=detail,
    )

    body = f"\n{CLI_BLUE}Trial finished (state={result.state}); awaiting batch eval{CLI_CLR}\n"
    if parallel:
        _tprint(trial.task_id, body)
    else:
        print(body)

    return (trial.task_id, trial.trial_id, detail)


def _collect_scores_after_submit(
    client: HarnessServiceClientSync,
    run_id: str,
    debug_logger: JsonlDebugLogger,
    poll_interval: float = 5.0,
    max_wait_sec: float = 600.0,
) -> dict[str, tuple[float, list[str]]]:
    """Poll `get_run` until the run is fully evaluated, then return
    {task_id: (score, score_detail)} for every trial."""
    import time
    deadline = time.time() + max_wait_sec
    last_state = None
    while True:
        try:
            run = client.get_run(GetRunRequest(run_id=run_id))
        except ConnectError as exc:
            print(f"{CLI_YELLOW}get_run failed ({exc.code}: {exc.message}); retrying...{CLI_CLR}")
            time.sleep(poll_interval)
            if time.time() > deadline:
                debug_logger.log("eval_poll_timeout", run_id=run_id, last_state=str(last_state))
                return {}
            continue

        if run.state != last_state:
            last_state = run.state
            print(f"{CLI_BLUE}Run state: {RunState.Name(run.state)} (trials done: {run.stats.done_count}/{run.stats.done_count + run.stats.running_count + run.stats.new_count + run.stats.error_count}){CLI_CLR}")
            debug_logger.log(
                "eval_state_change",
                run_id=run_id,
                state=RunState.Name(run.state),
                done=run.stats.done_count,
                running=run.stats.running_count,
                error=run.stats.error_count,
            )

        if run.state == RunState.RUN_STATE_EVALUATED:
            results: dict[str, tuple[float, list[str]]] = {}
            for th in run.trials:
                results[th.task_id] = (th.score, [])  # TrialHead has no score_detail
            # Pull detail via GetTrial for each — but that's N extra calls;
            # we already logged detail from end_trial earlier so skip.
            debug_logger.log(
                "eval_finished",
                run_id=run_id,
                overall_score=run.score,
                n_trials=len(run.trials),
            )
            return results

        if time.time() > deadline:
            debug_logger.log(
                "eval_poll_timeout",
                run_id=run_id,
                last_state=RunState.Name(run.state),
            )
            print(f"{CLI_RED}Eval polling timed out after {max_wait_sec}s in state {RunState.Name(run.state)}{CLI_CLR}")
            return {}

        time.sleep(poll_interval)


def main() -> None:
    bitgn_url = (
        os.getenv("BITGN_HOST")
        or os.getenv("BENCHMARK_HOST")
        or "https://api.bitgn.com"
    )
    bitgn_api_key = os.getenv("BITGN_API_KEY") or ""
    bench_id = os.getenv("BENCH_ID") or os.getenv("BENCHMARK_ID") or "bitgn/ecom1-dev"
    model_id = os.getenv("MODEL_ID") or "deepseek/deepseek-v4-pro"
    workers = max(1, int(os.getenv("WORKERS", "4")))
    parallel = workers > 1

    task_filter = sys.argv[1:]
    scores: list[tuple[str, float]] = []
    scores_lock = threading.Lock()
    debug_logger = JsonlDebugLogger()
    debug_logger.log(
        "run_started",
        benchmark_id=bench_id,
        model_id=model_id,
        task_filter=task_filter,
        workers=workers,
    )
    print(f"Debug logs: {debug_logger.path.name}  (workers={workers})")

    try:
        client = HarnessServiceClientSync(bitgn_url, http_client=HttpxSyncClient())
        print("Connecting to BitGN", client.status(StatusRequest()))
        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=bench_id))
        print(
            f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
            f"with {len(res.tasks)} tasks.\n{CLI_GREEN}{res.description}{CLI_CLR}"
        )

        run = client.start_run(
            StartRunRequest(
                name=os.getenv("RUN_NAME", "bitgn-ecom-agent"),
                benchmark_id=bench_id,
                api_key=bitgn_api_key,
            )
        )

        executed_task_ids: list[str] = []
        try:
            if parallel:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [
                        pool.submit(
                            _process_trial,
                            client,
                            tid,
                            model_id,
                            debug_logger,
                            task_filter,
                            True,
                        )
                        for tid in run.trial_ids
                    ]
                    for fut in as_completed(futures):
                        task_id, _trial_id, _ = fut.result()
                        if task_id:
                            with scores_lock:
                                executed_task_ids.append(task_id)
            else:
                for trial_id in run.trial_ids:
                    task_id, _trial_id, _ = _process_trial(
                        client, trial_id, model_id, debug_logger, task_filter, False
                    )
                    if task_id:
                        executed_task_ids.append(task_id)
        finally:
            print(f"\n{CLI_BLUE}Submitting run for batch evaluation...{CLI_CLR}")
            client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))

        # New harness API: scores arrive only after submit_run completes eval.
        # Poll get_run() until RUN_STATE_EVALUATED, then populate `scores`.
        eval_results = _collect_scores_after_submit(client, run.run_id, debug_logger)
        for tid in executed_task_ids:
            if tid in eval_results:
                score, _detail = eval_results[tid]
                scores.append((tid, score))

    except ConnectError as exc:
        print(f"{exc.code}: {exc.message}")
        debug_logger.log("run_error", error_code=str(exc.code), error_message=exc.message)
    except KeyboardInterrupt:
        print(f"{CLI_RED}Interrupted{CLI_CLR}")
        debug_logger.log("run_interrupted")

    if scores:
        scores.sort(key=lambda x: x[0])
        for task_id, score in scores:
            style = CLI_GREEN if score == 1 else CLI_RED
            print(f"{task_id}: {style}{score:0.2f}{CLI_CLR}")

        total = sum(score for _, score in scores) / len(scores) * 100.0
        print(f"FINAL: {total:0.2f}%")
        debug_logger.log("run_finished", final_score=total, scores=scores)
    else:
        debug_logger.log("run_finished", final_score=None, scores=scores)
    debug_logger.close()


if __name__ == "__main__":
    main()
