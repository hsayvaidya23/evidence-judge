from __future__ import annotations

import time

from evidence_judge.core.evidence import Channel, NetworkEvent, Run
from evidence_judge.ingest import attach_har

BUDGET_S = 2.0


def network_run(source: str, count: int) -> Run:
    return Run(
        run_id="r",
        source=source,
        network=[
            NetworkEvent(
                id=f"net#{i}",
                ts=float(i),
                method="POST" if i % 10 == 0 else "GET",
                url=f"https://shop.test/api/x{i % 2000}",
                status=200,
            )
            for i in range(count)
        ],
        captured={Channel.NETWORK},
    )


def test_attach_har_is_linear() -> None:
    trace = network_run("playwright-trace", 20_000)
    har = network_run("har", 20_000)
    har = har.model_copy(update={"network": har.network[::-1]})

    start = time.perf_counter()
    merged = attach_har(trace, har)
    elapsed = time.perf_counter() - start

    assert len(merged.network) == 20_000
    assert elapsed < BUDGET_S
