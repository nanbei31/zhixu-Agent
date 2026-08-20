"""CLI entry point and interactive REPL — mirrors cli.ts."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from .agent import Agent
from .benchmark import load_suite, run_benchmark, validate_suite
from .bugsinpy import load_catalog as load_bugsinpy_catalog
from .bugsinpy import prepare_case as prepare_bugsinpy_case
from .bugsinpy import run_case as run_bugsinpy_case
from .ci import CiFixConfig, run_ci_fix_workflow
from .ci.report import write_json_report
from .ci.storage import get_data_root, list_recent_runs, usage_summary
from .ui import print_welcome, print_user_prompt, print_error, print_info, print_plan_for_approval, print_plan_approval_options
from .session import load_session, get_latest_session_id
from .memory import list_memories
from .pybughive import load_catalog as load_pybughive_catalog
from .pybughive import prepare_case as prepare_pybughive_case
from .pybughive import run_case as run_pybughive_case
from .skills import discover_skills, get_skill_by_name
from .tools import tool_definitions
from .workspace_policy import WorkspacePolicy


def _ci_tool_definitions(workspace_policy: WorkspacePolicy) -> list[dict]:
    """Expose only policy-approved file tools; Skill loading stays in workflow."""
    return [
        tool for tool in tool_definitions
        if (
            tool["name"] in workspace_policy.allowed_agent_tools
            and tool["name"] != "skill"
        )
    ]


def _print_recent_runs() -> None:
    rows = list_recent_runs()
    if not rows:
        print(f"No AutoCI runs recorded yet. Data directory: {get_data_root()}")
        return
    print("Recent AutoCI runs")
    for row in rows:
        started = (row.get("started_at") or "unknown")[:19]
        print(
            f"  {row['run_id']}  {row['status'].upper():6}  {started}  "
            f"{row.get('model') or '-'}  {row['attempt_count']} attempts  "
            f"{row['changed_file_count']} files  ${row['estimated_cost_usd']:.4f}  "
            f"{row['total_duration_seconds']:.2f}s"
        )
        print(f"    {row['artifact_dir']}")


def _print_usage_summary() -> None:
    summary = usage_summary()
    runs = int(summary.get("run_count") or 0)
    passed = int(summary.get("passed_count") or 0)
    rate = (passed / runs * 100) if runs else 0.0
    print("AutoCI usage")
    print(f"  Runs: {runs} ({passed} passed, {rate:.1f}% success)")
    print(
        f"  Tokens: {int(summary.get('input_tokens') or 0)} input / "
        f"{int(summary.get('output_tokens') or 0)} output / "
        f"{int(summary.get('cache_read_tokens') or 0)} cache read / "
        f"{int(summary.get('cache_creation_tokens') or 0)} cache write"
    )
    print(f"  Estimated cost: ${float(summary.get('estimated_cost_usd') or 0):.6f}")
    print(f"  Average duration: {float(summary.get('avg_duration_seconds') or 0):.2f}s")
    print(f"  Average attempts: {float(summary.get('avg_attempts') or 0):.2f}")
    print(f"  Data directory: {get_data_root()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mini-claude",
        description="Mini Claude Code — a minimal coding agent",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true", help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations (for CI)")
    parser.add_argument("--auto", action="store_true", help="Auto Mode: LLM classifier judges each action")
    parser.add_argument("--thinking", action="store_true", help="Enable extended thinking")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--web", action="store_true", help="Start the local Web workbench")
    parser.add_argument("--web-host", default="127.0.0.1", help="Web server host (default: 127.0.0.1)")
    parser.add_argument("--web-port", type=int, default=8765, help="Web server port (default: 8765)")
    parser.add_argument("--no-open-browser", action="store_true", help="Do not open the Web workbench automatically")
    parser.add_argument("--fix-ci", action="store_true", help="Run tests and repair pytest failures")
    parser.add_argument("--benchmark", action="store_true", help="Run the pytest repair benchmark")
    parser.add_argument("--benchmark-suite", help="Path to a benchmark suite YAML file")
    parser.add_argument("--benchmark-case", action="append", default=[], help="Run a benchmark case ID (repeatable)")
    parser.add_argument("--benchmark-category", action="append", default=[], help="Run a benchmark category (repeatable)")
    parser.add_argument("--benchmark-limit", type=int, help="Run only the first N selected benchmark cases")
    parser.add_argument("--benchmark-repetitions", type=int, default=1, help="Repeat each benchmark case N times")
    parser.add_argument("--benchmark-output", help="Directory for benchmark reports and case artifacts")
    parser.add_argument("--benchmark-validate", action="store_true", help="Validate benchmark fixtures without a model")
    parser.add_argument("--pybughive", action="store_true", help="Run one real-world PyBugHive repair case")
    parser.add_argument("--pybughive-dataset", help="Path to pybughive_current.json")
    parser.add_argument("--pybughive-case", help="Case ID such as black-2254")
    parser.add_argument("--pybughive-workspaces", help="Directory for prepared project checkouts")
    parser.add_argument("--pybughive-test-command", help="Override the dataset test command")
    parser.add_argument("--pybughive-output", help="Directory for PyBugHive run artifacts")
    parser.add_argument("--pybughive-list", action="store_true", help="List available cases without calling a model")
    parser.add_argument("--pybughive-prepare-only", action="store_true", help="Prepare the buggy checkout without running tests or a model")
    parser.add_argument("--bugsinpy", action="store_true", help="Run one real-world BugsInPy repair case")
    parser.add_argument("--bugsinpy-root", help="Path to the cloned BugsInPy repository")
    parser.add_argument("--bugsinpy-case", help="Case ID such as black-1")
    parser.add_argument("--bugsinpy-workspaces", help="Directory for prepared BugsInPy checkouts")
    parser.add_argument(
        "--bugsinpy-localization",
        choices=("end-to-end", "oracle"),
        default="end-to-end",
        help="Fault localization mode (default: end-to-end)",
    )
    parser.add_argument("--bugsinpy-test-command", help="Override the triggering test command")
    parser.add_argument("--bugsinpy-full-test-command", help="Optional full regression command")
    parser.add_argument("--bugsinpy-output", help="Directory for BugsInPy run artifacts")
    parser.add_argument("--bugsinpy-list", action="store_true", help="List available BugsInPy cases")
    parser.add_argument("--bugsinpy-prepare-only", action="store_true", help="Prepare a BugsInPy checkout without a model")
    parser.add_argument(
        "--test-command",
        default="python -m pytest -q",
        help="Test command used by --fix-ci",
    )
    parser.add_argument(
        "--max-fix-attempts",
        type=int,
        default=2,
        help="Maximum repair attempts for --fix-ci",
    )
    parser.add_argument(
        "--repair-skill",
        default="pytest-repair",
        help="Skill used to structure each AutoCI repair attempt",
    )
    parser.add_argument(
        "--ci-timeout",
        type=float,
        default=300.0,
        help="Per-test-run timeout in seconds for --fix-ci",
    )
    parser.add_argument("--ci-report", help="Write the AutoCI-Fix report as JSON")
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="File or directory to prioritize during CI repair (repeatable)",
    )
    parser.add_argument(
        "--allowed-path",
        action="append",
        default=[],
        help="Further narrow project writable paths (repeatable)",
    )
    parser.add_argument(
        "--no-isolate",
        dest="isolate",
        action="store_false",
        help="Run AutoCI-Fix in the current checkout instead of a Git worktree",
    )
    parser.add_argument(
        "--keep-failed-worktree",
        action="store_true",
        help="Preserve the isolated worktree when repair fails",
    )
    parser.add_argument(
        "--artifacts-dir",
        help="Base directory for run reports, patches, logs, and SQLite index",
    )
    parser.set_defaults(isolate=True)
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    args = parser.parse_args()
    if sum(bool(value) for value in (
        args.web, args.fix_ci, args.benchmark, args.pybughive, args.bugsinpy,
    )) > 1:
        parser.error(
            "--web, --fix-ci, --benchmark, --pybughive, and --bugsinpy are mutually exclusive"
        )
    if args.web and args.prompt:
        parser.error("--web does not accept a positional prompt")
    if args.web_port < 1 or args.web_port > 65535:
        parser.error("--web-port must be between 1 and 65535")
    if args.fix_ci and args.prompt:
        parser.error("--fix-ci does not accept a positional prompt")
    if args.benchmark and args.prompt:
        parser.error("--benchmark does not accept a positional prompt")
    if args.pybughive and args.prompt:
        parser.error("--pybughive does not accept a positional prompt")
    if args.pybughive and args.plan:
        parser.error("--pybughive cannot be combined with --plan")
    if args.pybughive and args.resume:
        parser.error("--pybughive cannot be combined with --resume")
    if args.bugsinpy and args.prompt:
        parser.error("--bugsinpy does not accept a positional prompt")
    if args.bugsinpy and args.plan:
        parser.error("--bugsinpy cannot be combined with --plan")
    if args.bugsinpy and args.resume:
        parser.error("--bugsinpy cannot be combined with --resume")
    if args.fix_ci and args.plan:
        parser.error("--fix-ci cannot be combined with --plan")
    if args.fix_ci and args.resume:
        parser.error("--fix-ci cannot be combined with --resume")
    if not args.fix_ci and (
        args.target
        or args.allowed_path
        or not args.isolate
        or args.keep_failed_worktree
        or args.artifacts_dir
    ):
        parser.error(
            "--target, --allowed-path, and isolation options currently require --fix-ci"
        )
    if args.keep_failed_worktree and not args.isolate:
        parser.error("--keep-failed-worktree cannot be combined with --no-isolate")
    if args.max_fix_attempts < 1:
        parser.error("--max-fix-attempts must be at least 1")
    if args.ci_timeout <= 0:
        parser.error("--ci-timeout must be greater than 0")
    benchmark_options = (
        args.benchmark_suite
        or args.benchmark_case
        or args.benchmark_category
        or args.benchmark_limit is not None
        or args.benchmark_repetitions != 1
        or args.benchmark_output
        or args.benchmark_validate
    )
    if benchmark_options and not args.benchmark:
        parser.error("benchmark options require --benchmark")
    if args.benchmark_limit is not None and args.benchmark_limit < 1:
        parser.error("--benchmark-limit must be at least 1")
    if args.benchmark_repetitions < 1:
        parser.error("--benchmark-repetitions must be at least 1")
    pybughive_options = (
        args.pybughive_dataset
        or args.pybughive_case
        or args.pybughive_workspaces
        or args.pybughive_test_command
        or args.pybughive_output
        or args.pybughive_list
        or args.pybughive_prepare_only
    )
    if pybughive_options and not args.pybughive:
        parser.error("PyBugHive options require --pybughive")
    if args.pybughive and not args.pybughive_dataset:
        parser.error("--pybughive requires --pybughive-dataset")
    if args.pybughive and not args.pybughive_list and not args.pybughive_case:
        parser.error("--pybughive requires --pybughive-case unless --pybughive-list is used")
    if args.pybughive and not args.pybughive_list and not args.pybughive_workspaces:
        parser.error("--pybughive requires --pybughive-workspaces")
    bugsinpy_options = (
        args.bugsinpy_root
        or args.bugsinpy_case
        or args.bugsinpy_workspaces
        or args.bugsinpy_test_command
        or args.bugsinpy_full_test_command
        or args.bugsinpy_output
        or args.bugsinpy_list
        or args.bugsinpy_prepare_only
    )
    if bugsinpy_options and not args.bugsinpy:
        parser.error("BugsInPy options require --bugsinpy")
    if args.bugsinpy and not args.bugsinpy_root:
        parser.error("--bugsinpy requires --bugsinpy-root")
    if args.bugsinpy and not args.bugsinpy_list and not args.bugsinpy_case:
        parser.error("--bugsinpy requires --bugsinpy-case unless --bugsinpy-list is used")
    if args.bugsinpy and not args.bugsinpy_list and not args.bugsinpy_workspaces:
        parser.error("--bugsinpy requires --bugsinpy-workspaces")
    return args


def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    if args.auto:
        return "auto"
    if args.fix_ci or args.benchmark or args.pybughive or args.bugsinpy:
        return "acceptEdits"
    return "default"


async def run_repl(agent: Agent) -> None:
    """Interactive REPL loop."""

    async def confirm_fn(message: str) -> bool:
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    agent.set_confirm_fn(confirm_fn)

    async def plan_approval_fn(plan_content: str) -> dict:
        print_plan_for_approval(plan_content)
        print_plan_approval_options()
        while True:
            try:
                choice = input("  Enter choice (1-4): ").strip()
            except EOFError:
                return {"choice": "manual-execute"}
            if choice == "1":
                return {"choice": "clear-and-execute"}
            elif choice == "2":
                return {"choice": "execute"}
            elif choice == "3":
                return {"choice": "manual-execute"}
            elif choice == "4":
                try:
                    feedback = input("  Feedback (what to change): ").strip()
                except EOFError:
                    feedback = ""
                return {"choice": "keep-planning", "feedback": feedback or None}
            else:
                print("  Invalid choice. Enter 1, 2, 3, or 4.")

    agent.set_plan_approval_fn(plan_approval_fn)

    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        # Always signal a running /loop or /goal to stop — during its inter-tick
        # wait or between-turn evaluation the agent isn't "processing", so the
        # abort path below wouldn't catch it.
        agent.stop_loop()
        agent.stop_goal()
        # is_processing tracks the live task; _output_buffer is only set for
        # SUB-agents, so testing it here meant the main agent could never be
        # interrupted mid-task.
        if agent._aborted is False and agent.is_processing:
            # Agent is processing
            agent.abort()
            print("\n  (interrupted)")
            sigint_count = 0
            print_user_prompt()
        else:
            sigint_count += 1
            if sigint_count >= 2:
                print("\nBye!\n")
                sys.exit(0)
            print("\n  Press Ctrl+C again to exit.")
            print_user_prompt()

    signal.signal(signal.SIGINT, handle_sigint)
    print_welcome()

    while True:
        print_user_prompt()
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!\n")
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print("\nBye!\n")
            break

        # REPL commands
        if inp == "/clear":
            agent.clear_history()
            continue
        if inp == "/plan":
            agent.toggle_plan_mode()
            continue
        if inp == "/cost":
            agent.show_cost()
            continue
        if inp == "/compact":
            try:
                await agent.compact()
            except Exception as e:
                print_error(str(e))
            continue
        if inp == "/goal" or inp.startswith("/goal "):
            condition = inp[len("/goal"):].strip()
            if not condition:
                agent.show_goal()
                continue
            directive = agent.set_goal(condition)
            try:
                await agent.pursue_goal(directive)
            except Exception as e:
                if "abort" not in str(e).lower():
                    print_error(str(e))
            continue
        if inp == "/loop" or inp.startswith("/loop "):
            rest = inp[len("/loop"):].strip()
            try:
                await agent.run_loop(rest)
            except Exception as e:
                if "abort" not in str(e).lower():
                    print_error(str(e))
            continue
        if inp == "/memory":
            memories = list_memories()
            if not memories:
                print_info("No memories saved yet.")
            else:
                print_info(f"{len(memories)} memories:")
                for m in memories:
                    print(f"    [{m.type}] {m.name} — {m.description}")
            continue
        if inp == "/skills":
            skills = discover_skills()
            if not skills:
                print_info("No skills found. Add skills to .claude/skills/<name>/SKILL.md")
            else:
                print_info(f"{len(skills)} skills:")
                for s in skills:
                    tag = f"/{s.name}" if s.user_invocable else s.name
                    print(f"    {tag} ({s.source}) — {s.description}")
            continue
        if inp == "/runs":
            _print_recent_runs()
            continue
        if inp == "/usage":
            _print_usage_summary()
            continue

        # Skill invocation: /<skill-name> [args]
        if inp.startswith("/"):
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name)
            if skill and skill.user_invocable:
                print_info(f"Invoking skill: {skill.name}")
                try:
                    await agent.invoke_skill(skill.name, cmd_args)
                except Exception as e:
                    if "abort" not in str(e).lower():
                        print_error(str(e))
                continue

        # Normal chat
        try:
            await agent.chat(inp)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))

    # Loop exited (EOF / exit / quit) — release MCP subprocesses (issue #8)
    await agent.close()


def main() -> None:
    args = parse_args()

    if args.help:
        print("""
