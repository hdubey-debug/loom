# AIOS Architecture — Tree Map and Comparison with Loom

**Date:** 2026-05-11
**Subject system:** AIOS (agiresearch/AIOS), v0.2.2 era, commit `5de61c9`.
**Local clone:** `/mmfs1/scratch/jacks.local/hdubey/07-LLM/AIOS`
**Compared against:** Loom kernel v0.2 (this repo, refactor landed 2026-05-10).

This document captures a from-source architectural read of AIOS and contrasts
its design with Loom's. The AIOS tree below uses the same notation as Loom's
own internal architecture trees (`├─`, `└─`, `←` for inline descriptions,
indented pseudo-code blocks for hot loops). The two systems live at different
layers of the stack — AIOS is a *resource management substrate* (LLM,
memory, storage, tools, computer-use VM); Loom is a *conversation
orchestration substrate* (multi-agent turns, lease arbitration, journaled
events). They are complementary, not competitors.

---

## 1. AIOS architectural tree

```
FastAPI app (runtime/launch.py — uvicorn entrypoint, process-level lifecycle)
  ├─ app: FastAPI                            ← CORS open ("*") by default; flagged in code
  ├─ active_components: dict                  ← module-level mutable singleton dict
  │     {llm, memory, storage, tool, scheduler, factory} — initialized on import
  ├─ selected_llms: dict                      ← global "currently selected" model list
  ├─ PROC_DIR = Path("proc")                  ← per-execution JSON files (pseudo-process table)
  ├─ execute_request, SysCallWrapper, syscall_executor = useSysCall()
  │     ← module-level singleton SyscallExecutor; the actual kernel
  ├─ lifecycle helpers
  │   ├─ initialize_llm_cores(config) → LLMAdapter
  │   ├─ initialize_storage_manager(cfg) → StorageManager
  │   ├─ initialize_memory_manager(cfg, storage) → MemoryManager
  │   ├─ initialize_tool_manager() → ToolManager  (spawns mcp_server.py subprocess)
  │   ├─ initialize_scheduler(components, cfg)    ← branches on use_context_manager:
  │   │                                                True → RRScheduler; False → FIFOScheduler
  │   ├─ initialize_agent_factory(cfg) → {submit, await}
  │   ├─ initialize_components() / restart_kernel()
  │   └─ _ensure_initialized()                ← fires on first import (uvicorn worker boot)
  └─ HTTP endpoints (Pydantic-validated)
      ├─ POST /query                          ← THE main port; agent_name + query_type + query_data
      │     async → asyncio.to_thread(execute_request, agent_name, query)
      │     branches on LLMQuery | ToolQuery | StorageQuery | MemoryQuery
      ├─ POST /agents/submit                  ← AgentFactory.submit; returns execution_id
      ├─ GET  /agents/{id}/status             ← await Future result
      ├─ GET  /agents/ps                      ← list proc/*.json files
      ├─ GET  /status, /core/status           ← component liveness
      ├─ POST /core/refresh                   ← config.refresh() + restart_kernel()
      ├─ POST /core/cleanup                   ← reverse-dependency stop sequence
      ├─ POST /user/select/llms               ← writes global selected_llms
      ├─ GET  /core/llms/list                 ← enumerate configured models
      ├─ GET  /get/mcp/server                 ← path to mcp_server_script_path
      └─ POST /core/config/update             ← writes api_key into config.yaml on disk

SyscallExecutor (aios/syscall/syscall.py — the central dispatcher; module singleton)
  ├─ id, id_lock                              ← monotonically increasing syscall PID
  ├─ context_injector: ContextInjector|None   ← wired only when memory.provider == "mem0"
  ├─ conversation_extractor: ConversationExtractor|None
  ├─ create_syscall(agent_name, query)        ← dispatches on isinstance() of Cerebrum Query types
  │     LLMQuery → LLMSyscall
  │     StorageQuery → StorageSyscall
  │     MemoryQuery → MemorySyscall
  │     ToolQuery → ToolSyscall
  └─ API
      ├─ _execute_syscall(agent_name, query)  ← the blocking primitive
      │     while True:
      │       syscall = create_syscall(...)
      │       syscall.set_status("active"); set_created_time(now); set_response(None)
      │       set_source / set_pid (monotonic id)
      │       global_<type>_req_queue_add_message(syscall)   ← push onto queue.Queue
      │       syscall.start()    ← spawns OS thread
      │       syscall.join()     ← blocks until scheduler fills event
      │       completed_response = syscall.get_response()
      │       if syscall.get_status() == "done": break
      │     # loop has no escape if scheduler never sets status; no timeout
      │     return {response, start_times, end_times, waiting_times, turnaround_times}
      ├─ execute_llm_syscall(agent_name, query)
      ├─ execute_storage_syscall(agent_name, query)
      ├─ execute_memory_syscall(agent_name, query)
      ├─ execute_tool_syscall(agent_name, query)
      ├─ COMPOSITE MACROS (multi-syscall orchestrations)
      │   ├─ execute_file_operation(agent_name, query)
      │   │     LLM parses NL → storage tool calls → executes each → LLM summarizes
      │   │     (3× LLM round-trips per file op)
      │   ├─ execute_memory_content_analyze(agent_name, query) → {keywords, context, tags}
      │   │     LLM-extracts JSON metadata for a memory; brittle JSON parsing with fallbacks
      │   ├─ execute_memory_evolve(query, similar_memories) → (query, evolved_memories)
      │   │     LLM-decides whether to merge new memory with neighbors
      │   └─ execute_request(agent_name, query) ← the FastAPI dispatch entry
      │       ├─ LLMQuery + action_type:
      │       │   ├─ "chat" | "chat_with_tool_call_output":
      │       │   │     pre:  context_injector.inject(agent_name, query)   if wired
      │       │   │     call: execute_llm_syscall(...)
      │       │   │     post: conversation_extractor.extract_async(...) (daemon thread)
      │       │   ├─ "chat_with_json_output": execute_llm_syscall
      │       │   ├─ "call_tool": LLM emits tool_calls → execute_tool_syscall
      │       │   └─ "operate_file": execute_file_operation
      │       ├─ ToolQuery: execute_tool_syscall
      │       ├─ MemoryQuery + operation_type:
      │       │   ├─ "add_agentic_memory": analyze → retrieve_raw → evolve → add → update neighbors
      │       │   └─ "add_memory" / "remove_memory" / "update_memory" / "retrieve_memory" / "get_memory"
      │       └─ StorageQuery: execute_storage_syscall

Syscall (aios/syscall/__init__.py — base class extends threading.Thread)
  ├─ fields: agent_name, query, event, pid, aid, status, response, time_limit,
  │          created_time, start_time, end_time, source, target, priority
  ├─ getters/setters for all fields (Java-bean style)
  ├─ run() override:
  │     self.event.wait()                     ← thread blocks immediately; never does real work
  └─ Subclasses (empty `pass` bodies — pure type tagging)
      ├─ LLMSyscall (syscall/llm.py)
      ├─ StorageSyscall (syscall/storage.py)  ← also exports storage_syscalls JSON schema list
      ├─ MemorySyscall (syscall/memory.py)
      └─ ToolSyscall (syscall/tool.py)

Global request queues (aios/hooks/stores/_global.py — module import side-effects)
  ├─ global_llm_req_queue, *_get_message, *_add_message, *_is_empty
  ├─ global_memory_req_queue, …
  ├─ global_storage_req_queue, …
  └─ global_tool_req_queue, …
       Each backed by stdlib `queue.Queue` (thread-safe, unbounded).
       getMessage uses block=True, timeout=0.1 → continuous polling on idle.
       NO authentication / membrane — any module can push or pull.

Hooks layer (aios/hooks/ — "React-named" Pydantic-validated factories)
  ├─ modules/
  │   ├─ llm.useCore(params) → LLMAdapter
  │   ├─ memory.useMemoryManager(params) → MemoryManager
  │   ├─ storage.useStorageManager(params) → StorageManager
  │   ├─ tool.useToolManager() → ToolManager
  │   ├─ scheduler.fifo_scheduler_nonblock(params) → FIFOScheduler
  │   ├─ scheduler.rr_scheduler_nonblock(params) → RRScheduler
  │   └─ agent.useFactory(params) → (submitAgent, awaitAgentExecution)
  ├─ stores/
  │   ├─ _global.py                            ← module-level singleton queue construction
  │   ├─ queue.REQUEST_QUEUE: dict[str → Queue]
  │   └─ processes.AGENT_PROCESSES: dict[pid → Future]
  ├─ types/ (Pydantic models, TypeAliases for Queue)
  │   ├─ llm.LLMParams, LLMRequestQueue = TypeAlias[Queue]
  │   ├─ scheduler.SchedulerParams (carries 4 manager + 4 get-message refs)
  │   └─ {memory,storage,tool,agent}.* mirror this shape
  └─ utils/
      └─ validate.py: @validate(ModelClass) decorator
           Silent fail mode: prints + returns None on Pydantic ValidationError.
           NOT a true DI container — no scope, lifecycle, or composition graph.

Schedulers (aios/scheduler/)
  ├─ base.BaseScheduler (ABC)
  │   ├─ fields: llm, memory_manager, storage_manager, tool_manager,
  │   │          get_<type>_syscall × 4, active, log_mode, processing_threads
  │   ├─ start_processing_threads([fn,...])    ← spawns one Thread per processor
  │   ├─ stop_processing_threads()             ← join all
  │   └─ abstract: process_<type>_requests × 4, start, stop
  ├─ fifo_scheduler.FIFOScheduler
  │   ├─ batch_interval = 1.0s (default)       ← only LLM queue is batched
  │   ├─ start() → 4 daemon threads:
  │   │     process_llm_requests: sleep(batch_interval); drain queue; LLM.execute_llm_syscalls(batch)
  │   │     process_memory_requests: get; _execute_syscall(MM.address_request)
  │   │     process_storage_requests: get; _execute_syscall(SM.address_request)
  │   │     process_tool_requests: get; _execute_syscall(TM.address_request)
  │   └─ _execute_syscall(syscall, executor, type):
  │         syscall.set_status("executing"); set_start_time(now)
  │         response = executor(syscall)
  │         syscall.set_response(response); syscall.event.set()
  │         syscall.set_status("done"); set_end_time(now)
  └─ rr_scheduler.RRScheduler
      ├─ time_slice = 1.0s (default)            ← used ONLY by SimpleContextManager, not by RR itself
      ├─ context_manager: SimpleContextManager  ← real KV-cache preemption (see below)
      ├─ start() → same 4 daemon threads
      └─ process_llm_requests calls _execute_batch_syscalls(single_syscall, ...)   ← probable bug:
            passes a single syscall where a list is expected (rr_scheduler.py:181)

LLMAdapter (aios/llm_core/adapter.py — multi-backend LLM router; 1090 LOC)
  ├─ llm_configs: list[LLMConfig]                ← parsed from config.yaml `llms.models`
  ├─ llms: list[str | HfLocalBackend | OpenAI]   ← backend handles
  ├─ available_llm_names: list[str]
  ├─ context_manager: SimpleContextManager|None  ← only if use_context_manager
  ├─ router: SequentialRouting | SmartRouting    ← selected via config.llms.router.strategy
  ├─ _dynamic_registration_lock: threading.Lock
  ├─ _ollama_hostname                            ← extracted from first ollama config
  ├─ _setup_api_keys()                           ← reads config OR env; writes os.environ
  ├─ _initialize_single_llm(LLMConfig):
  │     case "huggingface" | "hflocal" → HfLocalBackend (transformers in-proc)
  │     case "vllm" | "sglang" → OpenAI client pointed at hostname
  │     default → litellm string "<backend>/<name>"
  ├─ _query_ollama_available_models()            ← GET /api/tags
  ├─ _dynamic_register_ollama_model(name)        ← thread-safe runtime registration
  ├─ _handle_completion_error(err, model)        ← classifies into status_code via exception isinstance
  ├─ TWO-LEVEL PARALLELISM
  │   ├─ execute_llm_syscalls(batch)
  │   │     check_availability → optionally _dynamic_register_ollama_model →
  │   │     router.get_model_idxs(...) → group by model_idx →
  │   │     ThreadPoolExecutor (per-group worker) →
  │   │     inner ThreadPoolExecutor (per-syscall within group) →
  │   │     fan results back; set syscall.response/status/event
  │   └─ _process_batch_for_model(model_idx, tasks)
  ├─ execute_llm_syscall(model_idx, syscall, temperature)
  │     extract messages/tools/return_type/temperature/max_tokens
  │     if tools: slash_to_double_underscore(tools)
  │     completed, finished = _get_model_response(...)
  │     return (syscall, _process_response(completed, ...))
  ├─ _get_model_response(model_name, model, messages, tools, syscall, api_base, ...)
  │     if use_context_manager: SimpleContextManager.generate_response_with_interruption(...)
  │     elif isinstance(model, str): litellm.completion(model=..., messages=..., tools=..., ...)
  │     elif isinstance(model, OpenAI): model.chat.completions.create(...)
  │     elif isinstance(model, HfLocalBackend): model.generate(...)
  └─ _process_response(completed, finished, tools, model, return_type)
       decode_litellm_tool_calls → double_underscore_to_slash → wrap in LLMResponse

Backends (aios/llm_core/local.py)
  ├─ HfLocalBackend(model_name, max_gpu_memory, eval_device, hostname)
  │     loads AutoModelForCausalLM + AutoTokenizer; in-proc; supports hosted-HF via litellm
  ├─ VLLMLocalBackend(model_name, ...)           ← scaffold; in practice supplanted by OpenAI client
  └─ OllamaBackend(model_name, hostname)         ← scaffold; in practice litellm "ollama/..." string

Routing strategies (aios/llm_core/routing.py)
  ├─ RouterStrategy: Sequential | Smart
  ├─ SequentialRouting(llm_configs)
  │     get_model_idxs: pick first name in selected_llm_list that's available; index 0 fallback
  └─ SmartRouting(llm_configs, bootstrap_url, performance_requirement, n_similar)
      ├─ QueryStore (Chroma OR Qdrant collection "historical_queries")
      │   ├─ persist_directory: aios/llm_core/llm_router/
      │   ├─ bootstrap_from_drive(url): downloads JSONL via gdown if collection empty
      │   ├─ add_data: ingest queries + model output stats (correctness, token_length)
      │   ├─ query_similar(text, n=16)
      │   └─ predict(query, configs, n): returns (perf, len) matrices over candidate models
      ├─ get_model_idxs (per-query greedy): pick cheapest among models meeting perf threshold
      └─ optimize_model_selection_global (PuLP LP): minimize cost s.t. mean perf ≥ threshold

SimpleContextManager (aios/context/simple_context.py — the ONLY genuine "OS" piece)
  ├─ context_dict: dict[str(pid) → state]
  ├─ generate_response_with_interruption(model_name, model, messages, tools,
  │     message_return_type, temperature, max_tokens, pid, time_limit, response_format)
  │   ├─ if HF model: generate_with_time_limit_hf(...)
  │   │     loads context_data from context_dict[pid] if present
  │   │     manual token loop:
  │   │         while i < max_tokens:
  │   │             if elapsed > time_limit: finished = False; break
  │   │             outputs = model.model(generated_tokens, return_dict=True)
  │   │             next_token = sample/argmax
  │   │             generated_tokens = cat(..., next_token)
  │   │             past_key_values = outputs.past_key_values
  │   │             if next_token == EOS: finished = True; break
  │   │     if not finished: context_dict[pid] = {generated_tokens, past_key_values,
  │   │                                            start_idx, input_length}
  │   │     else: clear_context(pid)
  │   ├─ if tools: non-streaming call (single shot)
  │   └─ else: streaming text/JSON; accumulate deltas; stop at time_limit
  ├─ load_context(pid) → state | None
  └─ clear_context(pid)
  THIS IS THE GENUINE PREEMPTION MECHANISM: real KV-cache snapshot/restore
  for HF locals; degraded "accumulated text" for API-streamed models.

MemoryManager (aios/memory/manager.py — pluggable provider router)
  ├─ provider: MemoryProvider                    ← in-house | mem0 | zep
  ├─ known_user_ids: set[str]                    ← side-channel discovery of real users
  ├─ address_request(syscall)                     ← dispatches on operation_type
  ├─ Operations: add / remove / update / get / retrieve / retrieve_raw
  ├─ Personalization pipeline (wired in launch.py only for mem0)
  │   ├─ ContextInjector.inject(agent_name, query)  ← pre-LLM hook
  │   │     retrieves own + cross-agent shared memories (4× over-fetch),
  │   │     token-budget-truncates, injects as system message at index 0
  │   └─ ConversationExtractor.extract_async(agent, user, assistant, user_id)
  │         ← post-LLM hook; daemon thread; stores (user, assistant) as memory
  └─ Providers (aios/memory/providers/)
      ├─ in_house.InHouseProvider (ChromaDB or Qdrant)
      ├─ mem0.Mem0Provider (Mem0 cloud + ChromaDB persistence)
      └─ zep.ZepProvider (Zep cloud, free-tier graph.search; soft-delete)

  Privacy model (metadata-based filtering):
      memory.metadata = {owner_agent, user_id, sharing_policy: private|shared, memory_type}
      _apply_sharing_filter: same agent OR (shared + matching user_id)
      conversation memories: private, scoped by user_id
      profile / task memories: shareable cross-agent

StorageManager + LSFS (aios/storage/{storage.py, filesystem/lsfs.py})
  ├─ StorageManager: thin wrapper; address_request → LSFS method
  ├─ LSFS (LLM Semantic File System; ~476 LOC)
  │   ├─ file ops: create_file, sto_write, sto_delete, sto_create_directory
  │   ├─ retrieval: sto_retrieve(query_text, k, keywords)  ← vector similarity
  │   ├─ versioning: sto_rollback, get_file_history (Redis LIST per SHA256(path))
  │   ├─ sharing: sto_share → uploads to transfer.sh; cached in Redis 7d
  │   ├─ mounting: sto_mount (bulk index a directory tree)
  │   ├─ change handling: FileChangeHandler (watchdog observer)  ← scheduled but DISABLED
  │   ├─ per-file locks: dict[sha256 → threading.Lock] + meta-lock
  │   └─ collection-per-agent isolation in vector DB
  └─ vector_db.py
      ├─ get_vector_db(): factory branches on env VECTOR_DB_BACKEND
      ├─ ChromaDBBackend (local PersistentClient; embeds via DefaultEmbeddingFunction)
      └─ QdrantBackend (remote; UUID5(file_path) ids; payload-based filtering)

ToolManager + LiteCUA (aios/tool/)
  ├─ ToolManager (aios/tool/manager.py)
  │   ├─ tool_conflict_map: dict[name → Lock]   ← serialize same-tool concurrent invocations
  │   ├─ mcp_server: subprocess.Popen("mcp_server.py")  ← spawned at init
  │   ├─ address_request(syscall):
  │   │     for tc in syscall.query.tool_calls:
  │   │         tool = AutoTool.from_preloaded(tc.name)    ← Cerebrum-side registry
  │   │         result = tool.run(params=tc.parameters)
  │   └─ cleanup(): mcp_server.terminate()
  └─ virtual_env/  (LiteCUA — computer-use sandbox; novel)
      ├─ desktop_env.DesktopEnv (Gymnasium-style: reset / step / render)
      ├─ providers/  ← pluggable VM backends
      │   ├─ vmware.VMwareProvider
      │   ├─ virtualbox.VirtualboxProvider
      │   ├─ docker.DockerProvider
      │   ├─ aws.EC2Provider
      │   ├─ azure.AzureProvider
      │   └─ Provider ABC: start_emulator/get_ip/save_state/revert_to_snapshot/stop
      ├─ VMManager: PID-locked occupancy registry; free-VM pool
      ├─ controllers/
      │   ├─ python_controller.PythonController  ← HTTP client to in-VM Flask
      │   │     /screenshot, /accessibility, /execute, /terminal, /file
      │   └─ setup_controller.SetupController
      ├─ server/main.py  ← Flask app running INSIDE the VM
      │   ├─ /screenshot → pyautogui image + cursor overlay
      │   ├─ /accessibility → pyatspi (Linux) / pywinauto (Win) / oa_atomacos (mac)
      │   ├─ /execute → Python or shell command
      │   ├─ /terminal → terminal session state
      │   └─ /file → read/write endpoint
      ├─ accessibility_tree_wrap/heuristic_retrieve
      │     filters a11y tree by node_type/visible/enabled → leaf node coords
      └─ evaluators/
          ├─ getters (screenshot, file, terminal, Chrome, GIMP, VLC, VS Code state)
          └─ metrics (table_match, text_ocr, file_equality, system_time, app_list, ...)
          AND mcp_server.py at top level wraps DesktopEnv as MCP tools
          (click, type, hotkey, drag, scroll, screenshot, evaluate)

AgentFactory (aios/hooks/modules/agent.py — agent process lifecycle)
  ├─ thread_pool: ThreadPoolExecutor(max_workers=64 default)
  ├─ manager: cerebrum.manager.agent.AgentManager('https://app.aios.foundation')
  ├─ submitAgent(declaration) → execution_id
  │     download_agent → load_agent → agent_class(agent_name)
  │     thread_pool.submit(agent.run, task_input) → Future
  │     ProcessStore.addProcess(future, randint(100_000, 999_999))
  └─ awaitAgentExecution(pid) → result | None (running)

ConfigManager (aios/config/config_manager.py — singleton, YAML-backed)
  ├─ config: dict                              ← yaml.safe_load(config.yaml)
  ├─ refresh()                                  ← reload from disk
  ├─ save_config()                              ← write back
  └─ getters: get_api_key (config OR env fallback), get_llms_config,
              get_router_config, get_storage_config, get_memory_config,
              get_tool_config, get_scheduler_config, get_agent_factory_config,
              get_server_config, get_mcp_server_script_path

aios-rs (Rust scaffold; 250 LOC; pure trait declarations + placeholders)
  ├─ Cargo.toml: dependencies = { anyhow = "1" }   ← NO async, NO Tokio, NO pyo3
  ├─ src/
  │   ├─ lib.rs: re-exports + AIOS_RS_SCAFFOLD_VERSION = "0.0.1-alpha"
  │   ├─ context.rs: trait ContextManager + InMemoryContextManager
  │   │     (writes context to file by PID; mirrors Python's PID-keyed dict)
  │   ├─ memory.rs: trait MemoryManager + MemoryNote + InMemoryMemoryManager
  │   ├─ storage.rs: trait StorageManager + FsStorageManager (root + put/get)
  │   ├─ tool.rs: trait ToolManager + NoopToolManager (echo)
  │   ├─ llm.rs: trait LLMAdapter + EchoLLM
  │   ├─ scheduler.rs: trait Scheduler + NoopScheduler (start/stop bool flag)
  │   └─ prelude.rs: convenience re-exports
  └─ Status (per README): no async runtime, no vector DB, no FFI bridge to Python.
     Roadmap items 1-7 mostly unrealized. Aspirational, not load-bearing.

End-to-end request flow (single /query POST)
  HTTP POST /query
    ↓ asyncio.to_thread → SyscallExecutor.execute_request
    ↓ (for chat) ContextInjector.inject  → query.messages mutated
    ↓ _execute_syscall:
        create syscall thread
        push to global_<type>_req_queue
        syscall.start()  (thread runs run() = event.wait())
        syscall.join()   (blocks here)
        ┌────────────── (concurrently) ───────────────┐
        │ Scheduler daemon thread:                    │
        │   queue.get(timeout=0.1)                     │
        │   syscall.set_status("executing")            │
        │   response = manager.address_request(syscall)│
        │   syscall.set_response; event.set();         │
        │   set_status("done")                          │
        └─────────────────────────────────────────────┘
        ← syscall.join() returns
    ↓ (for chat) ConversationExtractor.extract_async  → daemon thread
    ↓ return response dict + timing metrics
```

