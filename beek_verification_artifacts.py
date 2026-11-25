"""Save a Kivi verification executed on a Beek-built model.

Add this module beside your notebooks in ``last-beek/``. It does NOT
regenerate a C3 model or change Kivi's sources. It calls your existing
``verify_kivi_model`` and retains generated PROMELA, raw process output,
Kivi's formatted counterexamples, and a parser-compatible raw-trail.txt.

The ``raw-trail.txt`` format is a reconstructed, runner-compatible report;
``counterexample.native.txt`` preserves Kivi's unmodified str_failures().

Kivi's util and verifier operator are process-global; do not use this
function concurrently against a single Kivi checkout.
"""

from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import importlib
import io
import json
import re
import shutil
import sys
import traceback
from time import perf_counter_ns
from beek_metrics import seconds
from typing import Callable, Any


PROMELA_NAMES = ("main.pml", "config.pml", "event.pml", "intentsCheck.pml")
_ERROR_RE = re.compile(r"\berrors\s*:\s*(\d+)\b", re.IGNORECASE)
_TRUNCATION_RE = re.compile(
    r"max search depth too small|VECTORSZ too small|out of memory|"
    r"search (?:was )?truncated|Search not completed", re.IGNORECASE
)
_CHECKED_RE = re.compile(r"(?m)^\s*Working on the intents:\s*(.*?)\s*$")


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, content):
        for stream in self.streams:
            stream.write(content)
        return len(content)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


def _known_violating_intents(native_text: str) -> list[str]:
    """Only infer a name when Kivi's trace contains a recognizable assertion."""
    names = []
    if "[Scheduler - no_feasible_node]" in native_text or "No feasible node!" in native_text:
        names.append("pod_always_schedulable")
    if "[checkBalanceNode]" in native_text or "The pods are unbalanced on Node!" in native_text:
        names.append("pod_balance_across_node")
    return names


def _checked_intents(model: dict, operator_stdout: str) -> list[str]:
    found = _CHECKED_RE.findall(operator_stdout)
    if found:
        return found[-1].split()
    # The default schedulability intent is injected by Kivi itself.
    names = ["pod_always_schedulable"]
    for entry in model.get("intents", []):
        if entry.get("name") == "checkBalanceNode":
            names.insert(0, "pod_balance_across_node")
    return names


def _render_raw_trail(native_text: str, checked: list[str], violating: list[str],
                      failure_count: int, status: str) -> str:
    """Make the human-readable Kivi trace compatible with the existing parser.

    This is *not* a byte-for-byte capture of the old CLI. The Kivi-native
    failure section is preserved unmodified inside it and separately on disk.
    """
    header = "Working on the intents: " + " ".join(checked) + "\n"
    if failure_count:
        contents = native_text.rstrip("\n") + "\n"
        ending = (
            "\nSummary:\n"
            f"    {failure_count} failure(s) found!\n"
            f"    Violating intent(s): {' '.join(violating) if violating else 'unclassified'}\n"
        )
    elif status == "PASS":
        contents = "No failure is found!\n"
        ending = "\nSummary:\n    0 failure(s) found!\n"
    else:
        contents = "Verification inconclusive: no confirmed counterexample.\n"
        ending = "\nSummary:\n    Verification incomplete.\n"
    return header + contents + ending


def _classify(observations: list[dict], failures: int) -> tuple[str, int | None, str]:
    search = [p for p in observations if p["stage"] == "pan_search"]
    if not search:
        return "INCOMPLETE", None, "No pan search captured"
    pre = [p for p in observations if p["stage"] in ("spin", "gcc")]
    if any(not p["success"] for p in pre):
        return "INCOMPLETE", None, "SPIN or gcc subprocess failed"
    result = search[-1]
    matches = _ERROR_RE.findall(result["stdout"])
    if not matches:
        return "INCOMPLETE", None, "pan did not report an error count"
    error_count = int(matches[-1])
    truncated = bool(_TRUNCATION_RE.search(result["stdout"] + "\n" + result["stderr"]))
    if failures and error_count > 0:
        return "FAIL", error_count, "Confirmed Kivi counterexample and pan error"
    if not failures and error_count == 0 and result["success"] and not truncated:
        return "PASS", 0, "Completed pan search with zero errors"
    return "INCOMPLETE", error_count, "Kivi failure count and pan output do not establish a result"


def _snapshot_pan_files(directory: Path) -> dict[str, tuple[int, int]]:
    if not directory.is_dir():
        return {}
    snapshot = {}
    for file in directory.glob("pan*"):
        if file.is_file():
            s = file.stat()
            snapshot[file.name] = (s.st_mtime_ns, s.st_size)
    return snapshot


