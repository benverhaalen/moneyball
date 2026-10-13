"""Compatibility alias for moneyball.league.auth."""
import importlib
import sys
_module = importlib.import_module("moneyball.league.auth")
if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
