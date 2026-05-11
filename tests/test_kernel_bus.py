"""Tests for ``loom.kernel.bus`` — the MessageBus."""
from __future__ import annotations

import json
import threading
import time
import unittest

from loom.kernel import events as ev
from loom.kernel.bus import _KERNEL_AUTH, _KernelAuth, MessageBus, visible_to


class PostingAssignsIds(unittest.TestCase):
    def test_post_assigns_monotonic_ids(self):
        bus = MessageBus()
        a = ev.chat(sender="user", body="one")
        b = ev.chat(sender="user", body="two")
        ida = bus.post(a)
        idb = bus.post(b)
        self.assertEqual(ida, 0)
        self.assertEqual(idb, 1)
        self.assertEqual(a.id, 0)
        self.assertEqual(b.id, 1)
        self.assertGreater(a.ts, 0)
        self.assertLess(a.ts, time.time() + 1)

    def test_post_after_stop_returns_minus_one(self):
        bus = MessageBus()
        bus.stop()
        result = bus.post(ev.chat(sender="user", body="ignored"))
        self.assertEqual(result, -1)

    def test_concurrent_multi_producer_ids_are_unique_and_contiguous(self):
        bus = MessageBus()
        start = threading.Barrier(6)

        def producer(pid: int):
            start.wait(timeout=1.0)
            for i in range(25):
                bus.post(ev.chat(sender=f"p{pid}", body=f"{pid}:{i}"))

        threads = [
            threading.Thread(target=producer, args=(i,))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        start.wait(timeout=1.0)
        for t in threads:
            t.join(timeout=2.0)

        self.assertTrue(all(not t.is_alive() for t in threads))
        ids = [e.id for e in bus.snapshot()]
        self.assertEqual(len(ids), 125)
        self.assertEqual(sorted(ids), list(range(125)))


class WaitAfter(unittest.TestCase):
    def test_returns_immediately_when_already_past_idx(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="user", body="one"))
        new_len = bus.wait_after(0, timeout=0.01)
        self.assertEqual(new_len, 1)

    def test_blocks_until_post(self):
        bus = MessageBus()
        result = []

        def waiter():
            result.append(bus.wait_after(0, timeout=2.0))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)            # let the waiter start blocking
        self.assertEqual(result, []) # still blocked
        bus.post(ev.chat(sender="user", body="hi"))
        t.join(timeout=1.0)
        self.assertEqual(result, [1])

    def test_times_out(self):
        bus = MessageBus()
        start = time.time()
        new_len = bus.wait_after(0, timeout=0.05)
        elapsed = time.time() - start
        self.assertLess(elapsed, 0.5)
        self.assertEqual(new_len, 0)

    def test_stop_wakes_waiters(self):
        bus = MessageBus()
        result = []

        def waiter():
            result.append(bus.wait_after(0, timeout=5.0))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        bus.stop()
        t.join(timeout=1.0)
        self.assertEqual(result, [0])


class SnapshotFilters(unittest.TestCase):
    def setUp(self):
        self.bus = MessageBus()
        self.bus.post(ev.chat(sender="user", body="public hi"))
        self.bus.post(ev.chat(sender="user", body="psst",
                              channel="dm:claude_code"))
        self.bus.post(ev.chat(sender="claude_code", body="back at you",
                              channel="dm:claude_code"))
        self.bus.post(ev.system("session started"))
        self.bus.post(ev.topic_changed(None, "the moon"))

    def test_no_filters_returns_all(self):
        self.assertEqual(len(self.bus.snapshot()), 5)

    def test_filter_by_channel_main(self):
        snap = self.bus.snapshot(channel="main")
        self.assertEqual(len(snap), 3)
        self.assertTrue(all(e.channel == "main" for e in snap))

    def test_filter_by_dm_channel(self):
        snap = self.bus.snapshot(channel="dm:claude_code")
        self.assertEqual(len(snap), 2)
        self.assertTrue(all(e.channel == "dm:claude_code" for e in snap))

    def test_audience_user_sees_everything(self):
        snap = self.bus.snapshot(audience="user")
        self.assertEqual(len(snap), 5)

    def test_audience_target_sees_own_dm(self):
        snap = self.bus.snapshot(audience="claude_code")
        self.assertEqual(len(snap), 5)

    def test_audience_other_does_not_see_dm(self):
        snap = self.bus.snapshot(audience="gemini_cli")
        # Main channel (3) plus system DM access — gemini_cli is NOT
        # the dm target, so the 2 dm:claude_code events are hidden.
        self.assertEqual(len(snap), 3)
        bodies = [e.body for e in snap if e.kind == "chat"]
        self.assertIn("public hi", bodies)

    def test_filter_by_kinds(self):
        snap = self.bus.snapshot(kinds=["chat"])
        self.assertEqual(len(snap), 3)
        self.assertTrue(all(e.kind == "chat" for e in snap))

    def test_filter_since(self):
        # since=1 → events with id > 1, so ids {2, 3, 4} = 3 events.
        snap = self.bus.snapshot(since=1)
        self.assertEqual([e.id for e in snap], [2, 3, 4])

    def test_combine_channel_and_audience(self):
        snap = self.bus.snapshot(channel="dm:claude_code",
                                 audience="gemini_cli")
        self.assertEqual(snap, [])

    def test_combine_kinds_and_since(self):
        snap = self.bus.snapshot(kinds=["chat"], since=0)
        # All chat events with id > 0: ids 1 and 2 (the two DM chats).
        self.assertEqual([e.id for e in snap], [1, 2])


