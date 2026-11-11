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

Runs select approved knowledge using shared words with the goal and record which
items were provided to the model. New evidence-backed observations are saved as
proposals for review; they do not become approved knowledge automatically.

`scrappy_doo.evaluation` scores controlled scenarios against instrumented
execution traces. For a CLI run, obtain a trace with
`ExecutionStore(data_dir).evaluation_trace(execution_id)`. The runtime records
tool attempts before invoking handlers and finalizes the trace after saving the
run. A started event does not prove an action completed, and incomplete or older
uninstrumented traces are not considered complete. Checks include result and
status, required and unauthorized tool calls, evidence references, and knowledge
review. An artifact's presence does not prove its content supports a finding;
the trace is not tamper-proof, and passing these checks is not a live end-to-end
evaluation.

Run the controlled evaluation with the OpenRouter key and Playwright MCP
available in the environment:

```sh
PYTHONPATH=src python3 -m scrappy_doo.controlled_evaluation --data-dir /path/to/evaluation-data
```

The command starts a sample server on an ephemeral loopback port and runs three
separate browser sessions. Follow the terminal prompts to sign in manually; the
fixture credentials are `qa` / `test-password`. The evaluation makes live model
requests, records each execution in the selected data directory, and reports
whether the observed outcomes and traces match the known scenarios. Browser
origin filtering is best effort, not strict network isolation.

The `strict_policy` and `isolation_proof` modules contain hypothetical policy
decisions and simulated proof assessments. They do not configure or verify a
network boundary, and their results cannot enable strict mode. A real proof
still requires the isolated browser, proxy, protected viewer, and independent
receiver checks described in `docs/research/macos-browser-isolation.md`.
The `live_isolation_proof` module supplies a fail-closed orchestration seam,
but no live browser, proxy, or receiver adapter. Its reports cannot certify
isolation or enable strict mode, even when supplied observations pass.
