"""Reusable capabilities -- a documented catalog of existing app/phases/* and app/*_client.py
functions that already do real, useful work and could become agent-callable tools later. See
registry.py.

Deliberately NOT a tool-calling framework: no schema validation, no execution wrapper, no
binding to any specific agent SDK's tool format. Just a stable, documented catalog other code
(including a future decision layer, whatever it turns out to be built on) can read. Building
the actual calling convention is a later, separate decision -- this step only inventories what
already exists and is worth exposing."""
