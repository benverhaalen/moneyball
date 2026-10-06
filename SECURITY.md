# Security and private data

Keep ESPN authentication outside chat, issue reports and source control. Private stores contain league/account information and may contain licensed evidence; they are not public fixtures. The server is local and defaults to stdio. Do not expose its HTTP transport to an untrusted network without an appropriate access boundary.

For a vulnerability, use GitHub private vulnerability reporting if available on this repository. If it is unavailable, open a minimal issue requesting a private contact without including exploit details or secrets. No response-time guarantee is promised.

For ordinary bugs, provide a synthetic reproduction, package/Python version and redacted error output. Exclude credentials, cookies, account IDs and private snapshots. If a credential is exposed, revoke or refresh it at the provider; deleting a commit does not revoke a session.