---

## 2. Loom architectural tree (for side-by-side reference)

The Loom kernel tree was already captured in user-supplied form
(MessageBus → RoomState → RoomCoordinator → ParticipantActor →
ConversationPolicy → contracts → Journal → secret scrubber → build_prompt →
run_streaming_call). It is preserved verbatim in
`10-synthesis.md` and the v0.2 PR plan; not duplicated here. Refer to that
document for the canonical Loom tree.

---

## 3. Side-by-side architectural comparison

| Dimension | AIOS | Loom |
|---|---|---|
| **Layer** | Resource management substrate (LLM, memory, storage, tools, VM) | Conversation orchestration substrate (turns, leases, journal) |
| **Substrate** | FastAPI HTTP service + uvicorn workers | In-process Python library; embeddable |
| **Concurrency unit** | OS thread per syscall + 4 scheduler daemon threads | Per-actor daemon threads + central coordinator (RLock-guarded) |
| **Dispatch mechanism** | One global `queue.Queue` per request type + scheduler workers | Bus log (append-only) + lease arbitration + per-actor mailbox-via-cursor |
| **Identity / auth** | No membrane. Any module can push to any queue. CORS = "*" by default. | `_KernelAuth` token gates `post_internal`; sender-auth on `post`; grep-gated boundary tests |
| **State mutator discipline** | Many mutators (managers, factory, scheduler, executor all mutate shared dicts) | Single mutator (RoomCoordinator); RoomState `view()` is frozen + deep-frozen ParticipantInfoView |
| **Event log** | None. `proc/*.json` is per-execution; no ordered log of decisions. | `bus._log: list[Event]`; `ev.id == position`; append-only; replayable |
| **Determinism** | None — `while True` loops, randint PIDs, daemon threads | Append-only journal + epoch tracking + idempotent replay |
| **Policy boundary** | Implicit — anyone can call `manager.address_request`; no ABC | Explicit `ConversationPolicy` ABC; cannot import `loom.kernel.coordinator/journal`; cannot mutate `RoomState`; cannot post to bus |
| **LLM call lifecycle** | One-shot litellm.completion OR batched OR optionally interruptible (context manager) | Streaming + PASS protocol + lease + idle-dup filter + soft/hard cancel + lease_expired mid-stream |
| **Multi-agent coordination** | Implicit. Each agent has independent syscalls into shared managers. | Explicit. UserTurnPlan with required/optional/allowed_speakers, max_responses, routing_case, turn_order, wait_for_user_after |
| **Floor / turn control** | None | turn_order (round-robin), allowed_speakers, max_responses, wait_for_user, lease arbitration |
| **Replayability** | None. State lives in in-memory dicts and scattered files (Redis, vector DBs). | Snapshot v5 + journal replay; restore_from_snapshot tolerates legacy fields |
| **Watchdog / preemption** | LLM-generation preemption (KV-cache save/restore for HF local) — the genuine OS piece. No actor-level watchdog. | Coordinator watchdog thread (PR 10); lease TTL; policy_slow detection; idle_timeout |
| **Memory subsystem** | Pluggable providers (in-house Chroma/Qdrant, mem0, zep); LLM-classified notes; cross-agent sharing matrix in metadata | Not in scope — Loom doesn't manage memory; it's a policy/agent concern |
| **Tool subsystem** | ToolManager + MCP subprocess + full LiteCUA computer-use VM with 5 cloud/local providers | Not in scope — Loom agents have their own tool calling outside the kernel |
| **Persistence sink** | Redis (file versions) + vector DB (memory + storage embeddings) + YAML config + `proc/*.json` | `Journal` (single subscriber to bus); snapshot file + event-log file |
| **Error model** | Forgiving / silent fail (Pydantic validate prints and returns None; loops swallow exceptions; macros fallback to defaults) | Structured `policy_error`, `policy_slow`, `dead_letter`, `lease_denied(deny_reason, check_name)` events; boundary tests enforce fail-closed |
| **Secret scrubbing** | None in the kernel; LLMAdapter only masks API keys in error messages with a regex | `SecretShape` framework (7 default detectors) + register_secret_scrubber legacy hook; merge overlapping spans |
| **Observability** | print() + logging + `proc/*.json`; no aggregated metrics | Structured event stream; v0.2 lease_denied carries check_name + deny_reason for analytics |
| **Test discipline** | Live-HTTP integration tests on localhost:8000; fakeredis for unit; no boundary tests | Boundary tests (`test_kernel_kernel_boundary.py`); property tests (Hypothesis); mutation testing baseline |
| **Hot path** | `_execute_syscall` while-loop spawns OS thread that immediately blocks; scheduler thread does real work; thread amplification | `actor._loop` uses `bus.wait_after` (cond var); coordinator mutates under RLock; lease grant returns or denies in O(checks) |
| **Composition** | Mostly singletons (config, executor, queues) + module-level state | Rooms are independent; multiple rooms can coexist; clean tear-down |
| **Public API surface** | HTTP endpoints + Cerebrum SDK (separate repo) | Python facade (`loom.api`, `loom.contracts`); `make ux-check` enforces stability |

