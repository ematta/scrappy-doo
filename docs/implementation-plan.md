# QA agent implementation plan

Status: proposed sequence for the first local release. See [the design](qa-agent-design.md) for agreed behavior and [isolation research](research/macos-browser-isolation.md) for strict-mode feasibility.

## Working agreements

- For every behavioral slice, write unit tests first, run them to establish the expected failure, and ask the project owner whether coverage is comprehensive enough before implementing that behavior.
- Use Python's standard-library `unittest` as the initial test runner; add dependencies only when they provide necessary integration capability. Document commands as tools are introduced.
- The project owner owns end-to-end tests. Use a controlled sample application with an authenticated workflow, a deliberate failure, and a blocked dependency as a shared validation target.
- Strict mode remains unavailable until a separate proof verifies enforcement and browser interaction. Never substitute best-effort mode silently.

## Sequence

1. **Execution model and results.** Define goals, environment settings, permissions, limits, findings, evidence references, run status, and result aggregation. Test failed, inconclusive, passed, interrupted, cleanup, and reviewed-result precedence, including incomplete goals.
2. **Local persistence and review.** Add a configurable data directory, SQLite records, file-based artifacts, and CLI-accessible inspection. Cover application and environment isolation, interruptions, evidence failures, deletion, finding overrides, and review history. Preserve approved knowledge provenance when a source execution is deleted.
3. **Knowledge lifecycle.** Add environment-scoped knowledge, proposals, approval, editing, rejection, retirement, conflict reporting, and selection of relevant approved knowledge. Test that unapproved and other-application knowledge is not treated as trusted.
4. **Execution policy and orchestration.** Add the single-agent loop with validated tool calls, explicit permissions, action-specific approvals, clarification, retry and stop behavior, time and tool-call limits, and handling of untrusted content. Test that unanswered approval never authorizes an action and that failures and interruptions preserve partial results.
5. **Provider integration.** Add a configurable OpenRouter adapter with `z-ai/glm-5.3-flash` as the default, structured model responses, usage accounting, and credential handling. Unit-test with controlled provider responses; separately verify account access and model behavior. Only enable a spending limit when its enforcement can be supported and disclose estimates.
6. **Browser and local tools.** Integrate an isolated, visible Playwright MCP session, manual sign-in with suspended agent observation, configured local read-only inspection tools, and best-effort origin filtering. Test configuration and policy handling using controlled adapters; verify real browser behavior separately. Disclose best-effort limitations at startup.
7. **Interactive CLI and reporting.** Complete setup, startup validation and confirmation, prompts, evidence controls, actionable findings, cleanup reporting, knowledge review, and run deletion. Ensure secrets are excluded from configuration and deliberate capture. Document setup and commands.
8. **Integrated evaluation.** Exercise the vertical slice on the controlled sample application, with the project owner responsible for end-to-end coverage. Evaluate tool selection, permission behavior, results, evidence, memory review, interruption, and provider limitations. Record failures and refine with test-first changes.
9. **Strict-mode proof.** Independently prototype the candidate container, proxy, and protected viewer. Test the actual pinned macOS, Docker, browser, MCP, and proxy versions against allowed and denied destinations, private targets, redirects, proxy failure, and bypass attempts. Verify per-request enforcement, connection paths, and visible manual sign-in. Clipboard and file transfer default off. Keep strict mode unavailable if the required boundary cannot be established; do not represent tested cases as a guarantee against a compromised host.

## First test-review checkpoint

Start with slice 1. The initial unit tests should make the distinctions between an individual finding, the overall result, interruption, cleanup, and human review explicit. Present the actual tests and their failing run to the owner for a coverage review before implementing the model. Exact APIs and file organization can follow the tests.

## Decisions to resolve during work

CLI syntax and configuration format, schema and migration strategy, exact MCP transport, provider SDK choice, evidence capture mechanisms, model request parameters, default limits, and the strict-mode enforcement mechanism are not yet fixed. Choose them as their slices are reached and update the design when the choice affects agreed behavior.