Usage: mini-claude [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits, still confirm dangerous shell
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --auto              Auto Mode: an LLM classifier judges each action instead of asking
  --thinking          Enable extended thinking (Anthropic only)
  --model, -m         Model to use (default: claude-opus-4-6, or MINI_CLAUDE_MODEL env)
  --api-base URL      Use OpenAI-compatible API endpoint (key via env var)
  --resume            Resume the last session
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --web               Start the local three-panel Web workbench
  --web-host HOST     Bind host (default: 127.0.0.1)
  --web-port PORT     Bind port (default: 8765)
  --no-open-browser   Do not open a browser automatically
  --fix-ci             Run a test command and repair pytest failures
  --benchmark          Run the 40-case pytest repair benchmark
  --benchmark-case ID  Select a benchmark case (repeatable)
  --benchmark-category NAME
                       Select a benchmark category (repeatable)
  --benchmark-limit N  Run the first N selected cases
  --benchmark-repetitions N
                       Repeat each selected case (default: 1)
  --benchmark-output PATH
                       Write benchmark reports and case artifacts under PATH
  --benchmark-validate Validate all fixtures without calling a model
  --pybughive          Run one real-world PyBugHive repair case
  --pybughive-dataset PATH
                       Path to PyBugHive's pybughive_current.json
  --pybughive-case ID  Select a case such as black-2254
  --pybughive-workspaces PATH
                       Store prepared project checkouts under PATH
  --pybughive-test-command CMD
                       Override the test command from the dataset
  --pybughive-output PATH
                       Write PyBugHive run artifacts under PATH
  --pybughive-list     List cases without cloning or calling a model
  --pybughive-prepare-only
                       Clone and prepare the buggy revision, then stop
  --bugsinpy           Run one real-world BugsInPy repair case
  --bugsinpy-root PATH Path to the cloned BugsInPy repository
  --bugsinpy-case ID   Select a case such as black-1
  --bugsinpy-workspaces PATH
                       Store prepared BugsInPy checkouts under PATH
  --bugsinpy-localization MODE
                       end-to-end (default) or oracle fault localization
  --bugsinpy-test-command CMD
                       Override the triggering test command
  --bugsinpy-full-test-command CMD
                       Optionally require a full regression command
  --bugsinpy-output PATH
                       Write BugsInPy run artifacts under PATH
  --bugsinpy-list      List cases without cloning or calling a model
  --bugsinpy-prepare-only
                       Clone and prepare the buggy revision, then stop
  --test-command CMD   Test command for --fix-ci (default: python -m pytest -q)
  --max-fix-attempts N Maximum repair attempts (default: 2)
  --repair-skill NAME  Skill used for each repair attempt (default: pytest-repair)
  --ci-timeout SEC     Timeout for each test run (default: 300)
  --ci-report PATH     Write a machine-readable JSON report
  --target PATH        Prioritize a file or directory during repair (repeatable)
  --allowed-path PATH  Further narrow configured writable paths (repeatable)
  --no-isolate         Run in the current checkout instead of a Git worktree
  --keep-failed-worktree
                       Preserve the worktree when all repair attempts fail
  --artifacts-dir PATH Base directory for run artifacts and SQLite index
  --help, -h          Show this help

Local data commands (no API key required):
  mini-claude runs    List recent AutoCI runs and artifact directories
  mini-claude usage   Show cumulative AutoCI usage, cost, and timing

REPL commands:
  /clear              Clear conversation history
  /plan               Toggle plan mode (read-only <-> normal)
  /cost               Show token usage and cost
  /compact            Manually compact conversation
  /goal <condition>   Pursue a goal across turns until an evaluator judges it met
  /goal               Show the active goal's status
  /loop [interval] <prompt>  Re-run a prompt on an interval (5m/2h) or self-paced
  /memory             List saved memories
  /skills             List available skills
  /runs               List recent AutoCI runs
  /usage              Show cumulative AutoCI usage and cost
  /<skill-name>       Invoke a skill (e.g. /commit "fix types")

Examples:
  mini-claude "fix the bug in src/app.ts"
  mini-claude --yolo "run all tests and fix failures"
  mini-claude --plan "how would you refactor this?"
  mini-claude --max-cost 0.50 --max-turns 20 "implement feature X"
  mini-claude --fix-ci --test-command "pytest -q" --target src/app.py --ci-report ci-report.json
  OPENAI_API_KEY=sk-xxx mini-claude --api-base https://aihubmix.com/v1 --model gpt-4o "hello"
  mini-claude --resume
  mini-claude  # starts interactive REPL
""")
        sys.exit(0)

    if not args.fix_ci and args.prompt == ["runs"]:
        _print_recent_runs()
        sys.exit(0)
    if not args.fix_ci and args.prompt == ["usage"]:
        _print_usage_summary()
        sys.exit(0)

    permission_mode = _resolve_permission_mode(args)
    model = args.model or os.environ.get("MINI_CLAUDE_MODEL", "claude-opus-4-6")
    api_base = args.api_base

    # Resolve API config
    resolved_api_base = api_base
    resolved_api_key: str | None = None
    resolved_use_openai = bool(api_base)

    if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL"):
        resolved_api_key = os.environ["OPENAI_API_KEY"]
        resolved_api_base = resolved_api_base or os.environ.get("OPENAI_BASE_URL")
        resolved_use_openai = True
    elif os.environ.get("ANTHROPIC_API_KEY"):
        resolved_api_key = os.environ["ANTHROPIC_API_KEY"]
        resolved_api_base = resolved_api_base or os.environ.get("ANTHROPIC_BASE_URL")
        resolved_use_openai = False
    elif os.environ.get("OPENAI_API_KEY"):
        resolved_api_key = os.environ["OPENAI_API_KEY"]
        resolved_api_base = resolved_api_base or os.environ.get("OPENAI_BASE_URL")
        resolved_use_openai = True

    if not resolved_api_key and api_base:
        resolved_api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        resolved_use_openai = True

    if args.web:
        from .web import run_web
        run_web(
            args.web_host,
            args.web_port,
            open_browser=not args.no_open_browser,
        )
        return

    if args.benchmark and args.benchmark_validate:
        try:
            suite = load_suite(Path(args.benchmark_suite) if args.benchmark_suite else None)
            errors = validate_suite(suite)
            print(f"Benchmark suite: {suite.name} v{suite.version}")
            print(f"Cases: {len(suite.cases)}")
            if errors:
                for error in errors:
                    print_error(error)
                sys.exit(1)
            print("Validation: PASSED")
            sys.exit(0)
        except Exception as e:
            print_error(str(e))
            sys.exit(2)

    if args.pybughive:
        try:
            catalog = load_pybughive_catalog(Path(args.pybughive_dataset))
            if args.pybughive_list:
                print(f"PyBugHive dataset: {catalog.source}")
                print(f"Cases: {len(catalog.cases)}")
                for case in catalog.cases:
                    print(f"  {case.id:24} {case.title}")
                sys.exit(0)
            selected_pybughive_case = catalog.get(args.pybughive_case)
            prepared_pybughive_case = prepare_pybughive_case(
                selected_pybughive_case,
                Path(args.pybughive_workspaces),
                test_command=args.pybughive_test_command,
            )
            if args.pybughive_prepare_only:
                print(f"[PyBugHive] Prepared: {prepared_pybughive_case.project_root}")
                print(f"[PyBugHive] Case: {selected_pybughive_case.id} - {selected_pybughive_case.title}")
                print(f"[PyBugHive] Test command: {prepared_pybughive_case.test_command}")
                print("[PyBugHive] Original install steps:")
                for step in selected_pybughive_case.install_steps:
                    print(f"  {step}")
                print("Install compatible dependencies in your Conda environment, then rerun without --pybughive-prepare-only.")
                sys.exit(0)
        except Exception as e:
            print(f"PyBugHive preparation error: {e}", file=sys.stderr)
            sys.exit(2)

    if args.bugsinpy:
        try:
            bugsinpy_catalog = load_bugsinpy_catalog(Path(args.bugsinpy_root))
            if args.bugsinpy_list:
                print(f"BugsInPy dataset: {bugsinpy_catalog.source}")
                print(f"Cases: {len(bugsinpy_catalog.cases)}")
                for case in bugsinpy_catalog.cases:
                    print(
                        f"  {case.id:24} Python {case.python_version or '?':8} "
                        f"{'; '.join(case.test_files)}"
                    )
                sys.exit(0)
            selected_bugsinpy_case = bugsinpy_catalog.get(args.bugsinpy_case)
            prepared_bugsinpy_case = prepare_bugsinpy_case(
                selected_bugsinpy_case,
                Path(args.bugsinpy_workspaces),
                localization_mode=args.bugsinpy_localization,
                test_command=args.bugsinpy_test_command,
                full_test_command=args.bugsinpy_full_test_command,
            )
            if args.bugsinpy_prepare_only:
                print(f"[BugsInPy] Prepared: {prepared_bugsinpy_case.project_root}")
                print(
                    f"[BugsInPy] Case: {selected_bugsinpy_case.id} - "
                    f"{selected_bugsinpy_case.title}"
                )
                print(f"[BugsInPy] Python: {selected_bugsinpy_case.python_version or 'unknown'}")
                print(f"[BugsInPy] Localization: {prepared_bugsinpy_case.localization_mode}")
                print(f"[BugsInPy] Test command: {prepared_bugsinpy_case.test_command}")
                print("[BugsInPy] Dependency metadata:")
                print(f"  {prepared_bugsinpy_case.project_root / '.bugsinpy' / 'requirements.txt'}")
                if selected_bugsinpy_case.setup_script:
                    print(f"  {prepared_bugsinpy_case.project_root / '.bugsinpy' / 'setup.sh'}")
                print(
                    "Create a compatible external environment, install dependencies, "
                    "then rerun without --bugsinpy-prepare-only."
                )
                sys.exit(0)
        except Exception as e:
            print(f"BugsInPy preparation error: {e}", file=sys.stderr)
            sys.exit(2)

    if not resolved_api_key:
        print_error(
            "API key is required.\n"
            "  Set ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL) for Anthropic format,\n"
            "  or OPENAI_API_KEY + OPENAI_BASE_URL for OpenAI-compatible format."
        )
        sys.exit(1)

    if args.fix_ci:
        original_cwd = Path.cwd().resolve()
        report_path = Path(args.ci_report).resolve() if args.ci_report else None
        artifacts_dir = Path(args.artifacts_dir).resolve() if args.artifacts_dir else None
        config = CiFixConfig(
            test_command=args.test_command,
            cwd=original_cwd,
            max_attempts=args.max_fix_attempts,
            timeout_seconds=args.ci_timeout,
            repair_skill_name=args.repair_skill,
        )

        def _create_ci_agent(workspace_policy):
            custom_tools = _ci_tool_definitions(workspace_policy)
            return Agent(
                permission_mode=permission_mode,
                model=model,
                thinking=args.thinking,
                max_cost_usd=args.max_cost,
                max_turns=args.max_turns,
                api_base=resolved_api_base if resolved_use_openai else None,
                anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
                api_key=resolved_api_key,
                custom_tools=custom_tools,
                workspace_policy=workspace_policy,
                enable_mcp=False,
            )

        async def _fix_ci() -> int:
            print(f"[AutoCI-Fix] Running: {config.test_command}", flush=True)
            result = await run_ci_fix_workflow(
                config=config,
                agent_factory=_create_ci_agent,
                cli_allowed_paths=tuple(args.allowed_path),
                targets=tuple(args.target),
                isolate=args.isolate,
                keep_failed_worktree=args.keep_failed_worktree,
                artifacts_dir=artifacts_dir,
            )
            report = result.report
            print(report.render_text())
            if report_path:
                written_report = write_json_report(report, report_path)
                print(f"[AutoCI-Fix] Report written to: {written_report}")
            if result.artifact_dir:
                print(f"[AutoCI-Fix] Run artifacts: {result.artifact_dir}")
            if result.worktree_preserved and result.worktree_root:
                print(f"[AutoCI-Fix] Failed worktree preserved at: {result.worktree_root}")
            elif args.isolate and not report.succeeded:
                print("[AutoCI-Fix] Failed changes rolled back; source checkout was not modified.")
            return report.exit_code

        try:
            sys.exit(asyncio.run(_fix_ci()))
        except Exception as e:
            print(f"AutoCI-Fix error: {e}", file=sys.stderr)
            sys.exit(2)

    if args.pybughive:
        def _create_pybughive_agent(workspace_policy):
            return Agent(
                permission_mode="acceptEdits",
                model=model,
                thinking=args.thinking,
                max_cost_usd=args.max_cost,
                max_turns=args.max_turns,
                api_base=resolved_api_base if resolved_use_openai else None,
                anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
                api_key=resolved_api_key,
                custom_tools=_ci_tool_definitions(workspace_policy),
                workspace_policy=workspace_policy,
                enable_mcp=False,
            )

        async def _pybughive() -> int:
            print(
                f"[PyBugHive] Running {prepared_pybughive_case.case.id}: "
                f"{prepared_pybughive_case.test_command}",
                flush=True,
            )
            result = await run_pybughive_case(
                prepared_pybughive_case,
                agent_factory=_create_pybughive_agent,
                max_attempts=args.max_fix_attempts,
                timeout_seconds=args.ci_timeout,
                artifacts_dir=(Path(args.pybughive_output).resolve() if args.pybughive_output else None),
            )
            print(result.render_text())
            if result.artifact_dir:
                print(f"[PyBugHive] Run artifacts: {result.artifact_dir}")
            return 0 if result.passed else 1

        try:
            sys.exit(asyncio.run(_pybughive()))
        except Exception as e:
            print(f"PyBugHive error: {e}", file=sys.stderr)
            sys.exit(2)

    if args.bugsinpy:
        def _create_bugsinpy_agent(workspace_policy):
            return Agent(
                permission_mode="acceptEdits",
                model=model,
                thinking=args.thinking,
                max_cost_usd=args.max_cost,
                max_turns=args.max_turns,
                api_base=resolved_api_base if resolved_use_openai else None,
                anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
                api_key=resolved_api_key,
                custom_tools=_ci_tool_definitions(workspace_policy),
                workspace_policy=workspace_policy,
                enable_mcp=False,
            )

        async def _bugsinpy() -> int:
            print(
                f"[BugsInPy] Running {prepared_bugsinpy_case.case.id} "
                f"({prepared_bugsinpy_case.localization_mode}): "
                f"{prepared_bugsinpy_case.test_command}",
                flush=True,
            )
            result = await run_bugsinpy_case(
                prepared_bugsinpy_case,
                agent_factory=_create_bugsinpy_agent,
                max_attempts=args.max_fix_attempts,
                timeout_seconds=args.ci_timeout,
                artifacts_dir=(
                    Path(args.bugsinpy_output).resolve()
                    if args.bugsinpy_output
                    else None
                ),
            )
            print(result.render_text())
            if result.artifact_dir:
                print(f"[BugsInPy] Run artifacts: {result.artifact_dir}")
            return 0 if result.passed else 1

        try:
            sys.exit(asyncio.run(_bugsinpy()))
        except Exception as e:
            print(f"BugsInPy error: {e}", file=sys.stderr)
            sys.exit(2)

    if args.benchmark:
        suite = load_suite(Path(args.benchmark_suite) if args.benchmark_suite else None)
        output_dir = Path(args.benchmark_output).resolve() if args.benchmark_output else None

        def _create_benchmark_agent(workspace_policy):
            return Agent(
                permission_mode="acceptEdits",
                model=model,
                thinking=args.thinking,
                max_cost_usd=args.max_cost,
                max_turns=args.max_turns,
                api_base=resolved_api_base if resolved_use_openai else None,
                anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
                api_key=resolved_api_key,
                custom_tools=_ci_tool_definitions(workspace_policy),
                workspace_policy=workspace_policy,
                enable_mcp=False,
            )

        async def _benchmark() -> int:
            report, run_dir = await run_benchmark(
                suite,
                agent_factory=_create_benchmark_agent,
                case_ids=tuple(args.benchmark_case),
                categories=tuple(args.benchmark_category),
                limit=args.benchmark_limit,
                repetitions=args.benchmark_repetitions,
                max_attempts=args.max_fix_attempts,
                timeout_seconds=args.ci_timeout,
                output_dir=output_dir,
            )
            print(report.render_text())
            print(f"[Benchmark] Reports: {run_dir}")
            return 0 if report.summary()["passed_runs"] == report.summary()["total_runs"] else 1

        try:
            sys.exit(asyncio.run(_benchmark()))
        except Exception as e:
            print(f"Benchmark error: {e}", file=sys.stderr)
            sys.exit(2)

    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved_api_base if resolved_use_openai else None,
        anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
        api_key=resolved_api_key,
    )

    # Resume session
    if args.resume:
        session_id = get_latest_session_id()
        if session_id:
            session = load_session(session_id)
            if session:
                agent.restore_session({
                    "anthropicMessages": session.get("anthropicMessages"),
                    "openaiMessages": session.get("openaiMessages"),
                })
            else:
                print_info("No session found to resume.")
        else:
            print_info("No previous sessions found.")

    prompt = " ".join(args.prompt) if args.prompt else None

    if prompt:
        # One-shot mode — always release MCP subprocesses on the way out
        # (issue #8)
        async def _one_shot() -> None:
            try:
                await agent.chat(prompt)
            finally:
                await agent.close()
        try:
            asyncio.run(_one_shot())
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
    else:
        # Interactive REPL
        asyncio.run(run_repl(agent))


if __name__ == "__main__":
    main()
