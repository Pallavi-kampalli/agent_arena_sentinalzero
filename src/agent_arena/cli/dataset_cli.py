import argparse
import asyncio
import json

import sqlalchemy as sa

from agent_arena.db import get_session_maker
from agent_arena.models.task import Task
from agent_arena.services.dataset_service import DatasetService
from agent_arena.tasks.generator import FAMILIES, VARIANTS, TaskGenerator
from agent_arena.tasks.validator import validate_task
from agent_arena.world.generator import generate_world


def format_matrix_ascii(matrix: dict[str, dict[str, int]], title: str) -> str:
    """Renders a 6x6 Family x Variant matrix as a clean ASCII grid with row/col totals."""
    lines = [f"\n=== {title} ==="]

    fam_width = max(len(fam) for fam in FAMILIES) + 2
    fam_width = max(fam_width, len("Family \\ Variant") + 2)
    var_widths = {v: max(len(v) + 2, 8) for v in VARIANTS}
    tot_width = 8

    # Table borders and header
    sep_parts = ["+" + "-" * fam_width]
    for v in VARIANTS:
        sep_parts.append("-" * var_widths[v])
    sep_parts.append("-" * tot_width + "+")
    sep = "+".join(sep_parts)

    header_label = "Family \\ Variant"
    header_parts = [f"| {header_label:<{fam_width - 2}} "]
    for v in VARIANTS:
        header_parts.append(f"{v:>{var_widths[v] - 2}} ")
    header_parts.append(f"{'TOTAL':>{tot_width - 2}} |")
    header = " | ".join(header_parts)

    lines.append(sep)
    lines.append(header)
    lines.append(sep)

    col_totals = {v: 0 for v in VARIANTS}
    grand_total = 0

    for fam in FAMILIES:
        row_total = sum(matrix.get(fam, {}).get(v, 0) for v in VARIANTS)
        grand_total += row_total
        for v in VARIANTS:
            col_totals[v] += matrix.get(fam, {}).get(v, 0)
        c = matrix.get(fam, {})
        row_parts = [f"| {fam:<{fam_width - 2}} "]
        for v in VARIANTS:
            row_parts.append(f"{c.get(v, 0):>{var_widths[v] - 2}} ")
        row_parts.append(f"{row_total:>{tot_width - 2}} |")
        lines.append(" | ".join(row_parts))

    lines.append(sep)
    tot_parts = [f"| {'TOTAL':<{fam_width - 2}} "]
    for v in VARIANTS:
        tot_parts.append(f"{col_totals[v]:>{var_widths[v] - 2}} ")
    tot_parts.append(f"{grand_total:>{tot_width - 2}} |")
    lines.append(" | ".join(tot_parts))
    lines.append(sep)
    return "\n".join(lines)


async def cmd_load_dataset(dataset_type: str, count: int | None, seed: int | None, replace: bool):
    session_maker = get_session_maker()
    async with session_maker() as session:
        service = DatasetService(session)
        result = await service.generate_and_load_dataset(
            dataset_type=dataset_type,
            count=count,
            base_seed=seed,
            replace_existing=replace,
        )
        matrix = result.pop("matrix", None)
        print(json.dumps(result, indent=2))
        if matrix:
            print(format_matrix_ascii(matrix, f"{dataset_type.upper()} Dataset Generation Matrix"))


async def cmd_stats():
    session_maker = get_session_maker()
    async with session_maker() as session:
        dev_res = await session.execute(sa.select(sa.func.count()).select_from(Task).where(Task.dataset == "dev"))
        dev_count = dev_res.scalar() or 0

        hidden_res = await session.execute(sa.select(sa.func.count()).select_from(Task).where(Task.dataset == "hidden"))
        hidden_count = hidden_res.scalar() or 0

        print("\n==========================================")
        print("    Agent Arena Tasks Table Statistics    ")
        print("==========================================")
        print(f"Dev Tasks Loaded:    {dev_count}")
        print(f"Hidden Tasks Loaded: {hidden_count}")
        print(f"Total Tasks:         {dev_count + hidden_count}")

        # Build dev 6x6 matrix
        dev_matrix = {fam: {var: 0 for var in VARIANTS} for fam in FAMILIES}
        rows_dev = await session.execute(
            sa.select(Task.family, Task.variant, sa.func.count())
            .where(Task.dataset == "dev")
            .group_by(Task.family, Task.variant)
        )
        for fam, var, cnt in rows_dev.all():
            if fam in dev_matrix and var in dev_matrix[fam]:
                dev_matrix[fam][var] = cnt

        print(format_matrix_ascii(dev_matrix, "Dev Dataset 6x6 Matrix (Family x Variant)"))

        # Build hidden 6x6 matrix
        hidden_matrix = {fam: {var: 0 for var in VARIANTS} for fam in FAMILIES}
        rows_hid = await session.execute(
            sa.select(Task.family, Task.variant, sa.func.count())
            .where(Task.dataset == "hidden")
            .group_by(Task.family, Task.variant)
        )
        for fam, var, cnt in rows_hid.all():
            if fam in hidden_matrix and var in hidden_matrix[fam]:
                hidden_matrix[fam][var] = cnt

        print(format_matrix_ascii(hidden_matrix, "Hidden Dataset 6x6 Matrix (Family x Variant)"))


def cmd_generate_sample(family: str, variant: str, seed: int):
    world = generate_world(seed=seed)
    gen = TaskGenerator(seed=seed)
    task = gen.generate_task(
        base_world=world,
        family=family,
        variant=variant,
        task_id=f"SAMPLE-{family}-{variant}",
        dataset="dev",
    )
    is_valid, err = validate_task(task)
    print(f"Validation: {'PASSED' if is_valid else f'FAILED: {err}'}")
    print(json.dumps(task, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description="Agent Arena Dataset CLI (Internal Operator Tool)")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # load-dataset
    p_load = subparsers.add_parser("load-dataset", help="Generate, validate, and load dataset into PostgreSQL")
    p_load.add_argument("--type", choices=["dev", "hidden"], default="dev", help="Dataset type to generate")
    p_load.add_argument("--count", type=int, default=None, help="Optional count override (defaults to settings table)")
    p_load.add_argument("--seed", type=int, default=None, help="Base seed for reproducibility")
    p_load.add_argument("--no-replace", action="store_true", help="Fail if duplicate tasks exist instead of replacing")

    # stats
    subparsers.add_parser("stats", help="Show database task statistics and distributions")

    # generate-sample
    p_sample = subparsers.add_parser("generate-sample", help="Generate and inspect a single sample task")
    p_sample.add_argument("--family", default="duplicate_payment", help="Task family")
    p_sample.add_argument("--variant", default="normal", help="Task variant")
    p_sample.add_argument("--seed", type=int, default=42, help="Seed")

    args = parser.parse_args()

    if args.subcommand == "load-dataset":
        asyncio.run(cmd_load_dataset(args.type, args.count, args.seed, replace=not args.no_replace))
    elif args.subcommand == "stats":
        asyncio.run(cmd_stats())
    elif args.subcommand == "generate-sample":
        cmd_generate_sample(args.family, args.variant, args.seed)


if __name__ == "__main__":
    main()
