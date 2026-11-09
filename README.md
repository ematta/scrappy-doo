# scrappy-doo

Run unit tests with Python 3.13 or newer:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The OpenRouter adapter reads `OPENROUTER_API_KEY` from the environment unless
an API key is supplied explicitly. It defaults to `z-ai/glm-5.3-flash`; the
model can be changed when creating the adapter. Browser and CLI integration are
still in development.

The browser adapter launches a locally available Playwright MCP server through
`npx --no-install @playwright/mcp`; Node.js and the package must be available in
the environment. It starts an isolated visible session and supports only
explicitly selected best-effort origin filtering. This filter does not constrain
redirects and is not a security boundary. Strict isolation remains unavailable.

Create an environment, then run a goal interactively:

```sh
scrappy-doo environment add --application shop --name staging \
  --url https://staging.example.com --allow-origin https://staging.example.com \
  --boundary best-effort
scrappy-doo run --application shop --environment staging --goal "Verify the cart total"
```

Environments are read-only by default. Use `--allow-changes` when creating one
to permit action-specific approval. Add `--local-root PATH` to expose read-only
local inspection within a directory. The startup summary describes settings and
the data sent to OpenRouter; the run requires confirmation and a manual sign-in
handoff. Disable saved text observations or tool history with `--no-text-capture`
or `--no-history-capture`. Screenshot capture and transmission are not yet
integrated; an environment requesting them cannot start a real run.

Use `scrappy-doo report show ID` to inspect a run, `report review` to record a
finding review, and `knowledge list --application NAME` to review proposals. Use
`knowledge list --application NAME --approved --environment NAME` to list
approved knowledge. `scrappy-doo --help` shows the available commands.