---

## 4. Architectural divergences worth understanding

### 4.1 Syscalls vs Events

AIOS's `Syscall(Thread)` is structurally a **promise wrapper**: the thread
spawns, calls `event.wait()`, then dies after the scheduler fills its
response. The work happens elsewhere. The thread is dead weight — every
request costs ≥2 OS threads (the syscall + the scheduler worker pulling it).

Loom's `Event` is just a frozen record on the bus log. Actors subscribe
via cursor; the bus's `wait_after` is the single blocking primitive. No
extra threads spawn per event. The thread budget is `O(participants) +
O(rooms)`, not `O(in-flight requests)`.

### 4.2 Boundary discipline

AIOS has **no kernel/policy boundary at all**. The scheduler imports the
managers; the managers import config; macros in `SyscallExecutor` call the
LLM to classify and evolve memories (kernel→LLM→kernel coupling).
ConversationPolicy as a concept doesn't exist; whatever policy is encoded
lives implicitly in `execute_request`'s big if/elif on `action_type`.

Loom's v0.2 made the boundary a load-bearing invariant: `_KernelAuth`
sentinel, grep-gated boundary tests, frozen `ParticipantInfoView`, no
`loom.kernel.coordinator` import from `loom.policy.*`. The policy can hook
in via documented points (`charter_text`, `prompt_sections`,
`dead_letter_target`, `should_post_response`, `LeaseCheck` chain) without
ever touching kernel state directly.

