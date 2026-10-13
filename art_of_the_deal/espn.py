"""Compatibility alias for moneyball.league.espn."""
import importlib
import sys
_module = importlib.import_module("moneyball.league.espn")
if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
