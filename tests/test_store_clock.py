"""Event log replay, simulated clock, business hours."""

from datetime import date, datetime, timedelta

import pytest

from dme.clock import SimClock, Task, Timer, add_business_days, business_time
from dme.data_loader import load_case
from dme.events import (
    CaseOpened,
    OrderReceived,
    StateChanged,
    SupplierUpdated,
    TimerClosed,
    TimerScheduled,
)
from dme.models import CaseState, SupplierStatus
from dme.store import EventStore
from dme.view import CaseView
from tests.helpers import HALSTED, T0, good_order


def timer(i, at):
    return Timer(timer_id=i, at=at, task=Task(kind="x"))


def test_replay_rebuilds_the_same_state(tmp_path):
    events = [
        CaseOpened(at=T0, case=load_case()),
        StateChanged(at=T0, old="new", new="need_patient_info", reason="r"),
        TimerScheduled(at=T0, timer=timer(1, T0)),
        TimerScheduled(at=T0, timer=timer(2, T0 + timedelta(days=2))),
        TimerScheduled(at=T0, timer=timer(3, T0 + timedelta(days=3))),
        TimerClosed(at=T0, timer_id=1),
        TimerClosed(at=T0, timer_id=3),
        SupplierUpdated(at=T0, supplier_id=HALSTED, status="declined", reason="out_of_stock"),
        OrderReceived(at=T0, contact_no=1, document="doc", order=good_order(), defects=[]),
    ]
    live = CaseView()
    for e in events:
        live.apply(e)
    store = EventStore(tmp_path)
    store.append("c", *events[:4])
    store.append("c", *events[4:])
    loaded = store.load("c")
    assert loaded == live
    assert loaded.state is CaseState.NEED_PATIENT_INFO
    assert list(loaded.timers) == [2]
    assert loaded.next_timer_id == 4
    assert loaded.case.suppliers[HALSTED].status is SupplierStatus.DECLINED
    assert loaded.case.written_order == good_order()


def test_torn_last_line_is_dropped_and_log_stays_usable(tmp_path):
    store = EventStore(tmp_path)
    store.append("c", CaseOpened(at=T0, case=load_case()))
    with store.path("c").open("a") as f:
        f.write('{"type": "state_chan')
    assert store.load("c").state is CaseState.NEW
    store.append("c", StateChanged(at=T0, old="new", new="need_patient_info", reason="r"))
    assert store.load("c").state is CaseState.NEED_PATIENT_INFO


def test_timer_fires_at_plus_two_days_and_not_before():
    clock, fired = SimClock(T0), []
    clock.schedule(timer(1, T0 + timedelta(days=2)))
    clock.advance_to(T0 + timedelta(days=2, seconds=-1), lambda t: fired.append(t))
    assert fired == []
    clock.advance_to(T0 + timedelta(days=2), lambda t: fired.append(t))
    assert [t.timer_id for t in fired] == [1]


def test_timers_fire_in_order_and_cancelled_ones_do_not():
    clock, fired = SimClock(T0), []
    for i, h in [(1, 5), (2, 1), (3, 3)]:
        clock.schedule(timer(i, T0 + timedelta(hours=h)))
    clock.cancel(3)
    clock.advance_until_idle(lambda t: fired.append(t.timer_id))
    assert fired == [2, 1]
    assert clock.now == T0 + timedelta(hours=5)


def test_runaway_loop_is_stopped():
    clock = SimClock(T0)
    clock.schedule(timer(1, T0))
    with pytest.raises(RuntimeError, match="runaway"):
        clock.advance_until_idle(lambda t: clock.schedule(t), max_events=20)


@pytest.mark.parametrize(
    "given, expected",
    [
        (datetime(2026, 11, 23, 10, 0), datetime(2026, 11, 23, 10, 0)),
        (datetime(2026, 11, 23, 7, 0), datetime(2026, 11, 23, 9, 0)),
        (datetime(2026, 11, 23, 17, 0), datetime(2026, 11, 24, 9, 0)),
        (datetime(2026, 11, 25, 18, 0), datetime(2026, 11, 27, 9, 0)),  # over Thanksgiving
        (datetime(2026, 11, 28, 12, 0), datetime(2026, 11, 30, 9, 0)),  # Saturday
    ],
)
def test_business_time(given, expected):
    assert business_time(given) == expected


def test_add_business_days_skips_weekends_and_holidays():
    assert add_business_days(date(2026, 11, 23), 2) == date(2026, 11, 25)
    assert add_business_days(date(2026, 11, 25), 1) == date(2026, 11, 27)
    assert add_business_days(date(2026, 11, 27), 1) == date(2026, 11, 30)
    assert add_business_days(date(2026, 11, 30), -1) == date(2026, 11, 27)