### 4.3 "Scheduling" semantics

The names `FIFOScheduler` and `RRScheduler` are misleading. Both spawn the
same 4 daemon threads; both call `queue.get(block=True, timeout=0.1)`.

- **FIFO** has a real distinction: it batches LLM requests over a 1s window
  (`batch_interval=1.0`) so multiple agents' LLM calls can be coalesced.
- **RR** claims round-robin but doesn't preempt at the scheduler level.
  Its actual purpose is to instantiate a `SimpleContextManager` so the
  LLMAdapter can do per-PID generation interruption.

There is **no preemption at the syscall layer** in either scheduler. The
only true preemption is inside the LLM generation loop (PR-style cooperative
preemption with KV-cache save) — and that only fires when
`time_limit` is set, which only happens under RR + `use_context_manager`.

Loom has no analogous "scheduler" because Loom doesn't multiplex syscalls.
What Loom *does* have: lease arbitration (only one actor can hold the floor
for a given trigger), turn-order rotation, `max_responses` enforcement at
lease grant time, and the v0.2 watchdog thread for idle/lease/policy timing.

### 4.4 Replayability and audit

AIOS's runtime state lives in:
- Python module-level singletons (`active_components`, `selected_llms`,
  `ProcessStore.AGENT_PROCESSES`)
