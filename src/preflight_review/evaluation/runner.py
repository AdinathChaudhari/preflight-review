"""Run the eval sets (seeded / golden / clean) and write a results JSON file."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from preflight_review.errors import ConfigError, GitError, SchemaError
from preflight_review.evaluation.judge import deterministic_matches, judge_pairs
from preflight_review.evaluation.score import GoldenItem, Match, Scores, score
from preflight_review.evaluation.seeds import load_seeded_cases, materialise
from preflight_review.findings import Finding
from preflight_review.model import FakeBackend
from preflight_review.pipeline import ReviewOptions, run_review

EVAL_SETS = ("golden", "seeded", "clean")
_SAFE_OVERRIDES = {"mode", "budget_usd", "walkthrough", "show_pre_existing"}


def _no_gh(*args, **kwargs):
    return subprocess.CompletedProcess(args, 1, "", "gh disabled for eval")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def shared_clone(src: Path | str, dest: Path, rev: str) -> Path:
    """Clone `src` without touching it (shared objects, no checkout), then check out `rev`."""
    src = Path(src)
    proc = subprocess.run(
        ["git", "clone", "-q", "--shared", "--no-checkout", str(src), str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise GitError(f"clone of {src} failed: {proc.stderr.strip()}")
    _git(dest, "checkout", "-q", "-b", f"eval/{rev[:12]}", rev)
    return dest


def _make_backend(backend_name: str, case: str | None = None):
    if backend_name == "fake":
        path = os.environ.get("PREFLIGHT_FAKE_RESPONSES")
        if not path:
            return FakeBackend(default={"findings": [], "files_read_outside_packet": []})
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict) and "__per_case__" in data:
            inner = data["__per_case__"].get(case or "", {})
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "case.json"
                p.write_text(json.dumps(inner))
                return FakeBackend.from_file(p)
        return FakeBackend.from_file(path)
    if backend_name == "cli":
        from preflight_review.model.cli_backend import CliBackend

        return CliBackend()
    raise ConfigError(f"unknown backend: {backend_name!r} (expected 'cli' or 'fake')")


def _options(repo: Path, base: str, backend_name: str, overrides: dict | None, **kw) -> ReviewOptions:
    extra = {k: v for k, v in (overrides or {}).items() if k in _SAFE_OVERRIDES}
    unknown = set(overrides or {}) - _SAFE_OVERRIDES
    if unknown:
        raise ConfigError(f"unsupported eval config override(s): {sorted(unknown)}")
    return ReviewOptions(
        repo_root=repo, base=base, full=True, include_worktree=False,
        backend_name=backend_name, **{**kw, **extra},
    )


def _prefixed(findings: list[Finding], prefix: str) -> list[Finding]:
    out = []
    for i, f in enumerate(findings):
        sid = f.stable_id or f.run_id or f"{f.file}:{f.start_line}:{i}"
        out.append(dataclasses.replace(f, stable_id=f"{prefix}:{sid}"))
    return out


def _r(value):
    return round(value, 6) if isinstance(value, float) else value


def load_clean_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    if not path.is_file():
        return cases
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise SchemaError(f"{path}:{n}: invalid JSON: {exc}") from exc
        missing = {"repo_path", "base_sha", "head_sha"} - set(row)
        if missing:
            raise SchemaError(f"{path}:{n}: missing keys {sorted(missing)}")
        cases.append(row)
    return cases


def load_golden_rows(golden_dir: Path) -> list[dict]:
    rows: list[dict] = []
    if not golden_dir.is_dir():
        return rows
    for p in sorted(golden_dir.glob("*.jsonl")):
        for n, line in enumerate(p.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise SchemaError(f"{p}:{n}: invalid JSON: {exc}") from exc
            if row.get("is_real") is True and row.get("in_scope") is True and row.get("commit"):
                rows.append(row)
    return rows


def _golden_item(row: dict, i: int) -> GoldenItem:
    return GoldenItem(
        id=f"{row.get('repo')}#{row.get('pr')}:{i}",
        file=str(row.get("file") or ""),
        line_start=row.get("line_start"),
        line_end=row.get("line_end"),
        category=str(row.get("bug_class") or ""),
        severity=str(row.get("expected_severity") or ""),
        description=str(row.get("title") or row.get("bug_class") or ""),
        kind="real",
        is_real=True,
    )


def _match(goldens, findings, backend, judge, judge_models, cwd) -> tuple[list[Match], float | None]:
    if judge:
        return judge_pairs(goldens, findings, backend, list(judge_models), cwd)
    return deterministic_matches(goldens, findings), None


def run_eval(
    set_name: str,
    repo_root: Path | str,
    backend_name: str = "cli",
    backend=None,
    config_overrides: dict | None = None,
    judge: bool = True,
    judge_models=("sonnet", "opus"),
    out_dir: Path | str = "eval/results",
    repo_path: Path | str | None = None,
    cases_dir: Path | str | None = None,
    now=datetime.now,
) -> dict:
    if set_name not in EVAL_SETS:
        raise ConfigError(f"unknown eval set: {set_name!r} (expected one of {EVAL_SETS})")
    repo_root = Path(repo_root)
    cases: list[dict] = []
    skipped: list[str] = []
    all_goldens: list[GoldenItem] = []
    all_findings: list[Finding] = []
    all_matches: list[Match] = []
    agreements: list[float] = []
    cost = 0.0

    with tempfile.TemporaryDirectory(prefix="preflight-eval-") as tmp:
        tmp_path = Path(tmp)
        if set_name == "seeded":
            seeded = load_seeded_cases(cases_dir or repo_root / "eval" / "seeded" / "cases")
            for case in seeded:
                repo = materialise(case, tmp_path)
                be = backend or _make_backend(backend_name, case.name)
                opts = _options(repo, "main", backend_name, config_overrides)
                run, _ = run_review(opts, backend=be, gh_runner=_no_gh)
                found = _prefixed(list(run.policy.report) + list(run.policy.unverified), case.name)
                matches, agree = _match(case.expected, found, be, judge, judge_models, repo)
                if agree is not None:
                    agreements.append(agree)
                case_scores = score(case.expected, found, matches)
                cases.append({
                    "name": case.name, "n_findings": case_scores.n_findings,
                    "matched": case_scores.matched_ids,
                    "false_positives": case_scores.false_positive_ids,
                    "cost_usd": _r(run.cost_usd), "outcome": run.outcome,
                })
                cost += run.cost_usd
                all_goldens += case.expected
                all_findings += found
                all_matches += matches

        elif set_name == "golden":
            if repo_path is None:
                raise ConfigError("golden set needs --repo-path (a checkout of the reviewed repo)")
            rows = load_golden_rows(repo_root / "eval" / "golden")
            if not Path(repo_path).exists():
                print(f"eval: repo path {repo_path} does not exist; skipping {len(rows)} golden rows")
                skipped += [f"{r.get('repo')}#{r.get('pr')}: repo path missing" for r in rows]
                rows = []
            by_commit: dict[str, list[tuple[int, dict]]] = {}
            for i, row in enumerate(rows):
                by_commit.setdefault(row["commit"], []).append((i, row))
            for commit, items in by_commit.items():
                goldens = [dataclasses.replace(_golden_item(r, i), id=f"{commit[:8]}:{i}") for i, r in items]
                try:
                    clone = shared_clone(repo_path, tmp_path / f"golden-{commit[:12]}", commit)
                    _git(clone, "rev-parse", "--verify", f"{commit}^")
                except GitError as exc:
                    print(f"eval: skipping {commit[:12]}: {exc}")
                    skipped.append(f"{commit[:12]}: {exc}")
                    continue
                be = backend or _make_backend(backend_name)
                opts = _options(clone, f"{commit}^", backend_name, config_overrides, walkthrough=False)
                run, _ = run_review(opts, backend=be, gh_runner=_no_gh)
                found = _prefixed(list(run.policy.report) + list(run.policy.unverified), commit[:8])
                matches, agree = _match(goldens, found, be, judge, judge_models, clone)
                if agree is not None:
                    agreements.append(agree)
                cs = score(goldens, found, matches)
                cases.append({
                    "name": commit[:12], "n_findings": cs.n_findings, "matched": cs.matched_ids,
                    "false_positives": cs.false_positive_ids,
                    "cost_usd": _r(run.cost_usd), "outcome": run.outcome,
                })
                cost += run.cost_usd
                all_goldens += goldens
                all_findings += found
                all_matches += matches

        else:  # clean
            for n, row in enumerate(load_clean_cases(repo_root / "eval" / "clean" / "cases.jsonl")):
                src = Path(row["repo_path"])
                if not src.exists():
                    print(f"eval: repo path {src} does not exist; skipping")
                    skipped.append(f"{src}: repo path missing")
                    continue
                try:
                    clone = shared_clone(src, tmp_path / f"clean-{n}", row["head_sha"])
                except GitError as exc:
                    print(f"eval: skipping {src}: {exc}")
                    skipped.append(f"{src}: {exc}")
                    continue
                be = backend or _make_backend(backend_name)
                opts = _options(clone, row["base_sha"], backend_name, config_overrides, walkthrough=False)
                run, _ = run_review(opts, backend=be, gh_runner=_no_gh)
                confirmed = len(run.policy.report)
                cases.append({
                    "name": row.get("note") or f"{src.name}:{row['head_sha'][:8]}",
                    "n_findings": confirmed, "confirmed": confirmed, "target": 0,
                    "cost_usd": _r(run.cost_usd), "outcome": run.outcome,
                })
                cost += run.cost_usd
                all_findings += list(run.policy.report)

    if set_name == "clean":
        sc = Scores(None, None, None, None, len(all_findings), 0)
    else:
        sc = score(all_goldens, all_findings, all_matches)

    stamp = now()
    out = Path(out_dir)
    if not out.is_absolute():
        out = repo_root / out
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stamp.strftime('%Y%m%d-%H%M%S')}-{set_name}.json"
    result = {
        "set": set_name,
        "generated_at": stamp.isoformat(timespec="seconds"),
        "backend": backend_name if backend is None else getattr(backend, "name", backend_name),
        "judge": ",".join(judge_models) if judge else "none",
        "precision": _r(sc.precision),
        "ep_recall": _r(sc.ep_recall),
        "snr": _r(sc.snr),
        "snr_note": sc.snr_note,
        "f1": _r(sc.f1),
        "n_findings": sc.n_findings,
        "n_goldens": sc.n_goldens,
        "per_category_recall": {k: _r(v) for k, v in sc.per_category_recall.items()},
        "agreement_rate": _r(sum(agreements) / len(agreements)) if agreements else None,
        "cases": cases,
        "skipped": skipped,
        "cost_usd": _r(cost),
        "path": str(path),
    }
    path.write_text(json.dumps(result, indent=2) + "\n")
    return result
