# Loom — documentation

User-facing docs in reading order:

1. [`writing-an-adapter.md`](writing-an-adapter.md) — wrap your LLM
   client as an `Agent`. The `Agent` protocol is structural; three
   bundled helpers (`agent_from_send`, `agent_from_stream`,
   `agent_from_object`) cover the common shapes.
2. [`writing-a-policy.md`](writing-a-policy.md) — write a custom
   `ConversationPolicy`. Covers the `plan_user_turn` contract, the
   purity boundary, and declarative state mutation through
   `UserTurnPlan`.
3. [`loom-ux-spec.md`](loom-ux-spec.md) — what users see at the
   console. The UX contract that all bundled policies satisfy.
4. [`security-model.md`](security-model.md) — sandboxing posture,
   prompt-injection assumptions, and what the kernel does and does
   not protect against.

Reference reading:

- [`../README.md`](../README.md) — quick start, public surface,
  threading model, persistence.
- [`../CHANGELOG.md`](../CHANGELOG.md) — version history.
- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — setup + the policy
  contract reminder.

The [`internal/`](internal/) directory holds project baselines
(coverage, mutation, perf) that are useful for maintainers and
reviewers but not part of the user-facing documentation.