def _copy_directory(source: Path, target: Path) -> bool:
    if not source.exists():
        return False
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target.with_suffix(source.suffix))
    else:
        return False
    return True


def verify_and_save(
    model: dict,
    kivi_root: str | Path,
    output_dir: str | Path,
    *,
    verify_fn: Callable,
    case_id: str = "beek_case",
    overwrite: bool = False,
    copy_pan_files: bool = True,
):
    """Run Beek's Kivi verifier and save reproducible artifacts.

    Parameters
    ----------
    verify_fn:
        Your existing ``verify_kivi_model(model, kivi_root, case_id=...)``.
    output_dir:
        Case-specific path under your Beek project, e.g.
        ``Path('beek_runs') / 'C3_B6' / 'h1z1'``.

    Returns
    -------
    (verifier, files): ``files`` maps logical names to output Path objects.

    Raises
    ------
    Any exception raised by the underlying verifier, after persisting
    available diagnostic output to the requested directory.
    """
    whole_start_ns = perf_counter_ns()
    if not isinstance(model, dict):
        raise TypeError("model must be the dictionary returned by build_kivi_model")
    root = Path(kivi_root).resolve(strict=True)
    out = Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()) and not overwrite:
        raise FileExistsError(f"Artifact directory is not empty: {out}. Use a new path or overwrite=True.")
    out.mkdir(parents=True, exist_ok=True)
    if not (root / "kivi" / "verifier_operator.py").is_file():
        raise FileNotFoundError(root / "kivi" / "verifier_operator.py")

    model_json = json.dumps(model, indent=2, ensure_ascii=False) + "\n"
    (out / "model.json").write_text(model_json, encoding="utf-8")
    digest = sha256(json.dumps(model, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    started = datetime.now(timezone.utc).isoformat()

    kivi_module_dir = str(root / "kivi")
    if kivi_module_dir not in sys.path:
        sys.path.insert(0, kivi_module_dir)
    original_argv = sys.argv[:]
    try:
        # util.py parses sys.argv at import time. This is a compatibility shim.
        sys.argv = ["kivi_runner.py", "-c", "c3", "-o"]
        util = importlib.import_module("util")
        operator_module = importlib.import_module("verifier_operator")
    finally:
        sys.argv = original_argv

    original_generate = util.generate_dir
    original_run = operator_module.run_script
    generated_paths = {}
    calls: list[dict] = []

    def record_paths(*args, **kwargs):
        result = original_generate(*args, **kwargs)
        generated_paths["pml_dir"] = str(Path(result[0]).resolve())
        generated_paths["result_dir"] = str(Path(result[1]).resolve())
        return result

    def record_subprocess(command, *args, **kwargs):
        process_start_ns = perf_counter_ns()
        try:
            result = original_run(command, *args, **kwargs)
        finally:
            process_elapsed_s = seconds(perf_counter_ns() - process_start_ns)
        argv = [str(arg) for arg in command] if not isinstance(command, str) else [command]
        executable = Path(argv[0]).name if argv else ""
        if executable == "spin":
            stage = "spin"
        elif executable == "gcc":
            stage = "gcc"
        elif executable == "pan":
            stage = "pan_replay" if any(a == "-r" or a.startswith("-r") for a in argv[1:]) else "pan_search"
        else:
            stage = "other"
        if stage != "other" and isinstance(result, (tuple, list)) and len(result) >= 3:
            calls.append({
                "stage": stage,
                "command": argv,
                "success": bool(result[0]),
                "duration_s": process_elapsed_s,
                "stdout": _as_text(result[1]),
                "stderr": _as_text(result[2]),
            })
        return result

    before_pan = _snapshot_pan_files(root / "bin")
    stdout_buffer, stderr_buffer = io.StringIO(), io.StringIO()
    verifier = None
    error = None
    util.generate_dir = record_paths
    operator_module.run_script = record_subprocess
    verifier_start_ns = perf_counter_ns()
    try:
        with redirect_stdout(_Tee(sys.stdout, stdout_buffer)), redirect_stderr(_Tee(sys.stderr, stderr_buffer)):
            verifier = verify_fn(model, root, case_id=case_id)
    except Exception as exc:
        error = exc
        stderr_buffer.write(traceback.format_exc())
    finally:
        verifier_wall_time_s = seconds(perf_counter_ns() - verifier_start_ns)
        persistence_start_ns = perf_counter_ns()
        # Always restore Kivi's process-global hooks.
        operator_module.run_script = original_run
        util.generate_dir = original_generate

    (out / "kivi.stdout.txt").write_text(stdout_buffer.getvalue(), encoding="utf-8")
    (out / "kivi.stderr.txt").write_text(stderr_buffer.getvalue(), encoding="utf-8")
    process_dir = out / "processes"
    process_dir.mkdir(exist_ok=True)
    for index, call in enumerate(calls, 1):
        stem = f"{index:02d}_{call['stage']}"
        (process_dir / f"{stem}.stdout.txt").write_text(call["stdout"], encoding="utf-8")
        (process_dir / f"{stem}.stderr.txt").write_text(call["stderr"], encoding="utf-8")
    (out / "subprocesses.json").write_text(
        json.dumps([{"stage": p["stage"], "command": p["command"],
                    "success": p["success"], "duration_s": p["duration_s"]}
                    for p in calls], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    saved_paths = {"model": out / "model.json", "stdout": out / "kivi.stdout.txt"}
    pml_path = Path(generated_paths.get("pml_dir", root / "temp" / case_id / "3"))
    result_path = generated_paths.get("result_dir")
    if pml_path.is_dir():
        # Save everything generated under Kivi's model directory (incl. PROMELA).
        _copy_directory(pml_path, out / "promela")
        saved_paths["promela"] = out / "promela"
    if result_path and Path(result_path).exists():
        _copy_directory(Path(result_path), out / "kivi_results")
        saved_paths["kivi_results"] = out / "kivi_results"

    if copy_pan_files:
        pan_target = out / "spin_generated"
        for name, stats in _snapshot_pan_files(root / "bin").items():
            if before_pan.get(name) != stats:
                pan_target.mkdir(exist_ok=True)
                shutil.copy2(root / "bin" / name, pan_target / name)
        if pan_target.exists():
            saved_paths["spin_generated"] = pan_target

    failures = len(getattr(verifier, "failures", [])) if verifier is not None else 0
    classification, pan_errors, explanation = _classify(calls, failures)
    if error is not None:
        classification = "ERROR"
        explanation = f"{type(error).__name__}: {error}"
    native_text = verifier.str_failures() if verifier is not None and failures else ""
    (out / "counterexample.native.txt").write_text(native_text, encoding="utf-8")
    native_logs = []
    if verifier is not None:
        for idx, fail in enumerate(getattr(verifier, "failures", []), 1):
            if len(fail) > 1:
                native_logs.append({"failure_number": idx, "raw_log": _as_text(fail[1]),
                                    "details": _as_text(fail[2]) if len(fail) > 2 else ""})
    (out / "failures.json").write_text(json.dumps(native_logs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    checked = _checked_intents(model, stdout_buffer.getvalue())
    violating = _known_violating_intents(native_text)
    (out / "raw-trail.txt").write_text(
        _render_raw_trail(native_text, checked, violating, failures, classification), encoding="utf-8"
    )
    saved_paths["raw_trail"] = out / "raw-trail.txt"
    saved_paths["native_counterexample"] = out / "counterexample.native.txt"

    subprocess_times: dict[str, float] = {}
    subprocess_counts: dict[str, int] = {}
    for call in calls:
        st = call["stage"]
        subprocess_times[st] = round(subprocess_times.get(st, 0.0) + call["duration_s"], 9)
        subprocess_counts[st] = subprocess_counts.get(st, 0) + 1
    metadata = {
        "schema": "beek.verification-artifacts.v1",
        "case_id": case_id,
        "metrics": {
            "verify_fn_wall_time_s": verifier_wall_time_s,
            "subprocess_wall_time_s_by_stage": subprocess_times,
            "subprocess_count_by_stage": subprocess_counts,
            "artifact_persistence_wall_time_s": seconds(perf_counter_ns() - persistence_start_ns),
            "total_wall_time_s": seconds(perf_counter_ns() - whole_start_ns),
            "timing_note": "Subprocess wall durations are contained within verify_fn time; do not sum them as independent stages",
        },
        "started_at_utc": started,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_sha256": digest,
        "kivi_root": str(root),
        "result": classification,
        "pan_errors": pan_errors,
        "reason": explanation,
        "kivi_failure_count": failures,
        "checked_intents": checked,
        "violating_intents_inferred_from_trace": violating,
        "generated_paths": generated_paths,
        "raw_trail_note": "Reconstructed Kivi-runner-like text; native verifier output is in counterexample.native.txt and failures.json",
        "files": {name: str(path) for name, path in saved_paths.items()},
    }
    (out / "verification.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    saved_paths["metadata"] = out / "verification.json"
    if error is not None:
        raise error
    return verifier, saved_paths
