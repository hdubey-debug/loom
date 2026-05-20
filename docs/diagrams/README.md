# Loom architecture diagrams

The four PNGs in this directory are rendered from
[`_render.py`](_render.py) via matplotlib. The Python script is the
source of truth — adjust coordinates / labels there and re-run to
regenerate.

```bash
python docs/diagrams/_render.py
```

Visual language used across all four diagrams:

- **Rectangles** — modules / state slots (blue).
- **Rounded rectangles** — events (orange).
- **Diamonds** — decision points (gold).
- **Solid arrows** — control flow / call edges.
- **Dashed arrows** — labelled reads, "effect applied" follow-ups.

| File | Used in README section |
|------|------------------------|
| `architecture.png` | §4 Architecture |
| `lease-lifecycle.png` | §12 Leases |
| `control-action-flow.png` | §11 Capabilities + Control Actions |
| `compaction-flow.png` | §14 Context compaction |
