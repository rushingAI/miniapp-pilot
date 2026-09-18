# MiniApp Pilot

Read README.md before changes. Read docs/architecture.md before execution or lifecycle changes, and docs/preparation.md before preparation changes.

- Native Harness is the only execution architecture. Coordinator / Suite Registry owns identity and lifecycle.
- Keep device tools app-agnostic; example-specific code belongs in examples/.
- For real-device actions, read device-mcp/skills/mobile-operation/SKILL.md and observe, act, wait for stability, verify. Stop after three attempts without progress.
- Validate behavior through its owning interface. Add focused regressions before relying on a fix; run Python and Node suites before delivery.
- Evidence must be real and Suite-local. A model assertion or green unit test is not device E2E evidence.
- Keep runtime files outside source. Explicitly review public-files.json and run scripts/release_check.py when adding distributable files.
- Public examples use synthetic data. Check screenshots, workbook metadata, URLs and logs before sharing.
- APP_VERSION in device-mcp/agent/build_version.py is the only version source.
