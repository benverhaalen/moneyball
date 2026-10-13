"""Compatibility alias for moneyball.league.league_research."""
import importlib
import sys
_module = importlib.import_module("moneyball.league.league_research")
if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