class Subscribers(unittest.TestCase):
    def test_subscribe_called_on_every_post(self):
        bus = MessageBus()
        seen = []
        bus.subscribe(lambda e: seen.append(e.body))
        bus.post(ev.chat(sender="user", body="one"))
        bus.post(ev.chat(sender="user", body="two"))
        self.assertEqual(seen, ["one", "two"])

    def test_unsubscribe_handle_stops_callbacks(self):
        bus = MessageBus()
        seen = []
        unsub = bus.subscribe(lambda e: seen.append(e.body))
        bus.post(ev.chat(sender="user", body="one"))
        unsub()
        bus.post(ev.chat(sender="user", body="two"))
        self.assertEqual(seen, ["one"])

    def test_unsubscribe_is_idempotent(self):
        bus = MessageBus()
        unsub = bus.subscribe(lambda e: None)
        unsub()
        # Second call must not raise.
        unsub()

    def test_subscriber_exception_does_not_break_bus(self):
        bus = MessageBus()
        seen_after = []

        def boom(_e):
            raise RuntimeError("intentional")

        bus.subscribe(boom)
        bus.subscribe(lambda e: seen_after.append(e.body))
        bus.post(ev.chat(sender="user", body="hi"))
        # The healthy subscriber still ran; the bus is not stopped;
        # subsequent posts still work.
        self.assertEqual(seen_after, ["hi"])
        self.assertEqual(len(bus.snapshot()), 1)


class VisibilityRules(unittest.TestCase):
    def test_main_channel_visible_to_all(self):
        e = ev.chat(sender="user", body="hi")
        self.assertTrue(visible_to(e, "user"))
        self.assertTrue(visible_to(e, "claude_code"))
        self.assertTrue(visible_to(e, "anybody"))

    def test_dm_visible_to_target_and_user_and_system(self):
        e = ev.chat(sender="user", body="psst", channel="dm:claude_code")
        self.assertTrue(visible_to(e, "claude_code"))
        self.assertTrue(visible_to(e, "user"))
        self.assertTrue(visible_to(e, "system"))
        self.assertFalse(visible_to(e, "gemini_cli"))

    def test_unknown_private_channel_visible_to_nobody(self):
        e = ev.chat(sender="user", body="secret", channel="private:x")
        self.assertFalse(visible_to(e, "user"))
        self.assertFalse(visible_to(e, "system"))
        self.assertFalse(visible_to(e, "x"))


class Stopped(unittest.TestCase):
    def test_stopped_property(self):
        bus = MessageBus()
        self.assertFalse(bus.stopped)
        bus.stop()
        self.assertTrue(bus.stopped)


class GetById(unittest.TestCase):
    """:meth:`MessageBus.get` is the O(1) by-id lookup added in the
    perf pass. ``ev.id`` equals position in the log, so ``bus.get(k)``
    returns ``self._log[k]`` when in range, ``None`` otherwise.
    """

    def test_get_returns_event_by_id(self):
        bus = MessageBus()
        a = ev.chat(sender="user", body="zero")
        b = ev.chat(sender="user", body="one")
        bus.post(a)
        bus.post(b)
        self.assertIs(bus.get(0), a)
        self.assertIs(bus.get(1), b)

    def test_get_out_of_range_returns_none(self):
        bus = MessageBus()
        bus.post(ev.chat(sender="user", body="hi"))
        self.assertIsNone(bus.get(-1))
        self.assertIsNone(bus.get(7))

    def test_get_empty_bus_returns_none(self):
        self.assertIsNone(MessageBus().get(0))


