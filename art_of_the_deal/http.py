"""Compatibility alias for moneyball.league.http."""
import importlib
import sys
_module = importlib.import_module("moneyball.league.http")
if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
