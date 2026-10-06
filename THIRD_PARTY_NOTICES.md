# Third-party notices

This distribution incorporates or adapts material from the projects below. Upstream service data and API access are governed separately by each service's current terms; an open-source client license does not grant rights to ESPN, Sleeper, CBS, FantasyPros, Kalshi, or other source data.

## espn-api

Project: `cwendt94/espn-api`
Source reviewed: <https://github.com/cwendt94/espn-api/tree/cec2935d9d94a3ab88dd6eff0cf5a4fdc0e80d2f>
Upstream license: <https://github.com/cwendt94/espn-api/blob/cec2935d9d94a3ab88dd6eff0cf5a4fdc0e80d2f/LICENSE>

Art of the Deal's ESPN adapter is its own conservative normalizer, but it adapts the upstream project's observed lineup/position identifiers, stat identifier meanings, `kona_player_info` free-agent view, and `x-fantasy-filter` request structure. The required MIT notice follows.

```text
MIT License

Copyright (c) 2019 Christian Wendt

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Model Context Protocol Python SDK

Package: `mcp`
Required version range: `>=1.26,<2`
Project: <https://github.com/modelcontextprotocol/python-sdk>
License for v1.26.0: <https://github.com/modelcontextprotocol/python-sdk/blob/v1.26.0/LICENSE>

```text
MIT License

Copyright (c) 2024 Anthropic, PBC

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The MCP SDK has its own transitive dependencies. Installed distributions retain their own license metadata and notices; downstream packaging should generate a complete dependency bill of materials for the resolved environment.
