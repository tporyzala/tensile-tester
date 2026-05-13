# Tensile tester repository

This repository contains mechanical, documentation, and experimental assets alongside the software stack.

The software and firmware implementation live under:

```text
src/
```

The source tree currently includes:

- Raspberry Pi FastAPI application
- SQLite persistence layer
- WebSocket/Plotly live telemetry UI
- Arduino Uno firmware for control, safety, telemetry, and button handling
- setup and protocol documentation

See [`src/README.md`](src/README.md) for software architecture, setup, transport selection, serial protocol, and firmware notes.

