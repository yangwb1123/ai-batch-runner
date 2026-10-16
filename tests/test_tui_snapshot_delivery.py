"""Fresh coalesced-presentation regression on the exact frozen 45-overlay source.

These cases describe presentation admission only: confirmed runtime facts stay
in the controller regardless of whether the UI notice is refused, and no case
claims provider, Codex or device acceptance.
"""
from dataclasses import replace

import pytest

from test_tui_fold_admission import App, consume, rows, setup


def _worker_app():
    app = App()
    app._ui_thread_id = -1
    app.is_mounted = True
    app.notices = []
    app.applied = []
    app.post_message = lambda message: (app.notices.append(message), True)[1]
    app.call_from_thread = lambda fn, *args: fn(*args)
    return app


def _begin(app, events=None):
    for event in (events or rows()[:2]):
        assert app._ingest_runtime_event(event) is True


def _deltas(app, count, *, start_seq, prefix):
    base = rows()[1]
    accepted = []
    for index in range(count):
        event = replace(base, event_id=f'evt_{prefix}_{index}', seq=start_seq + index,
                        kind='message.delta',
                        payload={'item_id': 'pres_msg', 'text': f'{index:04d}'})
        assert app._ingest_runtime_event(event) is True
        accepted.append(event)
    return accepted


def _terminal(app, seq, *, prefix='pres'):
    base = rows()[-1]
    event = replace(base, event_id=f'evt_{prefix}_done', seq=seq,
                    kind='turn.completed', payload={'state': 'SUCCEEDED'})
    assert app._ingest_runtime_event(event) is True
    return event


def _dispatch(app, queue=None):
    queue = app.notices if queue is None else queue
    while queue:
        app.on_runtime_snapshot_ready(queue.pop(0))


def test_blocked_ui_cannot_delay_terminal_admission(monkeypatch):
    app, operation, handle, clock, closed = setup(monkeypatch)
    app._ui_thread_id = -1
    app.is_mounted = True
    app.notices = []
    app.post_message = lambda message: (app.notices.append(message), True)[1]
    blocked = []

    def busy_ui(fn, *args):
        blocked.append(1)
        clock[0] = 105.0
        return fn(*args)

    app.call_from_thread = busy_ui
    try:
        result = consume(app, handle, rows(), 'managed')
        assert result['success'] is True
        assert not blocked
        assert len(app.notices) == 1
        _dispatch(app)
        assert app._runtime_view.turn_state == 'SUCCEEDED'
    finally:
        app._event_controller.close()


def test_many_snapshots_post_one_notice_and_apply_latest():
    app = _worker_app()
    try:
        _begin(app)
        _deltas(app, 1000, start_seq=3, prefix='pres')
        _terminal(app, 1003)
        snapshot = app._event_controller.snapshot()
        assert len(app.notices) == 1
        assert len(snapshot.assistant_text) == 4000
        assert snapshot.assistant_text.endswith('0999')
        _dispatch(app)
        assert app._runtime_view.revision == snapshot.revision
        assert app._runtime_view.assistant_text == snapshot.assistant_text
    finally:
        app._event_controller.close()


def test_stale_notice_cannot_consume_the_newer_pending_snapshot():
    app = _worker_app()
    try:
        _begin(app)
        _deltas(app, 2, start_seq=3, prefix='first')
        assert len(app.notices) == 1
        stale = app.notices[0]
        _dispatch(app)
        applied = app._runtime_view.revision
        _deltas(app, 2, start_seq=5, prefix='second')
        assert len(app.notices) == 1
        app.on_runtime_snapshot_ready(stale)
        assert app._runtime_view.revision == applied
        assert app.notices, 'the stale replay consumed the pending notice'
        latest = app._event_controller.snapshot()
        _dispatch(app)
        assert app._runtime_view.revision == latest.revision
    finally:
        app._event_controller.close()


