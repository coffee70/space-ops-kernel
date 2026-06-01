---
title: Local Stack
layer: kernel
audience: ai-engineer
topics:
  - local-stack
  - edge-proxy
  - validation
status: mvp
last_verified: 2026-06-01
---

# Local Stack

## Purpose

This doc defines the official local stack entrypoint and canonical validation commands.

## Applies To

The sibling checkout containing `space-ops-kernel`, `space-ops-platform`, and `space-ops-apps`.

## Core Concepts

### Sibling checkout layout

The expected workspace layout is:

```text
project/
  space-ops-kernel/
  space-ops-platform/
  space-ops-apps/
```

### Start the stack

From `space-ops-kernel`:

```bash
docker compose up -d --build
```

### Official browser entrypoint

Use:

```text
http://localhost:8080
```

This goes through the Layer 1 edge proxy.

### Frontend-only development

`http://localhost:3000` is for direct Mission Control frontend development. It is not the full-system entrypoint.

## Procedure

Start Compose from `space-ops-kernel`, open Mission Control through `http://localhost:8080`, and validate operator-facing paths through that origin.

## Do Not Assume

Do not use `localhost:3000` to validate full-system behavior. Some platform routes, runtime application routes, and same-origin API paths depend on the edge proxy.

## Validation

Canonical validation:

```bash
./scripts/validate-node.sh
./scripts/validate-playwright.sh smoke
```

## Failure Modes

If `localhost:3000` works but `localhost:8080` fails, investigate edge proxy, control-plane registration, and same-origin route wiring.

## Related Docs

- [Layer 1 Role](./layer-1-role.md)
- [Validation Gates](../platform/validation-gates.md)
