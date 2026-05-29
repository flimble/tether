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
tether open-url <url> --agent-run-id <id> --json  # Open URL and collect observability evidence
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
tether observability                # Read agent observability events
tether observability --runId <id>   # Filter observability events by agent run ID
tether observability --clear        # Clear the observability log
```

## Agent observability events

Apps can emit structured observability log lines with a project-configured prefix. The default prefix is `[agent-observability]` and the default run ID field is `runId`.

Use `tether open-url <url> --agent-run-id <id> --json` when deep-link transport and observability collection must be checked together. JSON output includes `observabilityEvents`, `observabilityTransport`, and `healthcheckObserved` when the project config defines a required healthcheck.

If `observabilityTransport` is `blocked`, do not treat missing target events as app behavior. First fix the sink or healthcheck path.

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
  "observability": {
    "prefix": "[agent-observability]",
    "runIdField": "runId",
    "healthcheck": {
      "surface": "agentObservability",
      "name": "agentObservability.healthcheck",
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

## Scope

Use `tether --help` for the short operating loop and command-choice guidance. This skill covers deeper observability, config, and Maestro YAML details.
