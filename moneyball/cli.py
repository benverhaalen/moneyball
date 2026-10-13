"""Moneyball's primary connected-league CLI; legacy analytics stay explicit."""
import sys

def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "agent":
        args = args[1:]  # previous spelling, same implementation and store
    if args and (args[0] == "legacy" or "--data-dir" in args):
        from .legacy_cli import main as legacy_main
        previous = sys.argv
        try:
            sys.argv = ["moneyball legacy", *(args[1:] if args[0] == "legacy" else args)]
            return legacy_main()
        finally:
            sys.argv = previous
    from .league.cli import main as connected_main
    return connected_main(args)


def __getattr__(name):
    # Existing Python callers keep working without importing analytics on the
    # normal connection/retrieval path.
    from . import legacy_cli
    return getattr(legacy_cli, name)


if __name__ == "__main__":
    main()
