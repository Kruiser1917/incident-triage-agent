"""CLI: python -m triage.simulator {generate,validate,show}."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from triage.simulator.generate import generate
from triage.simulator.show import load_case, render
from triage.simulator.validate import validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m triage.simulator")
    parser.add_argument("--root", type=Path, default=Path("."), help="repository root")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate", help="regenerate fixtures and the golden set")
    commands.add_parser("validate", help="check the dataset (schema, balance, solvability)")
    show = commands.add_parser("show", help="print one case in a human-readable form")
    show.add_argument("case_id")
    show.add_argument("--answer", action="store_true", help="also print the answer key")
    args = parser.parse_args(argv)

    if args.command == "generate":
        cases = generate(args.root)
        print(f"generated {len(cases)} cases")
    elif args.command == "validate":
        report = validate(args.root)
        print(report.render())
        return 1 if report.errors else 0
    elif args.command == "show":
        print(render(load_case(args.root, args.case_id), answer=args.answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
