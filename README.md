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
