"""Compatibility alias for moneyball.league.free_agents."""
import importlib
import sys
_module = importlib.import_module("moneyball.league.free_agents")
if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