- Four global `queue.Queue` instances
- ChromaDB / Qdrant collections
- Redis lists for file versions
- YAML on disk for config
- `proc/*.json` per execution

There is no ordered, fork-free event log. Reconstructing "what happened" in
a given conversation is impossible — you'd have to correlate timestamps
across all these stores.

Loom's `bus._log` is the single source of truth. The journal subscribes;
snapshots are tagged with a schema version (v5 in v0.2, tolerant of v3/v4);
replay reconstructs state deterministically.

### 4.5 LLM call lifecycle

AIOS's LLM call (`LLMAdapter.execute_llm_syscall`):
1. extract messages / tools / response_format / temperature / max_tokens
2. preprocess tools (slash → double underscore)
3. `_get_model_response` → branches on backend (litellm / OpenAI client / HF)
4. `_process_response` → decode tool calls if any; wrap in `LLMResponse`

No streaming up to the caller; no PASS protocol; no idle-dup filter; no
sender-auth; no mid-stream lease invalidation. Errors are classified
into `status_code` and stuffed into `LLMResponse.error`.

Loom's `run_streaming_call`:
1. Phase 1 setup
2. Phase 2 prefix buffering for PASS protocol detection
3. Phase 3 streaming chunks to bus
4. Phase 4 post-stream filters (kernel-side empty/idle/IoU + v0.2
   `policy.should_post_response` veto last)
