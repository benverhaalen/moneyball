"""Compatibility alias for moneyball.league.strategy_sources."""
import importlib
import sys
_module = importlib.import_module("moneyball.league.strategy_sources")
if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
