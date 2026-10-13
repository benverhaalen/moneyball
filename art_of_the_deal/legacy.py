"""Compatibility alias for moneyball.league.legacy."""
import importlib
import sys
_module = importlib.import_module("moneyball.league.legacy")
if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
