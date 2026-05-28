---
name: tether
description: Maestro e2e test authoring CLI for React Native (Android + iOS).
---

# tether

Gives AI agents visibility into Android emulators and iOS simulators for writing and debugging Maestro e2e tests. Screenshots, element dumps with @refs, health checks, log streaming, and fast flow execution.

## Commands

```bash
tether doctor [--fix]       # Validate entire stack (adb, emulator, maestro)
tether boot                 # Ensure emulator/simulator is running
tether status               # Quick state check
tether install [path]       # Install app binary
tether launch [appId]       # Launch configured or explicit app
tether close [appId]        # Stop configured or explicit app
tether open-url <url>       # Open URL or deep link
tether open-url <url> --audit-run-id <id> --json  # Open URL and collect audit evidence
tether screen [path]        # Take screenshot (agent can view)
tether elements             # Dump visible UI elements with @refs
tether elements --json      # Machine-readable element dump
tether inspect              # Screenshot + elements + logs (JSON)
tether flow <path>          # Run single Maestro flow
tether smoke <dir>          # Run all flows in directory
tether progress [--clear]   # Show flow pass/fail history
tether last-error           # What failed last time
tether logcat [--follow]    # Filtered device logs
tether watch                # Auto-capture on UI changes
tether audit                # Read agent audit events
tether audit --runId <id>   # Filter audit events by agent run ID
tether audit --clear        # Clear the audit log
```

## Agent audit events

Apps can emit structured audit log lines with a project-configured prefix. The default prefix is `[agent-audit]` and the default run ID field is `runId`.

Use `tether open-url <url> --audit-run-id <id> --json` when deep-link transport and audit collection must be checked together. JSON output includes `auditEvents`, `auditTransport`, and `healthcheckObserved` when the project config defines a required healthcheck.

If `auditTransport` is `blocked`, do not treat missing target events as app behavior. First fix the sink or healthcheck path.

## Config (tether.json)

```json
{
  "platform": "android",
  "avd": "Pixel_XL_API_29",
  "appId": "com.myapp",
  "flows": {
    "defaults": ["auth/login", "home/feed"],
    "presets": {
      "auth": ["auth/login", "auth/signup"],
      "critical": ["auth/login", "checkout/payment"]
    }
  },
  "timeouts": { "boot": 90, "flow": 180, "screenshot": 10 },
  "audit": {
    "prefix": "[agent-audit]",
    "runIdField": "runId",
    "healthcheck": {
      "surface": "agentAudit",
      "name": "agentAudit.healthcheck",
      "required": true
    }
  }
}
```

For iOS:
```json
{
  "platform": "ios",
  "simulator": "iPhone-15-Pro",
  "appId": "com.myapp"
}
```

## Workflow

1. Run `tether doctor` to validate the stack
2. Run `tether boot` to start emulator/simulator
3. Optionally run `tether install <path>` and `tether launch` for app setup
4. Use `tether inspect` to see current state (screenshot + elements + logs)
5. Write Maestro flow YAML based on visible elements and @refs
6. Run `tether flow <path>` to test
7. Check `tether last-error` on failure, iterate
