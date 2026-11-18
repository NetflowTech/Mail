"""Python's cycle collector must run on the GUI thread only: on a worker
thread it can destroy a closed dialog while the GUI thread uses it, and AMail
deadlocks (seen when the organization signed an account out with its
settings window recently closed)."""
from __future__ import annotations

import gc
import threading


def test_collector_runs_from_a_gui_timer(qapp):
    from emailclient.app import _collect_garbage_on_gui_thread
    try:
        _collect_garbage_on_gui_thread(qapp)
        assert not gc.isenabled()
        assert qapp._gc_timer.isActive()

        class Cycle:
            pass
        freed_on = []

        class Probe:
            def __del__(self):
                freed_on.append(threading.current_thread() is threading.main_thread())

        def make_garbage_on_a_worker():
            a, b = Cycle(), Cycle()
            a.b, b.a, a.p = b, a, Probe()
            for _ in range(5000):      # enough allocations to trip the counters
                [object() for _ in range(3)]
        t = threading.Thread(target=make_garbage_on_a_worker)
        t.start()
        t.join()
        assert freed_on == []            # nothing collected on the worker
        gc.collect()                     # what the GUI timer does
        assert freed_on == [True]
    finally:
        qapp._gc_timer.stop()
        gc.enable()