class RenderMemo(unittest.TestCase):
    """:meth:`MessageBus.render_chat_line` and ``render_control_line``
    JSON-render once per ``ev.id`` then return the cached string.

    Events are immutable after id/ts assignment in :meth:`post`, so a
    cached render is forever stable. Each replying actor in a turn
    rebuilds its prompt by walking the same events; the memo turns
    that O(P × E) cost into O(P × E_uncached) where E_uncached is the
    set of events newly committed since the last render.
    """

    def test_chat_line_renders_expected_shape(self):
        bus = MessageBus()
        e = ev.chat(sender="alice", body="hi", addressees=["bob"])
        bus.post(e)
        line = bus.render_chat_line(e, scope="main")
        d = json.loads(line)
        self.assertEqual(d["sender"], "alice")
        self.assertEqual(d["body"], "hi")
        self.assertEqual(d["addressees"], ["bob"])
        self.assertEqual(d["scope"], "main")
        self.assertEqual(d["id"], e.id)

    def test_chat_line_caches_by_id_and_scope(self):
        bus = MessageBus()
        e = ev.chat(sender="alice", body="hi")
        bus.post(e)
        first_main = bus.render_chat_line(e, scope="main")
        second_main = bus.render_chat_line(e, scope="main")
        self.assertIs(first_main, second_main)
        first_dm = bus.render_chat_line(e, scope="dm")
        # Distinct scope → distinct render, populated separately.
        self.assertNotEqual(first_main, first_dm)
        self.assertIs(first_dm, bus.render_chat_line(e, scope="dm"))

    def test_control_line_renders_expected_shape(self):
        bus = MessageBus()
        e = ev.user_turn_opened(
            user_turn_id=1, routing_case="direct_mention",
            required_participants=["bob"], rationale="@bob")
        bus.post(e)
        line = bus.render_control_line(e)
        d = json.loads(line)
        self.assertEqual(d["kind"], "control")
        self.assertEqual(d["control_type"], "user_turn_opened")
        self.assertEqual(d["body"]["user_turn_id"], 1)

    def test_control_line_caches_by_id(self):
        bus = MessageBus()
        e = ev.user_turn_closed(user_turn_id=1, reason="completed")
        bus.post(e)
        self.assertIs(bus.render_control_line(e), bus.render_control_line(e))


class LockReleasedFanOut(unittest.TestCase):
    """v0.2: subscriber fan-out runs AFTER the bus lock is released.

    Slow subscribers must not block other writers. The append +
    notify_all are still under the lock (preserves
    ``ev.id == position``), but each subscriber runs without the lock
    so a slow callback cannot freeze the bus.
    """

    def test_slow_subscriber_does_not_hold_bus_lock(self):
        # While a slow subscriber is mid-flight on one writer thread,
        # readers (snapshot/get/__len__) on the main thread must not
        # block on the bus lock. Pre-v0.2 the lock was held across
        # subscriber fan-out, freezing every reader until the
        # subscriber returned.
        import threading as _t
        import time as _time
        bus = MessageBus()
        slow_called = _t.Event()
        slow_release = _t.Event()

        def _slow(event):
            slow_called.set()
            slow_release.wait(timeout=2.0)

        bus.subscribe(_slow)

        def _stuck_writer():
            bus.post(ev.chat(sender="user", body="first"))

        stuck = _t.Thread(target=_stuck_writer)
        stuck.start()
        slow_called.wait(timeout=1.0)
        self.assertTrue(slow_called.is_set(), "subscriber never fired")

        # Slow subscriber is mid-flight. Readers on the main thread
        # must complete quickly because the bus lock is free.
        t0 = _time.monotonic()
        n = len(bus)
        snap = bus.snapshot()
        got = bus.get(0)
        elapsed = _time.monotonic() - t0
        self.assertEqual(n, 1)
        self.assertEqual(len(snap), 1)
        self.assertIsNotNone(got)
        self.assertLess(
            elapsed, 0.1,
            f"reader paths blocked for {elapsed:.3f}s — bus lock "
            "must be released before subscriber fan-out")

        # Release the slow callback so the stuck thread can finish.
        slow_release.set()
        stuck.join(timeout=2.0)
        self.assertFalse(stuck.is_alive())


class KernelAuthRequired(unittest.TestCase):
    """Hardens invariant 9 — post_internal requires _KERNEL_AUTH token.

    Replaces the old "5 closed callers" convention with a structural
    identity check. Policy code cannot acquire the token because the
    kernel/policy import boundary forbids `loom.policy.*` from
    importing `loom.kernel.bus`.
    """

    def test_post_internal_without_auth_raises(self):
        bus = MessageBus()
        e = ev.chat(sender="system", body="x")
        with self.assertRaises(TypeError):
            # ``auth`` is required keyword-only; omitting it must error
            # at the call boundary.
            bus.post_internal(e)  # type: ignore[call-arg]

    def test_post_internal_wrong_auth_raises(self):
        bus = MessageBus()
        e = ev.chat(sender="system", body="x")
        # A separately constructed _KernelAuth() is not the singleton —
        # identity check fails.
        impostor = _KernelAuth()
        with self.assertRaises(RuntimeError):
            bus.post_internal(e, auth=impostor)
        # ``None`` and other non-_KernelAuth values must also fail.
        with self.assertRaises(RuntimeError):
            bus.post_internal(e, auth=None)  # type: ignore[arg-type]

    def test_post_internal_with_correct_auth_succeeds(self):
        bus = MessageBus()
        e = ev.chat(sender="system", body="x")
        rid = bus.post_internal(e, auth=_KERNEL_AUTH)
        self.assertEqual(rid, 0)

    def test_kernel_auth_not_re_exported_from_loom_init(self):
        import loom
        # The token is module-private to loom.kernel.bus and must not
        # leak into the public ``loom`` surface.
        self.assertFalse(hasattr(loom, "_KERNEL_AUTH"))
        self.assertFalse(hasattr(loom, "_KernelAuth"))


if __name__ == "__main__":
    unittest.main()