5. Phase 5 finalize with closed-set status enum ("ok" / "suppressed" /
   "filtered" / "error")

Plus mid-stream `lease_expired` if the lease invalidates (mode/membership
change).

### 4.6 The OS metaphor — where it pays off

AIOS frames itself as an OS. Most of the metaphor is decorative
(`syscall`, `pid`, `proc/`, `scheduler`), but **one piece earns its
keep**: `SimpleContextManager.generate_with_time_limit_hf` does
honest-to-god LLM-generation preemption — saving `past_key_values` (the KV
cache) and resuming from the saved tensors on the next scheduled call.
This is the genuine OS-like contribution of AIOS, and it has no analog
in Loom (where LLM calls run to completion or get cancelled).

Loom's "OS-like" pieces are at a different layer: lease arbitration, the
journal as append-only log, the boundary discipline, deterministic replay.
These are OS hygiene concerns; AIOS's KV-cache preemption is an OS
mechanism concern. They don't overlap.

### 4.7 LiteCUA — the part Loom has nothing to say about

The `aios/tool/virtual_env/` subtree is genuinely novel and orthogonal to
anything in Loom. It's a complete computer-use sandbox with pluggable
virtualization (VMware, VBox, Docker, EC2, Azure), an in-VM Flask agent
that exposes `/screenshot`, `/accessibility`, `/execute`, accessibility-tree
filtering for GUI target selection, and MCP-tool exposure to the agent
LLM. PID-locked VM occupancy, snapshot reset per task. This is the most
impressive single subsystem in AIOS and has no Loom counterpart because
Loom is silent about tool execution mechanics.

