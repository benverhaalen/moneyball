# Contributing

Python 3.11+ is supported. Create a virtual environment, install the package and optional test dependencies, then run both suites:

```sh
python -m pip install -e '.[agent,analytics,dev]'
python -m unittest discover -s tests -v
python -m unittest discover -s tests/art_of_the_deal -v
python -m build
```

The second discovery command is required because the connected-service tests live in a separate directory. Tests should use synthetic provider responses and temporary directories. The normal suite does not require a live fantasy account, paid data source or a browser session. Keep provider-live experiments separate and record what they actually verify.

Before a packaging change, install the built wheel into a fresh environment outside the repository and run `moneyball-agent demo`, `moneyball-agent --help`, and the packaged skill/resource checks. CI performs this distribution smoke test in addition to the suites.

Use namespaced player IDs, preserve source acquisition times and report partial failures. For research-method changes, update the method version and test binding/invalidation. For adapter changes, add representative exact-rule, null/co-owner, identity and failure-path fixtures. A schema test cannot prove research quality, and a mechanical test cannot prove strategic edge.

Do not commit private league snapshots, player dossiers, warehouse partitions, browser state, cookies, `.env` files or experiment transcripts. Review staged files explicitly. Public examples must be synthetic and must not contain a user's league/account IDs or local machine paths. Source-code licensing and data licensing are separate; retain third-party notices when adapting code.

Proposals are welcome as GitHub issues or pull requests. Explain the concrete user journey, the observed problem, the reference's specific useful mechanism and how the change was verified. Preserve narrower supported behavior instead of broadening claims beyond the tested implementation.