def test_refused_notice_keeps_facts_and_allows_a_new_notice():
    app = _worker_app()
    refusals = []
    posts = []

    def refuse(message):
        refusals.append(message)
        return False

    app.post_message = refuse
    try:
        _begin(app)
        _deltas(app, 3, start_seq=3, prefix='refused')
        assert refusals and not posts
        snapshot = app._event_controller.snapshot()
        assert snapshot.assistant_text == '000000010002'
        app.post_message = lambda message: (posts.append(message), True)[1]
        _deltas(app, 1, start_seq=6, prefix='after')
        assert len(posts) == 1
        _dispatch(app, posts)
        assert app._runtime_view.revision == app._event_controller.snapshot().revision
    finally:
        app._event_controller.close()


def test_unmounted_app_drops_late_presentation_without_resurrecting_it():
    app = _worker_app()
    try:
        initial = app._runtime_view.revision
        _begin(app)
        _deltas(app, 2, start_seq=3, prefix='late')
        assert len(app.notices) == 1
        app.is_mounted = False
        _dispatch(app)
        assert app._runtime_view.revision == initial
        app.is_mounted = True
        _dispatch(app)
        assert app._runtime_view.revision == initial
        _deltas(app, 1, start_seq=5, prefix='fresh')
        _dispatch(app)
        assert app._runtime_view.revision == app._event_controller.snapshot().revision
    finally:
        app._event_controller.close()


def test_final_rendezvous_applies_terminal_without_waiting_for_the_notice():
    app = _worker_app()
    app._projection_driven = True
    try:
        _begin(app)
        _deltas(app, 2, start_seq=3, prefix='finish')
        _terminal(app, 5)
        app._capture_runtime_finish()
        app._apply_runtime_finish()
        assert app._runtime_view.turn_state == 'SUCCEEDED'
        _dispatch(app)
        assert app._runtime_view.revision == app._event_controller.snapshot().revision
    finally:
        app._event_controller.close()


def test_delivery_reidentify_fences_the_old_controller_and_ticket():
    from pbatch.tui_snapshot_delivery import SnapshotDelivery

    class View:
        def __init__(self, revision):
            self.revision = revision

    posts = []
    first, second = object(), object()
    delivery = SnapshotDelivery(posts.append, first)
    view = View(1)
    assert delivery.offer(first, view) is True
    assert len(posts) == 1
    assert delivery.take(posts[0].delivery_id, posts[0].ticket) is view
    delivery.reidentify(second)
    assert delivery.take(posts[0].delivery_id, posts[0].ticket) is None
    assert delivery.offer(first, View(2)) is False
    assert delivery.offer(second, View(3)) is True
    delivery.close()
    assert delivery.take(posts[1].delivery_id, posts[1].ticket) is None
    assert delivery.offer(second, View(4)) is False


def test_delivery_coalesces_and_ignores_stale_or_replayed_tickets():
    from pbatch.tui_snapshot_delivery import SnapshotDelivery

    class View:
        def __init__(self, revision):
            self.revision = revision

    identity = object()
    posts = []
    delivery = SnapshotDelivery(posts.append, identity)
    first, second = View(1), View(2)
    assert delivery.offer(identity, first) is True
    assert delivery.offer(identity, second) is True
    assert len(posts) == 1
    assert delivery.take(posts[0].delivery_id, 999) is None
    assert delivery.take(posts[0].delivery_id, posts[0].ticket) is second
    assert delivery.take(posts[0].delivery_id, posts[0].ticket) is None
    assert delivery.offer(identity, View(1)) is False
    assert delivery.offer(identity, None) is False


def test_refused_post_clears_only_its_own_pending_ticket():
    from pbatch.tui_snapshot_delivery import SnapshotDelivery

    class View:
        def __init__(self, revision):
            self.revision = revision

    identity = object()
    posts, accepted = [], []

    def post(message):
        posts.append(message)
        return bool(accepted)

    delivery = SnapshotDelivery(post, identity)
    assert delivery.offer(identity, View(1)) is True
    assert len(posts) == 1 and not accepted
    accepted.append(True)
    assert delivery.offer(identity, View(2)) is True
    assert len(posts) == 2, 'a refused notice must allow exactly one new post'
    assert delivery.take(posts[0].delivery_id, posts[0].ticket) is None
    assert delivery.take(posts[1].delivery_id, posts[1].ticket).revision == 2
    assert delivery.take(posts[1].delivery_id, posts[1].ticket) is None
