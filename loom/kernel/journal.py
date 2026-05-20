"""Loom v0 — Journal: events.jsonl + room_state.json.

Persistence layer. ``events.jsonl`` is the authoritative source of
truth: every canonical event the bus posts is appended as a JSON line.
``room_state.json`` is an advisory fast-resume cache — written
atomically (temp file + rename + fsync where practical) on clean
shutdown, after key control events, and every N events.

On startup:

1. Load ``room_state.json`` if present.
2. Replay ``events.jsonl`` past the snapshot's event id to catch up.
3. If the snapshot is missing or corrupt, rebuild from ``events.jsonl``
   alone (slower but always correct).

The journal subscribes itself to the bus via :meth:`Journal.on_event`
so callers don't have to thread events through manually. The coordinator
calls :meth:`Journal.snapshot` after each protected control event and on
clean shutdown.

Snapshot version history:

- v1 (legacy): carried ``mode`` and ``debate`` keys for the now-retired
  ``normal``/``council``/``debate`` modes. v1 snapshots are still
  loadable: :func:`restore_state` ignores any keys not present on the
  v0 :class:`RoomState`.
- v2: drops ``mode``/``debate``. Adds nothing new.
- v3: adds ``turn_taking_mode``, ``turn_order``, ``next_speaker_idx``.
- v4: drops ``active_goal`` from control state.
- v5: drops ``turn_taking_mode``; round-robin is now signalled
  by ``turn_order`` being non-empty. v4 / v3 snapshots that carry a
  ``turn_taking_mode`` value are read but the field is discarded — if
  the snapshot was in ``round_robin`` the corresponding ``turn_order``
  is what restores the rotation. v3 snapshots that carry
  ``active_goal`` are honored as a fallback for ``RoomState.topic``
  (P2.3 topic-merge restore shim).
- v6 (current; v0.3 PR 1, doctrine P5 / §1): adopts the
  :class:`loom.kernel.state.KernelState` transactional root. The
  former v5 top-level RoomState fields are nested under a ``"room"``
  key; sibling slots are reserved for v0.3 subsystem states
  (``"capabilities"`` PR 5, ``"budget"`` PR 6, ``"actors"`` PR 13)
  and post-v0.3 subsystems (``"workflow"``, ``"tools"``). PR 1 always
  writes these sibling slots as ``null``; later PRs serialize the
  populated sub-states. v5 snapshots are still loadable: the migrator
  :func:`_migrate_v5_to_v6` reshapes a v5 dict into v6 in-memory
  before :func:`restore_kernel_state` consumes it.

Old ``events.jsonl`` lines with retired control types
(``mode_changed`` / ``debate_turn`` / ``forfeit`` / ``debate_end``)
deserialize fine but are filtered out at replay time via
:func:`is_known_control`.

Thread-safety: the journal's own lock guards the file handles and
counters. State serialization reads :class:`RoomState` from the caller —
the caller must hold the coordinator lock during snapshot if it wants
a consistent picture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, cast
import json
import os
import queue
import threading

import stat
import warnings

from loom.kernel import events as _ev
from loom.kernel.bus import _KERNEL_AUTH
from loom.kernel.events import Event, EventShapeError, is_known_control
from loom.kernel.room import (
    ParticipantInfo,
    RoomConfig,
    RoomControlState,
    RoomState,
    StyleLevel,
)
from loom.kernel.context import (
    ContextState,
    context_state_from_jsonable,
    context_state_to_jsonable,
    new_context_state,
)
from loom.kernel.state import KERNEL_STATE_SCHEMA_VERSION, KernelState, new_kernel_state


SNAPSHOT_VERSION = KERNEL_STATE_SCHEMA_VERSION  # v0.3.x PR 2: 6 → 7.

# v1 snapshots are still readable; the restore loop ignores keys
# (``mode``, ``debate``) that no longer exist on RoomState. v2 snapshots
# omit ``turn_order`` / ``next_speaker_idx`` — those default to empty /
# 0 in :func:`restore_state`. v3 / v4 snapshots may carry the retired
# ``turn_taking_mode`` field; restore_state discards it (round-robin
# is signalled by a non-empty ``turn_order``). v3 snapshots may carry
# ``control.active_goal``; the topic-merge restore shim folds it into
# ``state.topic`` when topic is unset. v5 snapshots are migrated to v6
# in-memory by :func:`_migrate_v5_to_v6` before :func:`restore_kernel_state`
# reads them. v6 snapshots lack the ``"context"`` slot; v0.3.x PR 2
# (:func:`_migrate_v6_to_v7`) fills it with an empty :class:`ContextState`.
_SUPPORTED_SNAPSHOT_VERSIONS = frozenset({1, 2, 3, 4, 5, 6, 7})


class Journal:
    """Append-only events.jsonl + advisory room_state.json cache."""

    def __init__(
        self,
        session_dir: str | Path,
        *,
        snapshot_every_events: int = 100,
        snapshot_queue_maxsize: int = 8,
    ) -> None:
        self.session_dir = Path(session_dir)
        # T6 / P0.6: lock down the session dir to owner-only. POSIX
        # ``mkdir(mode=0o700)`` is honored on creation; if the dir
        # already exists with looser perms we surface a warning so the
        # operator sees the divergence (we don't silently chmod — that
        # could surprise an operator who deliberately set group access).
        self.session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._warn_if_session_dir_world_readable()
        self.events_path = self.session_dir / "events.jsonl"
        self.state_path = self.session_dir / "room_state.json"
        self.snapshot_every_events = snapshot_every_events

        self._lock = threading.Lock()
        self._events_file: Optional[Any] = None
        self._event_count = 0
        self._last_snapshot_count = 0
        self._snapshot_due_cb: Optional[Callable[[], Optional[dict]]] = None

        # Off-lock snapshot writer. The due-callback runs on the post
        # thread (which is the coord-lock-holding thread for state-
        # mutating posts) and is required to be cheap — it returns a
        # snapshot *dict*, not a file. The dict goes on this queue and
        # the background thread does the slow disk work (json.dump,
        # fsync, atomic rename) without holding any room lock.
        #
        # P2.3 / audit RES3: the queue is BOUNDED. On overflow (slow
        # disk, NFS hiccup), the oldest pending snapshot is dropped in
        # favor of the newest — snapshots are coalesce-able since each
        # is a complete state, only the most recent matters. The
        # configured cap defaults to 8 which is plenty: with the
        # default snapshot_every_events=100, an 8-deep queue covers
        # a 800-event burst before any disk write completes.
        self._snapshot_queue_maxsize = snapshot_queue_maxsize
        self._snapshot_queue: queue.Queue = queue.Queue(maxsize=snapshot_queue_maxsize)
        self._snapshot_thread: Optional[threading.Thread] = None
        self._snapshots_dropped: int = 0
        self._snapshot_drop_callback: Optional[Callable[[int, int], None]] = None

        # Degraded-mode tracking. ``degraded`` flips True after any
        # ``events.jsonl`` write OR background snapshot write fails; the
        # journal continues running so the room itself doesn't crash,
        # but a ``journal_error`` control event is posted the first time
        # so callers can observe the divergence.
        self.degraded: bool = False
        self._failure_callback: Optional[Callable[[Exception], None]] = None
        # Set during the failure callback to avoid recursion when the
        # callback itself triggers another bus.post → on_event.
        self._in_failure_dispatch: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open ``events.jsonl`` and start the background snapshot thread.

        T6 / P0.6: the events file is opened via ``os.open`` with explicit
        mode ``0o600`` so a freshly-created journal is owner-readable
        only. ``os.fdopen`` wraps the descriptor for line-buffered text
        I/O — we keep the existing ``buffering=1`` semantics. If the
        file already exists with looser perms we warn (see
        :meth:`_warn_if_events_file_world_readable`) but do NOT chmod
        — preserving operator intent.
        """
        with self._lock:
            if self._events_file is not None:
                return
            self._warn_if_events_file_world_readable()
            fd = os.open(
                self.events_path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o600,
            )
            self._events_file = os.fdopen(fd, "a", buffering=1)
            if self._snapshot_thread is None:
                self._snapshot_thread = threading.Thread(
                    target=self._snapshot_loop,
                    name="loom-journal-snapshot",
                    daemon=True,
                )
                self._snapshot_thread.start()

    # ------------------------------------------------------------------
    # File permission helpers (T6 / P0.6)
    # ------------------------------------------------------------------

    @staticmethod
    def _world_or_group_perm_bits(mode: int) -> int:
        """Return the group + world rwx bits set on ``mode``."""
        return mode & (stat.S_IRWXG | stat.S_IRWXO)

    def _warn_if_session_dir_world_readable(self) -> None:
        """Warn (don't chmod) when the session dir has group/world bits."""
        try:
            st = self.session_dir.stat()
        except OSError:
            return
        bits = self._world_or_group_perm_bits(st.st_mode)
        if bits:
            warnings.warn(
                "loom.kernel.journal: session_dir "
                f"{self.session_dir} has group/world perm bits "
                f"({oct(bits)}). DM transcripts and any leaked secrets "
                "in error events become readable to other users. "
                "Recommended: chmod 700 the session_dir.",
                stacklevel=3,
            )

    def _warn_if_events_file_world_readable(self) -> None:
        if not self.events_path.exists():
            return
        try:
            st = self.events_path.stat()
        except OSError:
            return
        bits = self._world_or_group_perm_bits(st.st_mode)
        if bits:
            warnings.warn(
                "loom.kernel.journal: events.jsonl at "
                f"{self.events_path} has group/world perm bits "
                f"({oct(bits)}). Recommended: chmod 600.",
                stacklevel=3,
            )

    def close(self) -> None:
        """Close the events file and drain the snapshot queue.

        Pushes a ``None`` sentinel onto the queue and joins the writer
        thread so any pending snapshot lands on disk before we return.
        Safe to call multiple times.
        """
        with self._lock:
            file = self._events_file
            self._events_file = None
            thread = self._snapshot_thread
            self._snapshot_thread = None
        if thread is not None:
            self._snapshot_queue.put(None)
            thread.join(timeout=5.0)
        if file is not None:
            try:
                file.flush()
            finally:
                file.close()

    def __enter__(self) -> "Journal":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Event append
    # ------------------------------------------------------------------

    def on_event(self, event: Event) -> None:
        """Bus subscriber. Appends one JSON line per event.

        Safe to call before :meth:`open` (events are dropped silently
        until the journal is opened) — this allows wiring the
        subscription before the file is ready.

        On write failure: flips ``self.degraded`` to ``True``, fires the
        failure callback (if registered, and not already in a recursive
        dispatch), and returns. The room continues running but with the
        knowledge that ``events.jsonl`` is incomplete.
        """
        with self._lock:
            if self._events_file is None:
                return
            try:
                self._events_file.write(event.to_jsonl() + "\n")
            except OSError as exc:
                self.degraded = True
                cb = self._failure_callback
                in_dispatch = self._in_failure_dispatch
                if cb is not None and not in_dispatch:
                    self._in_failure_dispatch = True
                else:
                    cb = None
                # Drop the lock before invoking the callback (which may
                # post to the bus and re-enter this method).
                _failure_exc: Optional[Exception] = exc
            else:
                _failure_exc = None
                cb = None
                in_dispatch = False

            if _failure_exc is not None:
                # Don't count the failed write toward snapshot rotation.
                pass
            else:
                self._event_count += 1
            snap_cb = self._snapshot_due_cb
            due = (self._event_count - self._last_snapshot_count) >= self.snapshot_every_events
        if _failure_exc is not None and cb is not None:
            try:
                cb(_failure_exc)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._in_failure_dispatch = False
        if _failure_exc is not None:
            return
        if snap_cb is not None and due:
            try:
                payload = snap_cb()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                with self._lock:
                    self._last_snapshot_count = self._event_count
                self._enqueue_snapshot(payload)

    def _enqueue_snapshot(self, payload: dict) -> None:
        """Put ``payload`` on the snapshot queue, dropping oldest on overflow.

        P2.3 / audit RES3. Snapshots are coalesce-able (each is a
        complete state); when the writer can't keep up we keep the
        newest and drop the oldest. The configured cap is enforced
        here, NOT on the writer side — overflow detection and the
        drop notification both happen on the producer thread.
        """
        try:
            self._snapshot_queue.put_nowait(payload)
            return
        except queue.Full:
            pass
        # Drop oldest, then put. ``get_nowait`` may race against the
        # writer; in that case we just put the new payload and the
        # writer drains it normally.
        try:
            self._snapshot_queue.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            self._snapshots_dropped += 1
            total = self._snapshots_dropped
            cb = self._snapshot_drop_callback
            depth = self._snapshot_queue_maxsize
        try:
            self._snapshot_queue.put_nowait(payload)
        except queue.Full:
            # Should not happen — we just made room. Defensive.
            pass
        if cb is not None:
            try:
                cb(total, depth)
            except Exception:
                # A misbehaving drop callback must not break the
                # snapshot path — drops are observable, not load-bearing.
                pass

    def set_snapshot_due_callback(self, cb: Optional[Callable[[], Optional[dict]]]) -> None:
        """Register a snapshot-payload producer.

        ``cb()`` is invoked synchronously on the post thread when the
        event count reaches ``snapshot_every_events``. It must return a
        snapshot **dict** — typically ``Journal._state_to_dict(state)``.
        Construction is expected to be cheap (microseconds); the slow
        disk write happens off-thread on the journal's snapshot writer.

        Returning anything other than a dict (including ``None``) skips
        the snapshot for this rotation; the next rotation tries again.
        """
        with self._lock:
            self._snapshot_due_cb = cb

    def set_snapshot_drop_callback(self, cb: Optional[Callable[[int, int], None]]) -> None:
        """Register a callback invoked when the snapshot queue overflows.

        ``cb(dropped_total, queue_depth)`` runs on the post thread (the
        same thread that called :meth:`on_event`). Used by the runtime
        to emit a ``snapshot_dropped`` control event so consumers can
        observe disk-saturation. A buggy callback that raises is
        suppressed — drops are observability, not load-bearing.
        """
        with self._lock:
            self._snapshot_drop_callback = cb

    def set_failure_callback(self, cb: Optional[Callable[[Exception], None]]) -> None:
        """Register a callback fired the first time a write fails.

        The callback receives the originating ``OSError``. It runs on
        the poster's thread (the same thread that called ``bus.post``).
        Typical use: post a ``journal_error`` control event so consumers
        can observe the degraded state. If the callback itself triggers
        another write, the recursion guard suppresses re-entry.
        """
        with self._lock:
            self._failure_callback = cb

    # ------------------------------------------------------------------
    # State snapshot
    # ------------------------------------------------------------------

    def snapshot(self, state: "RoomState | KernelState") -> None:
        """Synchronously write ``room_state.json`` from ``state``.

        v0.3 PR 1: accepts either :class:`RoomState` (back-compat for
        existing call sites) or :class:`KernelState` (the v0.3
        canonical root). RoomState inputs are wrapped via
        :func:`loom.kernel.state.new_kernel_state` so the on-disk shape
        is always v6.

        Used at clean shutdown where the caller wants the snapshot to be
        durable before the function returns. Periodic mid-session
        rotation goes through the background thread instead — see
        :meth:`set_snapshot_due_callback`.
        """
        self._write_snapshot_dict(self._state_to_dict(state))
        with self._lock:
            self._last_snapshot_count = self._event_count

    def _write_snapshot_dict(self, payload: dict) -> None:
        """Atomically write ``payload`` to ``room_state.json``.

        Temp file + ``fsync`` + ``os.replace`` ensures readers see only
        a complete snapshot; a torn file cannot occur. The temp file
        is created with mode ``0o600`` so room state (which can include
        active topic / role hints / persona memories) is not world-
        readable on shared hosts. Failures flip ``degraded`` and fire
        the failure callback (same path as ``events.jsonl`` write
        failures).
        """
        try:
            tmp = self.state_path.with_suffix(".json.tmp")
            # T6 / P0.6: mode=0o600 on creation. ``open(tmp, "w")``
            # respects the umask and would default to 0o644 on most
            # systems; using ``os.open`` pins the perms to owner-only.
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2, default=str)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self.state_path)
        except OSError as exc:
            with self._lock:
                self.degraded = True
                cb = self._failure_callback
                in_dispatch = self._in_failure_dispatch
                if cb is not None and not in_dispatch:
                    self._in_failure_dispatch = True
                else:
                    cb = None
            if cb is not None:
                try:
                    cb(exc)
                except Exception:
                    pass
                finally:
                    with self._lock:
                        self._in_failure_dispatch = False

    def _snapshot_loop(self) -> None:
        """Background writer: drain the snapshot queue until shutdown.

        Runs on a daemon thread started by :meth:`open`. Receiving the
        ``None`` sentinel (pushed by :meth:`close`) terminates the loop.
        Any disk error is recorded via ``_write_snapshot_dict``'s own
        failure path, then we keep draining so a single bad write
        doesn't strand later snapshots.
        """
        while True:
            payload = self._snapshot_queue.get()
            if payload is None:
                return
            try:
                self._write_snapshot_dict(payload)
            except Exception:
                # _write_snapshot_dict already records the OSError
                # path; truly unexpected exceptions here just mean the
                # next snapshot tries again.
                pass

    @staticmethod
    def _room_state_to_dict(state: RoomState) -> dict:
        """Render the v5-shape room sub-dict (no envelope key).

        Split out from :meth:`_state_to_dict` so v0.3 PR 1's v6 envelope
        can nest the unchanged v5 fields under a ``"room"`` key without
        re-spelling them.
        """
        return {
            "room_epoch": state.room_epoch,
            "topic": state.topic,
            "anchor_id": state.anchor_id,
            "chair_id": state.chair_id,
            "default_responder_id": state.default_responder_id,
            "default_summarizer_id": state.default_summarizer_id,
            "current_user_turn_id": state.current_user_turn_id,
            "last_compacted_event_id": state.last_compacted_event_id,
            "participants": [
                {
                    "id": p.id,
                    "capable": p.capable,
                    "cost_tier": p.cost_tier,
                    "active": p.active,
                    "role_hints": p.role_hints,
                }
                for p in state.participants.values()
            ],
            # Room control state (roles / floor / wait / style / goal).
            # Persists across restarts so a "/floor gemini" + "/brief"
            # session resumes with the same constraints.
            "control": {
                "roles": dict(state.control.roles),
                "wait_for_user": state.control.wait_for_user,
                "style": state.control.style,
                "turn_order": list(state.control.turn_order),
                "next_speaker_idx": state.control.next_speaker_idx,
            },
        }

    @classmethod
    def _state_to_dict(cls, state: "RoomState | KernelState") -> dict:
        """Render a v7 snapshot dict from KernelState (or wrap a RoomState).

        v0.3 PR 1 envelope shape (bumped to v7 by v0.3.x PR 2):

        - ``"version"``: 7
        - ``"room"``: v5-shape room sub-dict (from
          :meth:`_room_state_to_dict`)
        - ``"context"``: v0.3.x PR 2 — JSON-able compaction state
          (:func:`loom.kernel.context.context_state_to_jsonable`).
        - ``"capabilities"``, ``"budget"``, ``"actors"``,
          ``"workflow"``, ``"tools"``: reserved sibling slots, always
          ``null`` in PR 1; populated by their owning PRs.
        - ``"kernel_version"``: monotonic transactional counter
          (:attr:`KernelState.version`).
        """
        if isinstance(state, KernelState):
            kernel = state
        else:
            kernel = new_kernel_state(state)
        return {
            "version": SNAPSHOT_VERSION,
            "room": cls._room_state_to_dict(kernel.room),
            "context": context_state_to_jsonable(kernel.context),
            "capabilities": None,
            "budget": None,
            "actors": None,
            "workflow": None,
            "tools": None,
            "kernel_version": kernel.version,
        }

    # ------------------------------------------------------------------
    # Load / replay
    # ------------------------------------------------------------------

    def load_state(self) -> Optional[dict]:
        """Read ``room_state.json``; return ``None`` on any failure or
        unsupported version skew.
        """
        if not self.state_path.exists():
            return None
        try:
            with open(self.state_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        if data.get("version") not in _SUPPORTED_SNAPSHOT_VERSIONS:
            # Far-future / corrupt snapshot → caller rebuilds from events.
            return None
        return data

    def load_events(self) -> list[Event]:
        """Read ``events.jsonl`` into a list; skip malformed lines silently.

        Materializes the full log in memory. Prefer :meth:`iter_events`
        for replay paths where you don't need the entire list at once —
        peak memory drops from O(N) to O(1) on long histories.

        Default behaviour (``emit_corruption_events=False``) preserves
        the legacy "skip malformed silently" contract; the replay path
        opts in via :meth:`iter_events` so corrupt lines surface as
        observable events rather than silent drops.
        """
        return list(self.iter_events(emit_corruption_events=False))

    def iter_events(self, *, emit_corruption_events: bool = False):
        """Stream ``events.jsonl`` line-by-line.

        Yields :class:`Event` instances one at a time. Used by
        :meth:`replay_into` so a 100k-event log doesn't materialize a
        100k-element list before the first event is posted.

        When ``emit_corruption_events=True`` (default for replay), a
        line that fails per-kind shape validation
        (:class:`EventShapeError` from :meth:`Event.from_jsonl`) is
        yielded as a synthetic ``journal_corruption`` control event
        rather than silently dropped (P0.2). The trailing line of the
        file gets a separate ``journal_truncated`` event (P0.4) — that
        position almost always means an interrupted write at crash, so
        it's worth distinguishing from genuine tampering.
        """
        if not self.events_path.exists():
            return
        with open(self.events_path) as f:
            # Read into a list so we can detect "this is the last line"
            # without an extra pass. For very large logs the perf-pass
            # left a streaming-replay path; this method is intended for
            # replay-time use where we already accepted reading every
            # line. Per-line allocation is what dominates, not the
            # outer list.
            raw_lines = list(f)
        last_idx = len(raw_lines) - 1
        for line_offset, raw in enumerate(raw_lines):
            line = raw.strip()
            if not line:
                continue
            try:
                yield Event.from_jsonl(line)
                continue
            except EventShapeError as exc:
                if not emit_corruption_events:
                    continue
                excerpt = raw[:120]
                if line_offset == last_idx:
                    yield _ev.journal_truncated(
                        line_offset=line_offset,
                        raw_excerpt=excerpt,
                    )
                else:
                    yield _ev.journal_corruption(
                        line_offset=line_offset,
                        raw_excerpt=excerpt,
                        error_class=type(exc).__name__,
                        error_message=str(exc),
                    )
            except Exception as exc:
                # Any other exception (e.g. a future ``json`` quirk
                # on a CPython upgrade) — surface the same way as a
                # shape error rather than mask it.
                if not emit_corruption_events:
                    continue
                excerpt = raw[:120]
                yield _ev.journal_corruption(
                    line_offset=line_offset,
                    raw_excerpt=excerpt,
                    error_class=type(exc).__name__,
                    error_message=str(exc),
                )

    def replay_into(self, coordinator) -> int:
        """Replay ``events.jsonl`` events into the coordinator's bus.

        Skips control events whose type is no longer registered (legacy
        ``mode_changed`` / ``debate_*`` lines from v1 sessions). Returns
        the number of events posted, INCLUDING any synthetic
        ``journal_corruption`` / ``journal_truncated`` events surfaced
        on parse failure (P0.2 / P0.4 — corruption is observable, not
        silent).

        The coordinator's lock is *not* held by this method; the caller
        is responsible for any external coordination needed during
        replay. Uses :meth:`iter_events` so peak RSS during replay is
        independent of log length.
        """
        posted = 0
        for event in self.iter_events(emit_corruption_events=True):
            if event.kind == "control" and not is_known_control(event):
                continue
            # P1: replay re-injects events with their original ``sender``
            # (which can be any participant id, ``"user"``, or
            # ``"system"``). The thread-actor binding doesn't apply here
            # — replay is privileged kernel code by definition; the
            # disk content has been validated by ``Event.from_jsonl`` /
            # ``EventShapeError``.
            coordinator.bus.post_internal(event, auth=_KERNEL_AUTH)
            posted += 1
        return posted


def _coerce_int(v: Any, default: int = 0) -> int:
    """``v`` if it is a real int (not bool), else ``default``.

    JSON parses ``true``/``false`` to Python ``True``/``False`` which
    are ``int`` subclasses. A tampered snapshot with ``"room_epoch": true``
    would otherwise propagate ``True`` into the state and surprise
    downstream callers.
    """
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    return default


def _coerce_str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return None


def _migrate_v5_to_v6(state_data: dict) -> dict:
    """Reshape a v5 (or earlier) snapshot dict into the v6 envelope shape.

    v6 nests the v5 top-level RoomState fields under a ``"room"`` key
    and adds reserved sibling slots for v0.3 subsystem states (all
    ``null`` in PR 1). Idempotent: a dict already in v6 shape (has a
    ``"room"`` key with a dict value) is returned as-is.

    The migration is in-memory only — :class:`Journal` does not rewrite
    older ``room_state.json`` files on disk. The next clean snapshot
    write replaces the on-disk file with v6 shape (and the v6→v7
    migration is applied immediately afterwards by
    :func:`_migrate_v6_to_v7`).
    """
    if isinstance(state_data.get("room"), dict):
        # Already v6-shaped (or later); the v6→v7 migration handles
        # the next step.
        return state_data
    # Strip the version key from the inner dict to avoid confusion;
    # everything else (room_epoch, topic, control, participants, etc.)
    # is what becomes the room sub-dict.
    inner = {k: v for k, v in state_data.items() if k != "version"}
    return {
        "version": 6,
        "room": inner,
        "capabilities": None,
        "budget": None,
        "actors": None,
        "workflow": None,
        "tools": None,
        "kernel_version": 0,
    }


def _migrate_v6_to_v7(state_data: dict) -> dict:
    """Add the v0.3.x PR 2 ``"context"`` slot to a v6 snapshot dict.

    v7 nests the empty-default :class:`ContextState` serialisation
    under ``"context"`` and bumps ``"version"`` to 7. Idempotent — a
    dict already at v7 (``version >= 7`` or a ``"context"`` key
    present) is returned as-is.

    The migration is in-memory only — :class:`Journal` does not
    rewrite older ``room_state.json`` files on disk. The next clean
    snapshot write replaces the file with v7 shape.
    """
    if not isinstance(state_data, dict):
        return state_data
    if state_data.get("version", 0) >= 7 and "context" in state_data:
        return state_data
    out = dict(state_data)
    out["version"] = 7
    out["context"] = context_state_to_jsonable(new_context_state())
    return out


def _room_sub_dict(state_data: Optional[dict]) -> Optional[dict]:
    """Return the v5-shape room sub-dict from any supported snapshot.

    For v6 snapshots, this is ``state_data["room"]`` (the nested
    dict). For v1–v5 snapshots, it is ``state_data`` itself (the v5
    fields live at the top level). Returns ``None`` when no usable
    payload is present.
    """
    if not state_data or not isinstance(state_data, dict):
        return None
    version = state_data.get("version")
    if isinstance(version, int) and version >= 6:
        sub = state_data.get("room")
        return sub if isinstance(sub, dict) else None
    return state_data


def restore_state(
    state_data: Optional[dict],
    config: RoomConfig,
) -> RoomState:
    """Rebuild a :class:`RoomState` from a snapshot dict.

    Accepts v1 (legacy) through v6. v6 snapshots have the v5-shape
    fields nested under ``state_data["room"]``; older versions have them
    at the top level. Any unrecognised keys (``mode``, ``debate`` from
    v1) are silently ignored — they don't exist on the v0 RoomState. If
    ``state_data`` is ``None`` (missing/corrupt snapshot), returns a
    fresh empty state with the given config — caller should then replay
    events to repopulate.

    P0.3: every scalar field is type-checked via :func:`_coerce_int` /
    :func:`_coerce_str_or_none` and bad participant entries are skipped
    rather than propagated. A tampered ``room_state.json`` cannot
    inject e.g. an integer participant id that confuses the regex
    parser downstream, nor a boolean ``room_epoch`` that breaks
    arithmetic.
    """
    state = RoomState(config=config)
    sub = _room_sub_dict(state_data)
    if sub is None:
        return state
    state_data = sub

    state.room_epoch = _coerce_int(state_data.get("room_epoch", 0), 0)
    state.topic = _coerce_str_or_none(state_data.get("topic"))
    state.anchor_id = _coerce_str_or_none(state_data.get("anchor_id"))
    state.chair_id = _coerce_str_or_none(state_data.get("chair_id"))
    state.default_responder_id = _coerce_str_or_none(state_data.get("default_responder_id"))
    state.default_summarizer_id = _coerce_str_or_none(state_data.get("default_summarizer_id"))
    cur_ut = state_data.get("current_user_turn_id")
    state.current_user_turn_id = (
        cur_ut if (cur_ut is None or _coerce_int(cur_ut, -1) == cur_ut) else None
    )
    state.last_compacted_event_id = _coerce_int(state_data.get("last_compacted_event_id", -1), -1)

    raw_participants = state_data.get("participants", [])
    if not isinstance(raw_participants, list):
        raw_participants = []
    for p_data in raw_participants:
        if not isinstance(p_data, dict):
            continue
        pid = p_data.get("id")
        if not isinstance(pid, str) or not pid:
            # Skip rather than propagate — a non-string id would crash
            # the routing regex parser and the prompt renderer.
            continue
        capable = p_data.get("capable", True)
        if not isinstance(capable, bool):
            capable = True
        cost_tier = _coerce_int(p_data.get("cost_tier", 0), 0)
        active = p_data.get("active", True)
        if not isinstance(active, bool):
            active = True
        role_hints = p_data.get("role_hints", {})
        if not isinstance(role_hints, dict):
            role_hints = {}
        info = ParticipantInfo(
            id=pid,
            capable=capable,
            cost_tier=cost_tier,
            active=active,
            role_hints=role_hints,
        )
        state.participants[info.id] = info
        # Add directly to bypass the epoch bump from add_participant —
        # we're restoring, not mutating live state.

    # Room control state. v1 snapshots predate this section; default
    # to a fresh :class:`RoomControlState` (open floor, normal style).
    control_data = state_data.get("control")
    if isinstance(control_data, dict):
        roles = control_data.get("roles") or {}
        if not isinstance(roles, dict):
            roles = {}
        # ``floor_owner`` was removed from kernel state in v0.2. Older
        # (v1-v4) snapshots may still carry the field; we read but
        # discard it (no equivalent state in v5+).
        style = control_data.get("style") or "normal"
        if style not in ("brief", "normal", "detailed"):
            style = "normal"
        # Round-robin fields. v3 / v4 snapshots may carry the retired
        # ``turn_taking_mode`` field; we ignore it — a non-empty
        # ``turn_order`` is the round-robin signal in v5.
        raw_order = control_data.get("turn_order") or []
        if not isinstance(raw_order, list):
            raw_order = []
        try:
            next_idx = int(control_data.get("next_speaker_idx", 0))
        except (TypeError, ValueError):
            next_idx = 0
        if next_idx < 0:
            next_idx = 0
        state.control = RoomControlState(
            roles={str(k): str(v) for k, v in roles.items()},
            wait_for_user=bool(control_data.get("wait_for_user", False)),
            style=cast(StyleLevel, style),
            turn_order=[str(p) for p in raw_order],
            next_speaker_idx=next_idx,
        )
        # P2.3 topic-merge shim: pre-v4 snapshots stored a separate
        # ``control.active_goal``. When loading them, fold the value
        # into ``state.topic`` so the merged single-source-of-truth
        # field carries forward. ``state.topic`` set above wins; only
        # fill from ``active_goal`` if topic is empty.
        legacy_goal = control_data.get("active_goal")
        if legacy_goal and not state.topic:
            state.topic = str(legacy_goal)

    return state


def restore_kernel_state(
    state_data: Optional[dict],
    config: RoomConfig,
) -> KernelState:
    """Rebuild a v0.3 :class:`KernelState` from a snapshot dict.

    The v6 envelope nests the v5-shape room fields under ``"room"`` and
    carries reserved sibling slots for v0.3 subsystem states. PR 1 reads
    the ``"room"`` sub-dict via :func:`restore_state` and the
    ``"kernel_version"`` counter; the subsystem slots are intentionally
    not consumed yet — their owning PRs (5, 6, 13) wire that in.

    Accepts older v1–v5 snapshots transparently — they are migrated
    in-memory by :func:`_migrate_v5_to_v6` before consumption (the
    on-disk file is not rewritten; the next clean :meth:`Journal.snapshot`
    replaces it with v6 shape). ``None`` / corrupt input returns a fresh
    empty :class:`KernelState` with the given config.
    """
    room = restore_state(state_data, config)
    kernel = new_kernel_state(room)
    if isinstance(state_data, dict):
        ver = state_data.get("version")
        if isinstance(ver, int) and ver >= 6:
            # v6 envelope carries an explicit kernel_version counter.
            kv = _coerce_int(state_data.get("kernel_version", 0), 0)
            if kv < 0:
                kv = 0
            kernel.version = kv
        # else: v1–v5 had no kernel_version; leave at default 0.
        # v0.3.x PR 2: v7 envelopes carry a ``"context"`` slot;
        # older envelopes are migrated to v7 in-memory by
        # :func:`_migrate_v6_to_v7` (applied at the load boundary)
        # before this restore runs, so the slot is always present
        # by the time we read it.
        if isinstance(ver, int) and ver >= 7:
            kernel.context = context_state_from_jsonable(
                state_data.get("context")
            )
    return kernel
