# Security

## Reporting a vulnerability

Do not put exploit details, credentials, customer files, or private observations in a public issue. Use the repository Security tab's **Report a vulnerability** option if it is available. This document does not assert that private reporting is enabled.

If no private reporting option or verified maintainer contact is available, open a minimal public issue requesting a confidential contact route, without disclosing the vulnerability. Wait for that route before sending sensitive details. In a confidential report, include the affected version/commit, platform, synthetic reproduction, impact, and relevant redacted logs.

## Execution boundary

`run` executes only the explicit command and arguments after `--`, using `shell=False`. On macOS/Linux it owns a new POSIX process group, enforces a per-invocation timeout, and limits captured stdout/stderr. It cleans up that group, including after a successful parent exit. Processes that escape into another session are outside that group control.

This runner is not a sandbox. A trusted adapter can read or change files, use inherited environment variables, access the network, and incur provider charges. Use an isolated environment with only the access your adapter requires. Windows execution is rejected before adapter startup; core validation, generation, and saved-output grading remain available.

## Data and artifacts

Frozen paths must resolve within the suite, and preflight checks schemas, hashes, and resource budgets before execution. The CLI checks the suite again after execution; adapter changes to frozen inputs invalidate grading. These checks protect benchmark integrity and are not a guarantee against a concurrently hostile filesystem.

Grading does not execute output, fetch external schema references, repair JSON, or call a model. The HTML report renders captured content as escaped text without scripts or external assets. Treat spreadsheets and adapter output as untrusted data even when they pass validation.

Observations and reports retain stdout/stderr and runtime metadata, including command arguments and absolute input paths from execution. Avoid secrets in argv and logs. Inspect artifacts before publishing them. The toolkit does not infer token usage or charges; runner usage is `null` and cost is unknown.
