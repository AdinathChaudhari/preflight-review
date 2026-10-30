"""Command-line entry point (argparse)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from preflight_review import __version__
from preflight_review.errors import PreflightError
from preflight_review.model.backend import claude_version

PROG = "preflight-review"


def prompts_sha() -> str:
    d = Path(__file__).parent / "prompts"
    h = hashlib.sha1()
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            h.update(p.read_bytes())
    return h.hexdigest()


def _not_implemented(args: argparse.Namespace) -> int:
    print("not implemented yet", file=sys.stderr)
    return 2


def _review(args: argparse.Namespace) -> int:
    from preflight_review.pipeline import ReviewOptions, run_review, summary_line

    opts = ReviewOptions(
        repo_root=Path.cwd(),
        base=args.base,
        full=args.full,
        incremental=args.incremental,
        mode=args.mode,
        backend_name=args.backend,
        include_worktree=not args.committed_only,
        show_pre_existing=args.show_pre_existing,
        budget_usd=args.budget_usd,
        walkthrough=False if args.no_walkthrough else None,
        json_stdout=args.json,
    )
    run, paths = run_review(opts)
    print(summary_line(run, paths), file=sys.stderr if args.json else sys.stdout)
    if args.json:
        print(json.dumps({
            "md": str(paths["md"]),
            "json": str(paths["json"]),
            "outcome": run.outcome,
            "confirmed": run.reused_confirmed if run.reused else len(run.policy.report),
            "unverified": run.reused_unverified if run.reused else len(run.policy.unverified),
            "round": run.round,
            "reused": run.reused,
        }))
    if run.outcome != "complete":
        for reason in run.incomplete_reasons:
            print(f"preflight: incomplete: {reason}", file=sys.stderr)
    return 0 if run.outcome == "complete" else 1


def _init(args: argparse.Namespace) -> int:
    from preflight_review.init_templates import init_repo

    result = init_repo(
        Path.cwd(), args.force, with_ast_rules=args.with_ast_rules
    )
    print(result.message)
    return 0


def _install_hook(args: argparse.Namespace) -> int:
    from preflight_review.hookinstall import install_hook

    print(install_hook(Path.cwd(), force=args.force).message)
    return 0


def _uninstall_hook(args: argparse.Namespace) -> int:
    from preflight_review.hookinstall import uninstall_hook

    print(uninstall_hook(Path.cwd()))
    return 0


def _respond(args: argparse.Namespace) -> int:
    from preflight_review.respond import respond

    result = respond(
        Path.cwd(), args.finding_id, args.label, args.reason, learn=args.learn
    )
    print(result.message)
    return 0


def _learn(args: argparse.Namespace) -> int:
    from preflight_review.learn import (
        apply_proposals,
        format_proposals_yaml,
        learn,
        list_learnings,
        propose_path_instructions,
    )

    if args.apply and not args.propose:
        raise PreflightError("--apply only works with --propose")
    if args.propose:
        if not list_learnings(Path.cwd()):
            print("no learnings to propose from")
            return 0
        if args.backend == "fake":
            import os

            from preflight_review.model import FakeBackend

            path = os.environ.get("PREFLIGHT_FAKE_RESPONSES")
            backend = (
                FakeBackend.from_file(path)
                if path
                else FakeBackend(default={"proposals": []})
            )
        else:
            from preflight_review.model.cli_backend import CliBackend

            backend = CliBackend()
        proposals = propose_path_instructions(Path.cwd(), backend, model=args.model)
        if not proposals:
            print("no path_instructions proposed")
            return 0
        print(format_proposals_yaml(proposals))
        if args.apply:
            target = apply_proposals(Path.cwd(), proposals)
            print(
                f"appended {len(proposals)} path_instructions to {target} "
                "(comments in config.yaml are not preserved)"
            )
        else:
            print("paste this under path_instructions: in .preflight/config.yaml")
        return 0
    if args.list:
        items = list_learnings(Path.cwd())
        if not items:
            print("no learnings")
        for l in items:
            print(f"{l.id}  {l.path_glob}  {l.text}")
        return 0
    if not args.text:
        raise PreflightError("learn needs text (or --list)")
    l = learn(Path.cwd(), args.text, args.path, args.from_finding)
    print(f"{l.id} added: {l.path_glob}  {l.text}")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    from preflight_review.doctor import doctor_ok, format_checks, run_doctor

    checks = run_doctor(Path.cwd())
    print(format_checks(checks))
    return 0 if doctor_ok(checks) else 1


def _import_coderabbit(args: argparse.Namespace) -> int:
    import re

    from preflight_review.importers.coderabbit import import_coderabbit

    if not args.repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        raise PreflightError("--repo must look like OWNER/NAME")
    res = import_coderabbit(
        args.repo,
        Path(args.out),
        from_fixture=Path(args.from_fixture) if args.from_fixture else None,
    )
    print(
        f"imported {res.n_items} CodeRabbit comments ({res.n_skipped} skipped)"
        f" -> {res.history_path}, {res.golden_path}"
    )
    return 0


def _eval(args: argparse.Namespace) -> int:
    from preflight_review.evaluation.runner import run_eval

    if args.compare_bundled:
        print("compare-bundled: not implemented in v1")
        return 0
    root = Path.cwd()
    if not (root / "eval").is_dir():
        pkg_root = Path(__file__).resolve().parents[2]
        if (pkg_root / "eval").is_dir():
            root = pkg_root
    models = [m.strip() for m in args.judge_models.split(",") if m.strip()]
    res = run_eval(
        args.eval_set,
        root,
        backend_name=args.backend,
        judge=not args.no_judge,
        judge_models=models,
        out_dir=args.out,
        repo_path=args.repo_path,
        cases_dir=args.cases_dir,
    )

    def fmt(v):
        return "n/a" if v is None else f"{v:.3f}"

    print(
        f"eval {args.eval_set}: precision={fmt(res['precision'])} "
        f"ep_recall={fmt(res['ep_recall'])} snr={fmt(res['snr'])} "
        f"f1={fmt(res['f1'])} \u2192 {res['path']}"
    )
    return 0


def _version(args: argparse.Namespace) -> int:
    cv = claude_version()
    sha = prompts_sha()
    if args.json:
        print(json.dumps({"version": __version__, "claude_code": cv, "prompts_sha": sha}))
    else:
        print(f"{PROG} {__version__} (claude {cv}, prompts {sha[:12]})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="Local pre-push AI code reviewer (report-only)."
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_, description=help_, prog=f"{PROG} {name}")
        p.set_defaults(func=_not_implemented)
        return p

    p = add("review", "Run the review pipeline and write a report.")
    p.add_argument("--base", help="base ref to diff against (default from config)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true", help="review the whole branch diff")
    g.add_argument("--incremental", action="store_true", help="review only new commits")
    p.add_argument("--mode", choices=["quick", "standard", "deep"])
    p.add_argument("--json", action="store_true", help="print machine-readable output")
    p.add_argument("--show-pre-existing", action="store_true")
    p.add_argument("--backend", choices=["cli", "fake"])
    p.add_argument("--committed-only", action="store_true", help="ignore uncommitted changes")
    p.add_argument("--no-walkthrough", action="store_true")
    p.add_argument("--budget-usd", type=float)
    p.set_defaults(func=_review)

    p = add("init", "Create .preflight/config.yaml in this repo.")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.add_argument(
        "--with-ast-rules",
        action="store_true",
        help="also install the bundled ast-grep rules (no shell=True, no unsafe yaml.load)",
    )
    p.set_defaults(func=_init)
    p = add("doctor", "Check the environment (git, claude, tools).")
    p.set_defaults(func=_doctor)
    p = add("install-hook", "Install the pre-push git hook.")
    p.add_argument("--force", action="store_true", help="overwrite a chained hook backup")
    p.set_defaults(func=_install_hook)
    p = add("uninstall-hook", "Remove the pre-push git hook.")
    p.set_defaults(func=_uninstall_hook)

    p = add("respond", "Record an author label for a finding.")
    p.add_argument("finding_id", help="F<n> from the latest report, or a 12-hex stable id")
    p.add_argument("label", choices=["fixed", "wrong", "intentional", "didnt_matter"])
    p.add_argument("reason", nargs="?")
    p.add_argument("--learn", action="store_true", help="also append a learning")
    p.set_defaults(func=_respond)

    p = add("learn", "Append a learning for future reviews.")
    p.add_argument("text", nargs="?")
    p.add_argument("--path", default="**", help="glob the learning applies to")
    p.add_argument("--from", dest="from_finding", help="finding id the learning comes from")
    p.add_argument("--list", action="store_true", help="list existing learnings")
    p.add_argument("--propose", action="store_true", help="ask a model to turn learnings into path_instructions")
    p.add_argument("--apply", action="store_true", help="with --propose: append proposals to .preflight/config.yaml")
    p.add_argument("--backend", choices=["cli", "fake"], default="cli")
    p.add_argument("--model", default="sonnet", help="model for --propose (default sonnet)")
    p.set_defaults(func=_learn)

    p = add("import-coderabbit", "Import CodeRabbit history for evals.")
    p.add_argument("--repo", help="GitHub repo as OWNER/NAME, e.g. example-org/example-repo")
    p.add_argument("--out", default=".preflight", help="output directory (default .preflight)")
    p.add_argument("--from-fixture", help="read pulls.json/pull_comments.json from DIR or a file inside it, instead of gh")
    p.set_defaults(func=_import_coderabbit)

    p = add("eval", "Replay the eval sets and score results.")
    p.add_argument("--set", dest="eval_set", choices=["golden", "seeded", "clean"], required=True)
    p.add_argument("--repo-path", help="checkout of the reviewed repo (golden set)")
    p.add_argument("--backend", choices=["cli", "fake"], default="cli")
    p.add_argument("--no-judge", action="store_true", help="match deterministically, no model judge")
    p.add_argument("--judge-models", default="sonnet,opus", help="comma-separated judge models")
    p.add_argument("--out", default="eval/results", help="results directory (default eval/results)")
    p.add_argument("--cases-dir", help="seeded cases directory (default eval/seeded/cases)")
    p.add_argument("--compare-bundled", action="store_true")
    p.set_defaults(func=_eval)

    p = sub.add_parser(
        "version", help="Print version information.", prog=f"{PROG} version"
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except PreflightError as exc:
        print(f"{PROG}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
