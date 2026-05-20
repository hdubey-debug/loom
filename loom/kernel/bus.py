"""Loom v0 — MessageBus.

Thread-safe append-only event ledger with publish/subscribe semantics.
Single source of truth for the protocol: every actor reads from the bus
via ``snapshot()`` / ``wait_after()`` and posts back via ``post()``.

Subscribers (renderer, journal writer, cost tracker) are notified
inline on the poster's thread. Subscriber exceptions are swallowed so
a misbehaving subscriber can never break the bus or stall actors.

Visibility rules for ``audience`` filtering:

- ``main``          visible to everyone
- ``dm:<target>``   visible to ``target``, the user, and the system

This module is deliberately simple — the protocol's interesting state
lives in :class:`RoomState` and :class:`RoomCoordinator`.

Security contract (see ``docs/security-model.md``):

- ``post`` does NOT authenticate the ``sender`` field. Today the
  architecture relies on every bus poster being in-process,
  code-reviewed, and trusted. Treating draft handlers, adapters,
  subscribers, and proxy iterators as part of the trust boundary is
  a documented assumption (audit finding C1; sender authentication
  via thread-local actor binding is the planned P1 hardening).
- Subscribers run synchronously, inline, under the bus lock. Any
  subscriber MUST complete in milliseconds — anything longer freezes
  every actor in the room (audit finding CON1). Long-running work
  belongs on a background thread (the journal's snapshot writer is
  the canonical example). Subscriber exceptions are swallowed;
  subscriber latency is not bounded.
- DM privacy is enforced at the actor / prompt boundary via
  :func:`visible_to`, NOT as in-process access control. ``Room`` /
  ``Runtime`` expose ``self.bus`` publicly (audit finding D3); any
  in-process caller can read every DM via ``bus.snapshot()`` without
  an audience filter. If process-level isolation is required, route
  callers through audience-gated facade methods.
- The render memo (``_render_chat_main_cache`` /
  ``_render_chat_dm_cache``) is keyed by ``(ev.id, scope)``. If a
  future change introduces audience-specific render variants, the
  cache key MUST include audience or DMs will silently misroute
  (audit finding D2).
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional
import json
import threading
import time

from loom.errors import LoomError
from loom.kernel.events import Event, EventKind, control_type_of


SubscriberCallback = Callable[[Event], None]
UnsubscribeHandle = Callable[[], None]
UnbindHandle = Callable[[], None]


DEFAULT_MAX_BODY_BYTES = 256 * 1024  # 256 KB — RES4 / P2.1 default cap


class _KernelAuth:
    """Sentinel granting privileged :meth:`MessageBus.post_internal` access.

    Module-private by convention: the only legitimate instance is
    :data:`_KERNEL_AUTH`, defined immediately below and never
    re-exported from :mod:`loom`. The kernel boundary contract is
    enforced both statically (policy modules must not import from
    :mod:`loom.kernel.bus`) and structurally (``post_internal`` checks
    ``auth is _KERNEL_AUTH`` by identity, so a different
    ``_KernelAuth()`` constructed from elsewhere does not unlock it).
    """

    __slots__ = ()


_KERNEL_AUTH: "_KernelAuth" = _KernelAuth()


class BodyOversizeError(LoomError, ValueError):
    """Raised by :meth:`MessageBus.post` when a chat / system / summary
    event's string body exceeds the bus's configured ``max_body_bytes``
    (default 256 KB; audit finding RES4, P2.1).

    Bounds the single-message DoS vector: a participant (human paste
    or hostile agent) cannot post a 10 MB body that the bus stores,
    the journal writes, the render memo caches, and every other
    actor's prompt re-renders. Streaming-delta texts are expected to
    be truncated at the proxy boundary; this is the last-line defense
    at the bus.

    Inherits from :class:`loom.errors.LoomError` so user code can catch
    via ``except LoomError``, and from :class:`ValueError` for back-compat.
    """


class SenderMismatchError(LoomError, ValueError):
    """Raised by :meth:`MessageBus.post` when a thread bound to actor
    ``X`` posts an event with ``sender != X`` (audit finding C1, P1).

    Catches all three concrete forgery vectors the audit identified:

    - C1.a: bound thread tries to post ``stream_end`` carrying another
      lease's ``lease_id`` / ``participant_id``.
    - C1.b: bound thread tries to post a control event with
      ``sender="system"``.
    - C1.c: bound thread tries to post a chat event with
      ``sender="user"``.

    Privileged kernel-internal callers (coordinator, runtime, journal
    replay, the actor's own ``actor_error`` emission) bypass the check
    via :meth:`MessageBus.post_internal`. Unbound threads (test code,
    runtime entry points, the coordinator thread) skip the check
    entirely; binding only applies to actor-loop threads.

    Inherits from :class:`loom.errors.LoomError` so user code can catch
    via ``except LoomError``, and from :class:`ValueError` for back-compat.
    """


def visible_to(ev: Event, audience: str) -> bool:
    """Return whether ``audience`` can see ``ev``.

    Audience is a participant id, ``"user"``, or ``"system"``. Main
    channel events are visible to everyone. DM channel events are
    visible to the named target, the user, and the system; nobody else.
    """
    if ev.channel == "main":
        return True
    if ev.channel.startswith("dm:"):
        target = ev.channel.split(":", 1)[1]
        return audience in (target, "user", "system")
    # Unknown channel — treat as private to nobody (defensive).
    return False


class MessageBus:
    def __init__(self, *, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self._cond = threading.Condition()
        self._log: list[Event] = []
        # P2.1 / audit RES4: cap the byte-size of chat / system /
        # summary bodies. Default 256 KB covers any reasonable
        # human-typed reply plus generous slack for pasted code; the
        # runtime can raise to 4 MB for high-context use cases. Stream-
        # delta texts are bounded at the proxy boundary, NOT here.
        self._max_body_bytes = max_body_bytes
        # Subscribers stored as an immutable tuple — rebuilt on
        # subscribe/unsubscribe under the bus lock. The hot ``post``
        # path can iterate the tuple directly without copying it.
        self._subscribers: tuple[SubscriberCallback, ...] = ()
        self._stopped = False
        # Per-event JSON-render memos — populated on first call to
        # :meth:`render_chat_line` / :meth:`render_control_line`. Keyed
        # by ``ev.id`` (and scope for chat). Bus events are immutable
        # after ``id``/``ts`` are assigned (see :meth:`post`), so cached
        # entries are forever stable. Memo memory is O(E) — bounded by
        # what's still in the bus log; a future log-compaction pass
        # would prune both together.
        self._render_chat_main_cache: dict[int, str] = {}
        self._render_chat_dm_cache: dict[int, str] = {}
        self._render_control_cache: dict[int, str] = {}
        # P1 / sender authentication — thread-local actor binding (audit
        # Option B). Threads register via :meth:`bind_actor` when they
        # enter ParticipantActor's loop; :meth:`post` then enforces
        # ``ev.sender == bound_id`` for that thread. Privileged kernel
        # callers (coordinator / runtime / replay / failure-callback)
        # bypass via :meth:`post_internal`. Lookups are O(1) on a plain
        # dict guarded by the bus condition.
        self._thread_actors: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    def post(self, ev: Event) -> int:
        """Append ``ev`` to the log; return its assigned id, or -1 if stopped.

        The bus assigns ``ev.id`` (monotonic = position in log) and
        ``ev.ts`` (epoch seconds). All waiters on :meth:`wait_after` are
        notified. Subscribers run synchronously on the caller's thread,
        under the bus lock, so every subscriber observes events in the
        same monotonic id order as the log. Any exception they raise is
        suppressed. Subscribers must complete quickly — long-running
        work must be pushed to a background thread (the journal does
        this for snapshot writes).

        Sender authentication (P1, audit finding C1): if the calling
        thread is bound to an actor id (via :meth:`bind_actor`), the
        event's ``sender`` field MUST match the bound id. A bound
        thread that tries to post with any other sender raises
        :class:`SenderMismatchError`. Unbound threads bypass the check
        — privileged kernel callers (coordinator, runtime, replay)
        either run on unbound threads or use :meth:`post_internal`.
        """
        bound = self._thread_actors.get(threading.get_ident())
        if bound is not None and ev.sender != bound:
            raise SenderMismatchError(
                f"thread bound to actor {bound!r} cannot post event "
                f"with sender={ev.sender!r}; use post_internal for "
                "privileged emission"
            )
        return self._post_unchecked(ev)

    def post_internal(self, ev: Event, *, auth: "_KernelAuth") -> int:
        """Privileged post — bypasses the thread-actor binding check.

        Same semantics as :meth:`post` otherwise. Requires the caller
        to supply the module-private :data:`_KERNEL_AUTH` token via
        the ``auth`` keyword argument; any other value (including
        ``None`` or a freshly constructed :class:`_KernelAuth`) raises
        :class:`RuntimeError`. The identity check elevates the
        previously convention-only "kernel-internal callers only" rule
        into a structural guarantee — policy code cannot acquire the
        token because :mod:`loom.kernel.bus` is unreachable from
        :mod:`loom.policy` (kernel/policy import boundary).

        Legitimate kernel-internal call sites:

        - The coordinator (sender = "system" for control events).
        - The runtime (sender = "user" for human input).
        - Journal replay (re-injects events with their original
          sender; replay is by definition a privileged operation —
          the disk content has already been validated by
          :meth:`Event.from_jsonl`).
        - The journal failure callback (posts ``journal_error`` with
          sender = "system" from whichever thread tripped the failure,
          which may be a bound actor thread).
        - The actor's own crash-handler (``actor_error`` with sender =
          "system" from the actor's own bound thread).

        Test code and benchmarks may import :data:`_KERNEL_AUTH`
        directly — they live in-tree and are part of the trust
        boundary. The name's leading underscore and the absence from
        :mod:`loom` re-exports keep external consumers from picking it
        up by accident.
        """
        if auth is not _KERNEL_AUTH:
            raise RuntimeError(
                "post_internal requires the kernel-private _KERNEL_AUTH "
                "token; this method is reserved for kernel-internal "
                "callers (coordinator/runtime/journal/actor crash path)"
            )
        return self._post_unchecked(ev)

    def _post_unchecked(self, ev: Event) -> int:
        """The core append path — no sender authentication.

        The ``max_body_bytes`` cap (RES4 / P2.1) is enforced HERE so
        privileged callers (``post_internal``, replay, coordinator)
        and authenticated callers (``post``) get the same defense.
        Stream-delta bodies are exempt — those are bounded at the
        proxy boundary, where chunk size is naturally limited.

        v0.2: subscriber fan-out runs AFTER the bus lock is released.
        The append + ``notify_all`` are protected (preserves the
        ``ev.id == position`` invariant); subscriber callbacks run
        without the lock so a slow subscriber cannot block other
        writers. ``self._subscribers`` is an immutable tuple
        snapshotted under the lock, so concurrent
        subscribe/unsubscribe is safe — the loop sees the snapshot
        from lock-release time. Across-subscriber ordering is
        relaxed: a subscriber may observe event N before another
        subscriber observes event N-1, but each subscriber still
        sees events in append order.
        """
        if ev.kind in ("chat", "system", "summary"):
            body = ev.body
            if isinstance(body, str) and len(body) > self._max_body_bytes:
                raise BodyOversizeError(
                    f"{ev.kind} body of {len(body)} bytes exceeds "
                    f"max_body_bytes={self._max_body_bytes}; truncate "
                    "at the proxy boundary or raise the bus cap"
                )
        # v0.3.x PR 1 (doctrine P21): every event carries a non-empty
        # thread_id. The Event dataclass defaults this to ``"main"``;
        # this assertion catches any future caller that explicitly sets
        # ``thread_id=None`` or to an empty string.
        if not isinstance(ev.thread_id, str) or not ev.thread_id:
            raise ValueError(f"event thread_id must be a non-empty string, got {ev.thread_id!r}")
        with self._cond:
            if self._stopped:
                return -1
            ev.id = len(self._log)
            ev.ts = time.time()
            self._log.append(ev)
            self._cond.notify_all()
            subscribers_snapshot = self._subscribers
        # Lock released. Fan out without holding it so a slow callback
        # cannot freeze the next ``post``.
        for cb in subscribers_snapshot:
            try:
                cb(ev)
            except Exception:
                # A misbehaving subscriber must not break the bus.
                pass
        return ev.id

    # ------------------------------------------------------------------
    # Sender authentication — thread-local actor binding (P1)
    # ------------------------------------------------------------------

    def bind_actor(self, actor_id: str) -> UnbindHandle:
        """Bind the calling thread to ``actor_id``.

        While bound, :meth:`post` requires ``ev.sender == actor_id``;
        any other sender raises :class:`SenderMismatchError`. The
        returned callable removes the binding when invoked. Used by
        :meth:`ParticipantActor._loop` to enforce sender authenticity
        for the actor's own thread without touching test code paths
        that drive the bus directly.

        Re-binding on a thread that already has a binding is allowed
        only when the new id matches the existing one — re-binding to
        a different id raises ``RuntimeError``, since that would
        usually indicate a thread re-use bug.
        """
        tid = threading.get_ident()
        with self._cond:
            existing = self._thread_actors.get(tid)
            if existing is not None and existing != actor_id:
                raise RuntimeError(
                    f"thread {tid} is already bound to actor {existing!r}; "
                    f"refusing to rebind to {actor_id!r}"
                )
            self._thread_actors[tid] = actor_id

        def _unbind() -> None:
            with self._cond:
                # Only remove if still bound to this actor (re-bind
                # raised; defensive against double-unbind in a finally).
                if self._thread_actors.get(tid) == actor_id:
                    del self._thread_actors[tid]

        return _unbind

    def unbind_actor(self) -> None:
        """Unbind the calling thread from any actor.

        Idempotent — calling on an unbound thread is a no-op. Provided
        as a convenience; the unbind handle returned from
        :meth:`bind_actor` is the preferred call style in finally
        clauses (preserves which actor is being unbound).
        """
        tid = threading.get_ident()
        with self._cond:
            self._thread_actors.pop(tid, None)

    def bound_actor_for(self, thread_ident: Optional[int] = None) -> Optional[str]:
        """Return the actor id bound to ``thread_ident`` (default: current)."""
        tid = thread_ident if thread_ident is not None else threading.get_ident()
        with self._cond:
            return self._thread_actors.get(tid)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def wait_after(self, idx: int, timeout: Optional[float] = None) -> int:
        """Block until ``len(log) > idx`` OR bus stopped OR timeout.

        Returns the current ``len(log)`` after waking. If the bus has
        been stopped, returns the current length even if no new events
        arrived.
        """
        with self._cond:
            self._cond.wait_for(
                lambda: len(self._log) > idx or self._stopped,
                timeout=timeout,
            )
            return len(self._log)

    def snapshot(
        self,
        *,
        channel: Optional[str] = None,
        audience: Optional[str] = None,
        kinds: Optional[Iterable[EventKind]] = None,
        since: Optional[int] = None,
    ) -> list[Event]:
        """Return a filtered slice of the log.

        - ``channel`` if set, restrict to events on this channel exactly.
        - ``audience`` if set, drop events the audience cannot see
          (DM-privacy filter). Combinable with ``channel``.
        - ``kinds`` if set, restrict to events whose ``kind`` is in the
          collection.
        - ``since`` if set, drop events with ``id <= since``. Useful for
          actor cursors.
        """
        # ``ev.id`` is the position in ``self._log`` (set in :meth:`post`),
        # so ``since`` collapses to a slice — O(E - since) instead of an
        # O(E) full copy + post-filter. This is the dominant per-actor
        # path: every wakeup calls ``snapshot(since=cursor)``.
        with self._cond:
            if since is None:
                log = list(self._log)
            else:
                start = since + 1
                if start <= 0:
                    log = list(self._log)
                elif start >= len(self._log):
                    log = []
                else:
                    log = self._log[start:]
        if channel is not None:
            log = [e for e in log if e.channel == channel]
        if audience is not None:
            log = [e for e in log if visible_to(e, audience)]
        if kinds is not None:
            ks = set(kinds)
            log = [e for e in log if e.kind in ks]
        return log

    def get(self, event_id: int) -> Optional[Event]:
        """Return the event with id ``event_id``, or ``None`` if not present.

        ``ev.id == position in _log`` (assigned in :meth:`post`), so this
        is O(1). Replaces the snapshot+scan that callers used to do for
        a single-id lookup.
        """
        with self._cond:
            if 0 <= event_id < len(self._log):
                ev = self._log[event_id]
                if ev.id == event_id:
                    return ev
        return None

    def render_chat_line(self, ev: Event, *, scope: str) -> str:
        """Cached JSON render of a chat event for prompt assembly.

        Populates a per-event memo on first call. Subsequent calls (from
        every replying actor's :func:`build_prompt`) return the memoized
        string without re-running ``json.dumps``. Cache keys are
        ``(ev.id, scope)`` — ``scope`` is ``"main"`` or ``"dm"``.

        Locking note: the memo dicts are written without holding the
        bus lock — the GIL makes single-key dict ops atomic, and a
        race that has two threads compute the same string and both
        store it is harmless (idempotent). This keeps the prompt-build
        path off the bus lock.
        """
        cache = self._render_chat_main_cache if scope == "main" else self._render_chat_dm_cache
        cached = cache.get(ev.id)
        if cached is not None:
            return cached
        out = json.dumps(
            {
                "id": ev.id,
                "ts": ev.ts,
                "sender": ev.sender,
                "addressees": ev.addressees,
                "scope": scope,
                "body": ev.body,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        cache[ev.id] = out
        return out

    def render_control_line(self, ev: Event) -> str:
        """Cached JSON render of a control event for prompt assembly.

        Same shape as :meth:`render_chat_line`. The memo keys on
        ``ev.id`` only (control events have a single render shape).
        """
        cached = self._render_control_cache.get(ev.id)
        if cached is not None:
            return cached
        body = ev.body if isinstance(ev.body, dict) else {}
        out = json.dumps(
            {
                "id": ev.id,
                "ts": ev.ts,
                "kind": "control",
                "control_type": control_type_of(ev),
                "body": {k: v for k, v in body.items() if k != "control_type"},
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self._render_control_cache[ev.id] = out
        return out

    def __len__(self) -> int:
        with self._cond:
            return len(self._log)

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def subscribe(self, callback: SubscriberCallback) -> UnsubscribeHandle:
        """Register ``callback`` to be invoked on every post.

        Returns an idempotent unsubscribe handle.
        """
        with self._cond:
            self._subscribers = self._subscribers + (callback,)

        def _unsubscribe() -> None:
            with self._cond:
                # Remove at most one matching subscriber (preserves the
                # original ``list.remove(callback)`` semantics for the
                # idempotent-on-double-call case the docstring promises).
                subs = list(self._subscribers)
                try:
                    subs.remove(callback)
                except ValueError:
                    return
                self._subscribers = tuple(subs)

        return _unsubscribe

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Mark stopped and wake all waiters.

        After ``stop()``, ``post`` returns ``-1`` and ``wait_after``
        returns immediately. Subscribers are not notified by ``stop()``
        itself; if you need a final event, post it before stopping.
        """
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    @property
    def stopped(self) -> bool:
        with self._cond:
            return self._stopped
