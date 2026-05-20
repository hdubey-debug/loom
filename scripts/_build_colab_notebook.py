"""Build examples/colab_demo.ipynb from a single source file.

Usage::

    python scripts/_build_colab_notebook.py

This script is the canonical source of truth for the Colab tutorial.
Edit the ``CELLS`` list, re-run to regenerate the .ipynb. Keeping
cells in Python here (rather than in JSON) makes the diffs reviewable.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "examples" / "colab_demo.ipynb"


def md(*lines: str) -> dict:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": ("\n".join(lines)).splitlines(keepends=True),
    }


def code(*lines: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": ("\n".join(lines)).splitlines(keepends=True),
    }


CELLS = [
    # ------------------------------------------------------------------
    # Part 0 — Setup
    # ------------------------------------------------------------------
    md(
        "# 🪡 Loom — kernel tour (v0.3)",
        "",
        "**What you'll build:** a 3-agent chatroom that demonstrates the v0.3 "
        "subsystems end-to-end — capabilities, control actions, leases, "
        "context compaction, slash commands — and finishes with `/summarize` "
        "on a real OpenAI + Gemini conversation.",
        "",
        "**How this notebook is structured:**",
        "",
        "1. Setup + install.",
        "2. Mental model — what Loom is and isn't.",
        "3. Mock-agent policy tour (no API keys needed).",
        "4. Custom policies + v0.3 hooks.",
        "5. v0.3 power features — capabilities, slash commands, compaction.",
        "6. Bus introspection bonus.",
        "7. Live LLM chat with real APIs.",
        "8. Where to go next.",
        "",
        "_Tested under both Colab and local Jupyter; mock-agent halves run "
        "without API keys, live-LLM half is guarded behind `have_openai` / "
        "`have_gemini` checks._",
    ),
    md(
        "## 0. Install",
        "",
        "Loom has no runtime dependencies — Python stdlib only. The `pip "
        "install` line below works on Colab; for a local install, "
        "`pip install -e .` from the repo root.",
        "",
        "> **Colab caveat**: if you change the install, restart the runtime "
        "before importing.",
    ),
    code(
        "!pip install -q git+https://github.com/hdubey-debug/loom.git",
    ),
    # ------------------------------------------------------------------
    # Part 1 — Mental model
    # ------------------------------------------------------------------
    md(
        "## 1. What is Loom?",
        "",
        "Loom is a **kernel**, not a framework. You bring agents (each is "
        "just a callable that takes a prompt and returns text) and pick a "
        "routing policy; Loom owns the hard parts:",
        "",
        "- **Race-safe turn taking** via typed *leases* — only one speaker "
        "  can hold a USER_TURN lease for a given trigger at a time.",
        "- **Pluggable policies** — `plan_user_turn(state, user_event)` is "
        "  the single extension hook. Pure callback. No I/O, no bus posts.",
        "- **State as effects** — every kernel mutation flows through a "
        "  registered control action + versioned reducer. Replay is "
        "  deterministic.",
        "- **Capabilities** — 33-verb typed registry that gates which agent "
        "  may run which control action (`SET_TOPIC`, `SET_ANCHOR`, etc.).",
        "- **Context compaction** — rolling, lineage-preserving summaries "
        "  per `(room_id, thread_id, actor_id)` scope. Triggered by policy "
        "  pressure or the user's `/summarize` command.",
        "",
        "Public surface fits in nine concepts (Agent · Policy · Bus · Kernel "
        "· Lease · Capability · Control Action · Slash Command · ContextScope). "
        "We'll use most of them below.",
    ),
    md(
        "## 2. Mini-architecture diagram",
        "",
        "```mermaid",
        "flowchart TD",
        "    User[User / prompt_fn] -->|post| Room[LoomRoom facade]",
        "    Room --> Session[LoomSession]",
        "    Session --> Kernel[Kernel<br/>bus · coord · state · effects · leases]",
        "    Session --> Policy[Policy<br/>plan_user_turn]",
        "    Session --> Agents[Agents<br/>your LLM clients]",
        "    Slash[Slash commands] -->|propose| Kernel",
        "    Kernel <-->|state read| Policy",
        "    Kernel <-->|stream| Agents",
        "```",
        "",
        "Full PNG version: `docs/diagrams/architecture.png` in the repo.",
    ),
    md(
        "## 3. Glossary",
        "",
        "| Concept | One-liner |",
        "|---|---|",
        "| **Agent** | Your LLM client. `id` + `stream(prompt)`. |",
        "| **Policy** | `plan_user_turn` callback. Decides who speaks. |",
        "| **Bus** | Append-only event log. Authoritative. |",
        "| **Lease** | Typed permit (`LeaseKind` discriminator, 6 kinds). |",
        "| **Capability** | Verb a participant may request (33 in v0.3). |",
        "| **Control Action** | Typed state mutation via propose→lease→effect→reduce. |",
        "| **Slash Command** | Human-root-action surface (user bypass per P15). |",
        "| **ContextScope** | `(room_id, thread_id, actor_id)`. Compaction partition key. |",
    ),
    # ------------------------------------------------------------------
    # Part 2 — Mock-agent policy tour
    # ------------------------------------------------------------------
    md(
        "## 4. Mock agents — three scripted personas",
        "",
        "Each agent is a function `prompt -> text`. We mark them as personas "
        "so Loom can render them in the prompt preamble.",
    ),
    code(
        "from loom import LoomRoom, agent_from_send",
        "",
        "",
        "def alice_send(prompt: str) -> str:",
        "    return ('Alice: I would start by stating the question — what '",
        "            'does success look like for this turn?')",
        "",
        "",
        "def bob_send(prompt: str) -> str:",
        "    return ('Bob: Agreed. I would add: name the constraint that is '",
        "            'binding us, then enumerate the next two moves.')",
        "",
        "",
        "def carol_send(prompt: str) -> str:",
        "    return ('Carol: I am skeptical of premature consensus. Let me '",
        "            'sketch a counter-example before we commit.')",
        "",
        "",
        "alice = agent_from_send('alice', alice_send, persona='planner')",
        "bob = agent_from_send('bob', bob_send, persona='critic')",
        "carol = agent_from_send('carol', carol_send, persona='skeptic')",
        "",
        "",
        "def show(result):",
        "    \"\"\"Pretty-print the messages from a TurnResult.\"\"\"",
        "    print(f'closed_reason={result.closed_reason}  '",
        "          f'routing={result.routing_case}  '",
        "          f'elapsed={result.elapsed_s:.3f}s')",
        "    for m in result.messages:",
        "        print(f'  {m.sender}: {m.body}')",
    ),
    md(
        "## 5. `OpenChatPolicy` — broadcast to everyone",
        "",
        "The simplest non-trivial policy: every user post fans out to every "
        "active capable participant. Useful as a baseline.",
    ),
    code(
        "from loom import OpenChatPolicy",
        "",
        "with LoomRoom(agents=[alice, bob, carol], policy=OpenChatPolicy()) as room:",
        "    result = room.post_and_wait('what is the most important question to ask?')",
        "    show(result)",
    ),
    md(
        "## 6. `SingleResponderPolicy` — one canonical voice",
        "",
        "Route every user post to one configured agent. Great for the "
        "\"talk to one assistant\" pattern (Q&A, support).",
    ),
    code(
        "from loom import SingleResponderPolicy",
        "",
        "with LoomRoom(agents=[alice, bob, carol],",
        "              policy=SingleResponderPolicy('bob')) as room:",
        "    result = room.post_and_wait('which of you is in charge here?')",
        "    show(result)",
    ),
    md(
        "## 7. `RoundRobinPolicy` — strict rotation",
        "",
        "One speaker per user post, rotating through a fixed order. Ideal "
        "for 20-questions, debates, classroom turn-taking.",
    ),
    code(
        "from loom import RoundRobinPolicy",
        "",
        "with LoomRoom(agents=[alice, bob, carol],",
        "              policy=RoundRobinPolicy(order=['alice', 'bob', 'carol'])) as room:",
        "    for q in ['question 1', 'question 2', 'question 3']:",
        "        result = room.post_and_wait(q)",
        "        show(result)",
        "        print()",
    ),
    md(
        "## 8. `DefaultPolicy` — `@-mention` for direct routing",
        "",
        "Loom's full classifier — vocative addressing, direct mention, "
        "acknowledgement, broadcast fallback. Production-grade.",
    ),
    code(
        "from loom import DefaultPolicy",
        "",
        "with LoomRoom(agents=[alice, bob, carol], policy=DefaultPolicy()) as room:",
        "    result = room.post_and_wait('@bob what did you think of alice'\"'\"'s idea?')",
        "    show(result)",
    ),
    # ------------------------------------------------------------------
    # Part 3 — Custom policies + v0.3 hooks
    # ------------------------------------------------------------------
    md(
        "## 9. Writing a custom policy in v0.3",
        "",
        "A policy subclasses `ConversationPolicy`. The required hook is "
        "`plan_user_turn`. Optional hooks any policy can override:",
        "",
        "- `prompt_sections(state, participant_id, trigger_event)` — extra "
        "  preamble blocks rendered with a `<<<NAME>>>` header.",
        "- `should_post_response(body, state, participant_id)` — veto a "
        "  draft after the kernel's idle/IoU filters pass.",
        "- `charter_text(state)` — extra system-preamble text after the "
        "  kernel charter.",
        "- `dead_letter_target(state, removed_participant)` — pick the "
        "  reroute target when an `@`-mentioned agent is removed mid-turn.",
        "- **v0.3** `control_interest_for_participant(state, pid)` — declare "
        "  which control events the participant cares about.",
        "",
        "Below: a `BrevityPolicy` that subclasses `OpenChatPolicy` and vetos "
        "any reply longer than 80 characters via `should_post_response`.",
    ),
    code(
        "class BrevityPolicy(OpenChatPolicy):",
        "    name = 'brevity'",
        "    MAX_CHARS = 80",
        "",
        "    def should_post_response(self, body, state, participant_id):",
        "        if len(body) > self.MAX_CHARS:",
        "            return False  # veto",
        "        return True",
        "",
        "",
        "with LoomRoom(agents=[alice, bob, carol], policy=BrevityPolicy()) as room:",
        "    result = room.post_and_wait('keep it short — what is the next step?')",
        "    show(result)",
    ),
    md(
        "## 10. Custom `LeaseCheck` — gate USER_TURN leases",
        "",
        "v0.3 lets you plug a custom check into `RoomConfig.lease_checks`. "
        "Each check exposes `applies_to: frozenset[LeaseKind]` so it only "
        "fires for the matching kind.",
        "",
        "Below: a `MuteParticipant` check that blocks every USER_TURN lease "
        "for one named participant — useful for soft-mute UIs.",
    ),
    code(
        "from loom import RoomConfig",
        "from loom.contracts import LeaseCheckResult, PASSED",
        "from loom.kernel.leases import LeaseKind",
        "",
        "",
        "class MuteParticipant:",
        "    name = 'mute_participant'",
        "    applies_to = frozenset({LeaseKind.USER_TURN})",
        "",
        "    def __init__(self, muted_id: str) -> None:",
        "        self.muted_id = muted_id",
        "",
        "    def check(self, *, holder, trigger_event_id, is_direct_mention, coordinator):",
        "        del trigger_event_id, is_direct_mention, coordinator",
        "        if holder == self.muted_id:",
        "            return LeaseCheckResult(False, f'muted:{self.muted_id}')",
        "        return PASSED",
        "",
        "",
        "config = RoomConfig(lease_checks=(MuteParticipant('alice'),))",
        "with LoomRoom(agents=[alice, bob, carol],",
        "              policy=OpenChatPolicy(),",
        "              room_config=config) as room:",
        "    result = room.post_and_wait('any thoughts?', timeout=2.0)",
        "    show(result)",
        "",
        "    # Inspect denials.",
        "    print()",
        "    for ev in room._session.bus.snapshot():",
        "        body = ev.body if isinstance(ev.body, dict) else {}",
        "        if body.get('control_type') == 'lease_denied':",
        "            print(f'  lease_denied: check={body[\"check_name\"]!r} '",
        "                  f'holder={body[\"holder\"]!r} reason={body[\"deny_reason\"]!r}')",
    ),
    # ------------------------------------------------------------------
    # Part 4 — v0.3 power features
    # ------------------------------------------------------------------
    md(
        "## 11. Capabilities + Control Actions",
        "",
        "Every kernel state mutation in v0.3 goes through one pipeline:",
        "",
        "```",
        "propose → CONTROL_ACTION lease → CapabilityCheck → effect → reducer",
        "```",
        "",
        "The user has a special bypass (P15) — when `proposer_id == 'user'`, "
        "the capability gate is skipped. So `/topic` works without granting "
        "yourself anything.",
        "",
        "Below: we run `/topic` as user (works immediately), then try to do "
        "the same as the `claude` agent (denied, no capability), then grant "
        "and retry.",
    ),
    code(
        "from loom.slash_commands import dispatch_slash_command",
        "from loom.kernel.capabilities import CapabilityName",
        "from loom.kernel.effects import CapabilityGrantedEffect",
        "from loom.kernel.room import ParticipantInfo",
        "",
        "claude_agent = agent_from_send('claude', lambda p: 'claude: noted.', persona='orchestrator')",
        "",
        "with LoomRoom(agents=[claude_agent],",
        "              policy=OpenChatPolicy(),",
        "              topic='kick-off') as room:",
        "    coord = room._session.coordinator",
        "    # Register \"user\" as a pseudo-participant so the universal",
        "    # _ParticipantRegisteredCheck passes for proposer_id='user'.",
        "    coord.register_participant(ParticipantInfo(id='user', capable=False))",
        "",
        "    # 1. User /topic — bypass.",
        "    r1 = dispatch_slash_command(coord, '/topic recursion lesson')",
        "    print(f'/topic user: granted={r1.granted} new_topic={room.topic!r}')",
        "",
        "    # 2. Agent SET_TOPIC without cap.",
        "    r2 = coord.propose_control_action(",
        "        proposer_id='claude', action_name='SET_TOPIC',",
        "        params={'topic': 'should fail'})",
        "    print(f'agent SET_TOPIC (no cap): granted={r2.granted} reason={r2.reason}')",
        "",
        "    # 3. Kernel-side grant (the GRANT_CAPABILITY action handler is",
        "    # on the v0.4 list; we apply the effect directly here).",
        "    with coord._lock:",
        "        coord._apply_effect(CapabilityGrantedEffect(",
        "            grant_id='g1', grantee_id='claude',",
        "            capability=CapabilityName.SET_TOPIC.value, grantor_id='user'))",
        "    print('kernel-side grant: SET_TOPIC → claude')",
        "",
        "    # 4. Retry — granted.",
        "    r4 = coord.propose_control_action(",
        "        proposer_id='claude', action_name='SET_TOPIC',",
        "        params={'topic': 'recursion deep dive'})",
        "    print(f'agent SET_TOPIC (with cap): granted={r4.granted} new_topic={room.topic!r}')",
    ),
    md(
        "## 12. Slash commands tour",
        "",
        "The eight built-in slash commands and what they propose:",
        "",
        "| Command | Action |",
        "|---|---|",
        "| `/grant <pid> <CAP>` | `GRANT_CAPABILITY` (handler queued for v0.4) |",
        "| `/revoke <gid>` | `REVOKE_CAPABILITY` (handler queued for v0.4) |",
        "| `/topic <text>` | `SET_TOPIC` ✅ |",
        "| `/anchor <pid>` | `SET_ANCHOR` ✅ |",
        "| `/responder <pid>` | `SET_DEFAULT_RESPONDER` ✅ |",
        "| `/floor <pid> ...` | `GRANT_FLOOR` (handler queued for v0.4) |",
        "| `/policy <name>` | `SWITCH_POLICY` (handler queued for v0.4) |",
        "| `/summarize [thread=]` | (Path B compaction) ✅ |",
        "",
        "Below: run the three that ship with reducers in v0.3 and inspect "
        "the resulting state deltas.",
    ),
    code(
        "with LoomRoom(agents=[alice, bob, carol],",
        "              policy=OpenChatPolicy(),",
        "              topic='start') as room:",
        "    coord = room._session.coordinator",
        "    coord.register_participant(ParticipantInfo(id='user', capable=False))",
        "",
        "    for cmd in ['/topic curriculum design',",
        "                '/anchor alice',",
        "                '/responder bob']:",
        "        result = dispatch_slash_command(coord, cmd)",
        "        print(f'{cmd:<30}  granted={result.granted}')",
        "",
        "    print()",
        "    print(f'topic   = {coord.state.topic!r}')",
        "    print(f'anchor  = {coord.state.anchor_id!r}')",
        "    print(f'default = {coord.state.default_responder_id!r}')",
    ),
    md(
        "## 13. Context compaction — `/summarize` (Path B)",
        "",
        "Loom rooms grow without bound by default. The v0.3.x compaction "
        "subsystem provides a *view-layer* rolling summary so prompts stay "
        "within model context windows. **The journal is never rewritten** — "
        "it remains the authoritative ledger.",
        "",
        "Two trigger paths converge on the same `SUMMARIZATION` lease:",
        "",
        "- **Path A** — policy detects pressure threshold, calls "
        "  `schedule_summarization(...)`.",
        "- **Path B** — user runs `/summarize`, calls "
        "  `request_summarization(...)`.",
        "",
        "Below: we seed 12 chat events, dispatch `/summarize`, then hand-roll "
        "a `SummaryRecord` and commit it via `submit_summary_proposed`. In a "
        "real room the summariser actor would produce the record after an "
        "LLM call.",
    ),
    code(
        "from loom.kernel import events as ev",
        "from loom.kernel.context import ContextScope, SummaryRecord",
        "",
        "with LoomRoom(agents=[alice], policy=OpenChatPolicy(), topic='long debate') as room:",
        "    coord = room._session.coordinator",
        "    bus = room._session.bus",
        "    coord.register_participant(ParticipantInfo(id='user', capable=False))",
        "    with coord._lock:",
        "        coord.state.set_default_summarizer('alice')",
        "",
        "    # 1. Seed 12 chat events.",
        "    for i in range(12):",
        "        bus.post(ev.chat(sender='alice', body=f'turn {i}: ...'))",
        "    bus_len = len(bus.snapshot())",
        "    print(f'bus length: {bus_len}')",
        "",
        "    # 2. /summarize (Path B).",
        "    sched = dispatch_slash_command(coord, '/summarize')",
        "    print(f'/summarize scheduled={sched.scheduled} lease={sched.lease_id}')",
        "",
        "    # 3. Hand-roll + submit the SummaryRecord.",
        "    record = SummaryRecord(",
        "        summary_id='sum-1',",
        "        scope=ContextScope(room_id=coord.config.room_id),",
        "        covers_event_range=(0, bus_len - 1),",
        "        text='the participants debated curriculum design.',",
        "        retained_event_ids=(0, bus_len - 1),",
        "        input_event_ranges=((0, bus_len - 1),),",
        "        model_id='demo-model',",
        "        prompt_hash='hash-demo',",
        "        summarizer_id='alice',",
        "        proposed_at_event_id=bus_len,",
        "    )",
        "    result = coord.submit_summary_proposed(record)",
        "    print(f'committed={result.committed} reason={result.reason}')",
        "",
        "    # 4. Active summary table.",
        "    print()",
        "    print('active_summary_by_scope:')",
        "    for s, sid in coord.kernel_state.context.active_summary_by_scope.items():",
        "        print(f'  {s.as_tuple()} -> {sid}')",
    ),
    md(
        "## 14. The view-layer effect of compaction",
        "",
        "After a `summary_committed` event lands, the next "
        "`build_prompt(state, actor_id, ...)` for any participant in the "
        "scope reads `active_summary_by_scope[scope]` and renders the "
        "committed text under a `<<<PRIOR ROOM SUMMARY>>>` block in the "
        "system preamble.",
        "",
        "The journal still contains the full event log — only the live "
        "prompt view shrinks.",
    ),
    code(
        "# Demonstrate by inspecting the room's kernel_state.context.",
        "# (We can't easily render a real prompt without a live actor",
        "# context — but we can show the data the prompt builder reads.)",
        "with LoomRoom(agents=[alice], policy=OpenChatPolicy()) as room:",
        "    coord = room._session.coordinator",
        "    ctx = coord.kernel_state.context",
        "    print(f'summaries dict keys: {list(ctx.summaries.keys())}')",
        "    print(f'active_summary_by_scope: {dict(ctx.active_summary_by_scope)}')",
        "    print(f'supersession_edges: {dict(ctx.supersession_edges)}')",
        "    print(f'failure_count: {dict(ctx.failure_count)}')",
    ),
    # ------------------------------------------------------------------
    # Part 5 — Bus introspection
    # ------------------------------------------------------------------
    md(
        "## 15. Bonus — peek at the bus",
        "",
        "The bus is the authoritative event log. Every chat post, every "
        "control event, every lease grant + denial — all of it lives on the "
        "bus, in order.",
    ),
    code(
        "with LoomRoom(agents=[alice, bob, carol], policy=OpenChatPolicy()) as room:",
        "    room.post_and_wait('one sentence each, please')",
        "    bus = room._session.bus",
        "    for ev in bus.snapshot():",
        "        body = ev.body if isinstance(ev.body, dict) else {}",
        "        ctype = body.get('control_type', '')",
        "        label = ctype or ev.kind",
        "        detail = ''",
        "        if ev.kind == 'chat':",
        "            detail = f' {ev.sender!r}: {ev.body[:50]!r}'",
        "        elif ctype in ('control_action_proposed', 'control_action_applied'):",
        "            detail = f' action={body.get(\"action_name\")!r}'",
        "        print(f'  [{ev.id:>3}] {label:<24}{detail}')",
    ),
    # ------------------------------------------------------------------
    # Part 6 — Live LLM chat
    # ------------------------------------------------------------------
    md(
        "---",
        "",
        "# Part 2 — Live chat with real APIs",
        "",
        "Below this line we install provider SDKs and wire up real "
        "OpenAI + Gemini agents. **You need API keys to run this half.** The "
        "earlier sections (1–15) work without any keys.",
    ),
    md(
        "## 16. Install provider SDKs",
    ),
    code(
        "!pip install -q openai google-generativeai",
    ),
    md(
        "## 17. Add your API keys",
        "",
        "Run the cell below; you'll be prompted in-browser for each key. "
        "Press ENTER to skip a provider — its agents won't be created.",
    ),
    code(
        "import getpass",
        "import os",
        "",
        "for name, label in [('OPENAI_API_KEY', 'OpenAI'),",
        "                    ('GEMINI_API_KEY', 'Gemini')]:",
        "    if not os.getenv(name):",
        "        val = getpass.getpass(f'{label} key (blank to skip): ')",
        "        if val:",
        "            os.environ[name] = val.strip()",
        "",
        "have_openai = bool(os.getenv('OPENAI_API_KEY'))",
        "have_gemini = bool(os.getenv('GEMINI_API_KEY'))",
        "print(f'OpenAI: {have_openai} | Gemini: {have_gemini}')",
    ),
    md(
        "## 18. Real agents — Planner / Critic / Synthesiser",
        "",
        "Three personas, each backed by a real provider when its key is set. "
        "We wire them up via `agent_from_send` — the kernel calls "
        "`send(prompt)` and waits for the string reply.",
    ),
    code(
        "def make_openai_agent(agent_id, system, *, model='gpt-4o-mini', persona=''):",
        "    from openai import OpenAI",
        "    client = OpenAI()",
        "",
        "    def send(prompt: str) -> str:",
        "        rsp = client.chat.completions.create(",
        "            model=model,",
        "            messages=[",
        "                {'role': 'system', 'content': system},",
        "                {'role': 'user', 'content': prompt},",
        "            ],",
        "        )",
        "        return rsp.choices[0].message.content or ''",
        "",
        "    return agent_from_send(agent_id, send, persona=persona)",
        "",
        "",
        "def make_gemini_agent(agent_id, system, *, model='gemini-1.5-flash', persona=''):",
        "    import google.generativeai as genai",
        "    genai.configure(api_key=os.environ['GEMINI_API_KEY'])",
        "    m = genai.GenerativeModel(model, system_instruction=system)",
        "",
        "    def send(prompt: str) -> str:",
        "        rsp = m.generate_content(prompt)",
        "        return getattr(rsp, 'text', '') or ''",
        "",
        "    return agent_from_send(agent_id, send, persona=persona)",
        "",
        "",
        "real_agents = []",
        "if have_openai:",
        "    real_agents.append(make_openai_agent(",
        "        'planner',",
        "        'You are the Planner — keep replies brief and structured.',",
        "        persona='planner'))",
        "    real_agents.append(make_openai_agent(",
        "        'critic',",
        "        'You are the Critic — name the constraint that is binding.',",
        "        persona='critic',",
        "        model='gpt-4o-mini'))",
        "if have_gemini:",
        "    real_agents.append(make_gemini_agent(",
        "        'synthesiser',",
        "        'You are the Synthesiser — produce a one-sentence summary at the end.',",
        "        persona='synthesiser'))",
        "",
        "print(f'real_agents: {[a.id for a in real_agents]}')",
    ),
    md(
        "## 19. Live chat — pick a policy, run a turn, then `/summarize`",
        "",
        "Edit `POLICY` to swap policies between runs. After three user "
        "posts, we run `/summarize` to compact the conversation.",
    ),
    code(
        "if real_agents:",
        "    POLICY = OpenChatPolicy()  # ← edit me",
        "",
        "    with LoomRoom(agents=real_agents, policy=POLICY,",
        "                  topic='design review') as room:",
        "        coord = room._session.coordinator",
        "        coord.register_participant(ParticipantInfo(id='user', capable=False))",
        "        with coord._lock:",
        "            coord.state.set_default_summarizer(real_agents[0].id)",
        "",
        "        questions = [",
        "            'pick a topic worth a 3-turn debate and propose it',",
        "            'critic, attack the strongest version of that topic',",
        "            'synthesiser, give us a one-sentence summary',",
        "        ]",
        "",
        "        for q in questions:",
        "            print(f'\\nUSER> {q}')",
        "            result = room.post_and_wait(q, timeout=30.0)",
        "            for m in result.messages:",
        "                print(f'  {m.sender}: {m.body[:200]}{\"...\" if len(m.body) > 200 else \"\"}')",
        "",
        "        # Trigger /summarize on the room scope. Without a real",
        "        # off-lock summariser we won't get a real LLM-produced",
        "        # summary, but we will see the SUMMARIZATION lease land.",
        "        sched = dispatch_slash_command(coord, '/summarize')",
        "        print(f'\\n/summarize scheduled={sched.scheduled} '",
        "              f'lease={sched.lease_id} denial={sched.denial_reason}')",
        "else:",
        "    print('No API keys set — skipping live chat. Re-run cell 17 to add keys.')",
    ),
    # ------------------------------------------------------------------
    # Part 7 — Where next
    # ------------------------------------------------------------------
    md(
        "## 20. Where next?",
        "",
        "**v0.4 in planning** — what's coming:",
        "",
        "- **Slash command → action bridge** for `/grant`, `/revoke`, "
        "  `/floor`, `/policy` (parsers ship in v0.3; handlers land in v0.4).",
        "- **Tool subsystem** — structured `tool_call` / `tool_result` "
        "  event kinds, tool channel visibility, tools-as-participants.",
        "- **Async / off-lock policies** for slow LLM-backed routing.",
        "- **Automatic restart-recovery** from the journal.",
        "- **Controller mechanism** — privileged participants open chained "
        "  user turns. The CEO / orchestrator pattern, structurally.",
        "",
        "**Read the doctrine**: `docs/internal/study/` has the full design "
        "dialogue behind each v0.3 principle — 22 principles, locked at "
        "v0.3.0.",
        "",
        "**Run the example scripts**: `examples/control_actions_demo.py`, "
        "`examples/summarize_demo.py`, `examples/custom_lease_check.py` "
        "all run from the repo root.",
    ),
]


def main():
    notebook = {
        "cells": CELLS,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {
                "name": "python",
                "version": "3.11",
            },
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    OUT.write_text(json.dumps(notebook, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {OUT.relative_to(REPO)} — {len(CELLS)} cells")
    print("note: run `ruff format examples/colab_demo.ipynb` for canonical formatting")


if __name__ == "__main__":
    main()