---

## 5. Where each system excels

### AIOS strengths
1. **Multi-backend LLM routing** with Sequential + Smart (PuLP LP) cost/perf
   optimization over historical query store
2. **LLM-generation preemption** with KV-cache save/restore for HF locals
3. **LiteCUA computer-use sandbox** — Gym-style, multi-provider, MCP-exposed
4. **Pluggable memory** with metadata-based cross-agent sharing matrix and
   LLM-driven memory evolution
5. **Personalization pipeline** (ContextInjector + ConversationExtractor)
   cleanly bracketing the LLM call
6. **Semantic file system** (LSFS) with vector indexing, Redis-backed
   versioning, time-travel rollback

### Loom strengths
1. **Hard kernel/policy boundary** with grep-gated tests, frozen state views,
   `_KernelAuth` privileged-write token
2. **Deterministic replay** via append-only journal and versioned snapshots
3. **Streaming LLM call lifecycle** with PASS protocol, cancellation,
   mid-stream lease invalidation
4. **Multi-agent turn semantics**: required/optional/allowed_speakers,
   max_responses, routing_case, turn_order, wait_for_user_after, dead-letter
   rerouting
5. **Extensible lease chain** (8 default LeaseChecks + pluggable
   `LeaseCheck` protocol) with structured `deny_reason` strings
