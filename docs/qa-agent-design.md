# QA agent design

Status: draft for review. Implementation has not started.

## Purpose and scope

Build a local Python CLI that carries out a QA engineer's natural-language test goal against one selected web application and environment. The agent explores as needed, reports evidence-backed findings, and proposes durable application knowledge for human review. Target macOS first while keeping Python components portable where practical. The first release covers the complete local workflow; scheduling, shared storage, parallel runs, and headless operation are deferred.

## Execution workflow

1. The engineer selects an application and environment, provides a goal and its expected behavior where necessary, and specifies permitted data changes and run limits. Environment configuration controls allowed destinations, capture, and whether changes are permitted at all. New environments start read-only.
2. The CLI validates required configuration, local storage, model access, the Playwright MCP connection, and any required network enforcement. It displays effective settings and asks for confirmation before actions begin.
3. An isolated, visible browser session starts. The engineer signs in manually when needed; agent observation and evidence capture are suspended during sign-in until the engineer indicates it is complete.
4. A Python-controlled loop gives the model the goal, relevant approved knowledge, available tools, and permitted observations. It validates requested calls, executes authorized tools, and returns results until the goal is addressed, a limit is reached, or the run is interrupted.
5. The agent explores within the goal, investigates unexpected results within limits, and continues after a failure where practical. It asks the engineer for clarification or a specific action approval when blocked. No answer is not approval. If it cannot proceed, affected work is inconclusive.
6. The agent saves findings, evidence, execution history, cleanup status, and candidate knowledge. The engineer reviews the report and knowledge proposals through the CLI.

## Architecture and integrations

- Use one agent with separate Python components for model access, tool access, storage, and reporting. Limit external libraries where practical.
- OpenRouter is the initial provider; `z-ai/glm-5.3-flash` is the configurable default model. Validate model tool calls and structured findings before using them. Provider credentials come from the local environment. Model suitability and account access require testing.
- Use a configured Playwright MCP server for browser actions. Start it or connect to it in isolated mode. The Python application mediates access to its tools.
- Expose defined local inspection tools, scoped to explicitly configured directories and read-only by default. Do not expose arbitrary shell commands or scripts in the first version.
- Use structured page observations as the primary browser input and screenshots when visual information is needed and permitted. Page content, downloaded files, and tool output are untrusted data and cannot change instructions, permissions, or the goal.
- Support one active execution per local data directory. Record the effective goal, environment, permissions, limits, model, and tool configuration without secrets.

## Boundaries and authentication

Each environment explicitly chooses one browser boundary mode:

- **Strict:** require a separately enforced and verified network boundary covering each browser request, including navigation, redirects, page requests, and requests inside encrypted connections. Refuse to start if the required enforcement cannot be verified. Explicitly configured private-network applications must be supported. Docker Desktop on macOS is an acceptable candidate dependency, subject to a working proof; the enforcement mechanism remains open. Strict mode is unavailable until a separate proof demonstrates enforcement and browser interaction. There is no automatic fallback to best effort.
- **Best effort:** use Playwright MCP origin filtering and disclose that it does not fully contain redirects or other traffic before execution.

Playwright MCP's origin filtering is not a security boundary and does not cover redirects. See the [Playwright MCP documentation](https://github.com/microsoft/playwright-mcp/blob/main/README.md).
The [isolation research](research/macos-browser-isolation.md) describes a candidate container and proxy topology, its limitations, and acceptance checks. An allowed destination can relay data elsewhere, so a destination allowlist cannot guarantee its ultimate destination.

Investigate a local TLS-intercepting proxy with a dedicated certificate trusted only by the isolated browser. Disclose that it can see HTTPS contents and do not persist those contents by default. If required inspection is incompatible with an application, stop strict execution rather than bypassing inspection; best-effort mode remains a separate explicit choice.

Investigate a protected viewer for the containerized browser so the engineer can observe it and sign in. Disable viewer clipboard sharing and file transfer by default. The initial complete workflow can use best-effort mode while strict mode awaits proof.

The engineer selects an account suitable for the environment. The account's privileges do not expand the run's declared permissions. Read-only means the agent must not intentionally submit changes; browsing can still have side effects, so account and environment permissions matter. Specific action approvals apply only to the described action unless explicitly broadened. The agent reports data it created or changed and cleanup status, and only cleans up within its permissions.

The engineer authorizes application data sent to the model. Capture controls cover screenshots, text observations, and history; saving a screenshot and sending it to the model are separately configurable. Avoid deliberately storing passwords and tokens. Capture controls reduce exposure but cannot guarantee all sensitive content is excluded.

## Findings and evidence

A single goal can produce multiple findings. Each finding records expected and observed outcomes, a result, and references to supporting observations or local artifacts. A failure requires evidence of an unmet expectation; insufficient evidence, missing expectations, blockers, and exhausted limits produce an inconclusive finding. Tool and infrastructure errors are distinguished from application behavior. If a capture fails, record it and continue when other evidence is sufficient.

The overall result is failed if any finding fails, otherwise inconclusive if any required part is inconclusive, otherwise passed when the entire goal is verified. An interrupted execution retains its interrupted status and saved partial results. Cleanup status is separate from the test outcome.

The engineer can override or revise a finding with a reason. Preserve the original agent assessment and all review revisions; calculate the current overall result from reviewed findings.

Keep a chronological history of tool requests and results, relevant decisions, and evidence references, subject to capture settings. Reports make the expected outcome, observation, result, evidence, and reasons for inconclusive findings accessible without reconstructing the whole history.

## Memory and local storage

Store structured records in a local SQLite database and reports and evidence as ordinary files, in a configurable data directory outside the repository by default. Exclude runtime data from Git. Rely on operating-system permissions and existing disk protection initially.

Knowledge belongs to an application and can be restricted to an environment. Load relevant approved knowledge only for the selected application and environment; do not silently reuse another application's knowledge. Observations become proposals, not trusted knowledge, until a QA engineer reviews them. The CLI supports inspecting, editing, approving, rejecting, and retiring knowledge. Retain source execution and evidence references when available, and record which approved knowledge influenced a run.

When current observations conflict with approved knowledge, use the observation for the current run, report the conflict, and propose an update. Previously approved knowledge remains until reviewed.

Retain runs and evidence until explicitly deleted. Deleting a run deletes its history, report, evidence, and unapproved proposals. Approved knowledge survives, marked to show that its source was deleted.

## Limits and recovery

Configure active execution time, tool calls, and optionally estimated model spending. If a spending limit cannot be supported by reliable usage or pricing information, refuse to start with that limit; disclose estimated enforcement. Human sign-in and prompt waits have separate configurable limits and do not consume active execution time. Reasonable retries remain within limits. Preserve partial records on interruption, and report unresolved infrastructure or tool failures as blockers rather than product failures.

## Validation and open work

Evaluate GLM 5.3 Flash against a controlled sample application with known passing, failing, and blocked scenarios, including tool selection, boundary behavior, and quality of evidence. The project owner handles end-to-end tests. Unit tests come first for behavioral implementation and are presented to the owner for coverage review.

Open technical decisions include the strict network enforcement mechanism and supported platforms; exact CLI commands, configuration format, database schema, capture implementation, model request settings, and limit defaults. Verify enforcement and integration behavior before claiming guarantees.
