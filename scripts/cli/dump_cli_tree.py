"""Print the complete command tree from the live ``dc`` argparse parser.

Run with ``uv run python scripts/dump_cli_tree.py``. Add ``--show-options`` to
include the arguments accepted by each command.
"""

from __future__ import annotations

import argparse

from docling_context.cli import _parser


def _subcommands(
    parser: argparse.ArgumentParser,
) -> list[tuple[str, argparse.ArgumentParser, str]]:
    commands = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        descriptions = {
            choice.dest: choice.help
            for choice in action._choices_actions
            if choice.help is not argparse.SUPPRESS
        }
        for name, child in action.choices.items():
            commands.append((name, child, descriptions.get(name, "")))
    return commands


def _arguments(parser: argparse.ArgumentParser) -> str:
    arguments = []
    for action in parser._actions:
        if (
            isinstance(action, argparse._SubParsersAction)
            or action.help is argparse.SUPPRESS
        ):
            continue
        if action.dest == "help":
            continue
        if action.option_strings:
            long_options = [
                value for value in action.option_strings if value.startswith("--")
            ]
            option = (
                "/".join(long_options)
                if isinstance(action, argparse.BooleanOptionalAction)
                else long_options[0]
                if long_options
                else action.option_strings[0]
            )
            arguments.append(option)
        else:
            arguments.append(action.dest.upper())
    return ", ".join(arguments)


def _print_children(
    parser: argparse.ArgumentParser, *, prefix: str = "", show_options: bool = False
) -> None:
    children = _subcommands(parser)
    for index, (name, child, description) in enumerate(children):
        last = index == len(children) - 1
        line = f"{prefix}{'└── ' if last else '├── '}{name}"
        if description:
            line += f"  {description}"
        if show_options:
            arguments = _arguments(child)
            if arguments:
                line += f"  [{arguments}]"
        print(line)
        _print_children(
            child,
            prefix=prefix + ("    " if last else "│   "),
            show_options=show_options,
        )


def main() -> None:
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument(
        "--show-options", action="store_true", help="Show arguments for each command"
    )
    args = options.parse_args()
    parser = _parser()
    root = f"{parser.prog}  {parser.description}"
    if args.show_options:
        root += f"  [{_arguments(parser)}]"
    print(root)
    _print_children(parser, show_options=args.show_options)


if __name__ == "__main__":
    main()