6. **Pluggable prompt sections** + `charter_text` + `dead_letter_target` +
   `should_post_response` policy hooks, all behind a stable ABC
7. **Boundary tests + property tests + mutation baseline** as discipline
   primitives

---

## 6. Composition opportunity

The two systems sit at different layers and could meaningfully compose:

```
                   ┌─────────────────────────────────┐
                   │ User / agent developer          │
                   └─────────────────────────────────┘
                                  │
                                  ▼
                   ┌─────────────────────────────────┐
                   │ Loom rooms (orchestration)      │
                   │   policy + actors + journal     │
                   └─────────────────────────────────┘
                                  │
                                  ▼
                   ┌─────────────────────────────────┐
                   │ AIOS managers (resources)       │
                   │   LLM / memory / storage / tool │
                   └─────────────────────────────────┘
```

A Loom actor's agent callback could call into AIOS's `/query` endpoint
(or its `SyscallExecutor` in-process). Loom would own the turn semantics,
journal, and policy; AIOS would own LLM routing/preemption, memory
storage, semantic file system, and the LiteCUA sandbox.

The boundary lives at the agent callback: Loom never sees AIOS state, and
AIOS never sees Loom's bus log. They communicate via Cerebrum-style
typed queries (`LLMQuery`, `MemoryQuery`, `ToolQuery`, `StorageQuery`).

---

## 7. Weaknesses & gaps in AIOS (notes for future evaluation)

1. **Thread amplification**: `_execute_syscall` while-loop spawns an OS
   thread that blocks immediately. Could be a `Future` with no behavioral
   change.
2. **No real scheduling**: FIFO/RR labels misrepresent behavior; RR's
   `process_llm_requests` passes a single syscall where a list is
   expected (`rr_scheduler.py:181`).
3. **No boundary discipline**: kernel macros call LLMs to classify memory;
   anyone can push to any queue; CORS is `*` by default.
4. **Hooks pattern is decorative**: no DI scope, no lifecycle, no graph;
   just Pydantic-validated factories with React names.
5. **Side-channel state**: `known_user_ids` registry, `ProcessStore`
   global, module-level `active_components` singleton dict.
6. **No replayability**: state scattered across Chroma + Qdrant + Redis +
   YAML + per-execution JSON files.
7. **Hardcoded externalities**: SmartRouting bootstraps from Google Drive
   via `gdown`; `sto_share` uploads to transfer.sh.
8. **Rust scaffold is aspirational**: 250 LOC of trait stubs; no async,
   no FFI, no Python bridge.
9. **Tests are integration-heavy**: minimal unit coverage for intent
   routing, scheduler correctness, context-switching invariants, no
   boundary tests.
10. **Computer-use security gap**: VM isolation is real, but no per-agent
    capability scoping; every agent has every tool.

---

## 8. Memory note

This document is a one-shot architectural snapshot. AIOS is not part of
Loom's dependency graph; this comparison is purely for understanding the
broader agent-substrate landscape. If Loom v0.3+ ever explores composition
with an external resource layer, AIOS is a candidate to evaluate — but
the boundary discipline and replayability gaps documented above would
need addressing first (likely on the AIOS side).
