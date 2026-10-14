from __future__ import annotations

from evidence_judge.core.claims import ClaimKind
from evidence_judge.core.evidence import (
    ActionEvent,
    Channel,
    NetworkEvent,
    PageState,
    Run,
    Screenshot,
)
from evidence_judge.core.verdict import (
    Citation,
    ClaimVerdict,
    Dimension,
    DimensionScore,
    ExpectationResult,
    Outcome,
    RunVerdict,
    Support,
    Usage,
)

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201b1f6b9a50000000049454e44ae426082"
)
EVIL = "<script>alert('xss')</script>"


def make_verdict(
    outcome: Outcome = Outcome.FAIL,
    text: str = "Ordered the kettle",
    quote: str = "I placed the order!",
) -> RunVerdict:
    return RunVerdict(
        judge_version="0.1.0",
        run_id="run-42",
        task="Buy a red kettle under $30",
        outcome=outcome,
        score=0.42,
        summary="The order request returned 500; the agent claimed success.",
        claims=[
            ClaimVerdict(
                claim_id="c1",
                claim_text=text,
                quote=quote,
                kind=ClaimKind.OUTCOME,
                key=True,
                status=Support.CONTRADICTED,
                reason="POST /api/orders returned 500",
                citations=[Citation(evidence_id="net#2", excerpt="500 Internal Server Error")],
                checker="request",
                confidence=1.0,
            ),
            ClaimVerdict(
                claim_id="c2",
                claim_text="Saw the confirmation page",
                quote="the confirmation page appeared",
                kind=ClaimKind.STATE,
                key=False,
                status=Support.SUPPORTED,
                reason="Screenshot shows confirmation",
                citations=[Citation(evidence_id="shot#1")],
                checker="screenshot",
                confidence=0.8,
            ),
            ClaimVerdict(
                claim_id="c3",
                claim_text="Applied a coupon",
                quote="applied a coupon",
                kind=ClaimKind.ACTION,
                key=False,
                status=Support.NO_EVIDENCE,
                reason="No coupon request captured",
                checker="request",
                confidence=1.0,
            ),
        ],
        expectations=[
            ExpectationResult(
                expectation_id="order_submitted",
                kind="expect",
                met=False,
                reason="No 2xx POST to /api/orders",
                citations=[Citation(evidence_id="net#2")],
            ),
            ExpectationResult(
                expectation_id="price", kind="constraint", met=None, reason="Not captured"
            ),
            ExpectationResult(expectation_id="no_account", kind="forbid", met=True, reason="ok"),
        ],
        dimensions={
            Dimension.STATE: DimensionScore(score=None, findings=["No snapshots captured"]),
            Dimension.SUCCESS: DimensionScore(score=0.0, findings=["Order failed"]),
            Dimension.SIDE_EFFECTS: DimensionScore(score=1.0),
        },
        claim_coverage=0.9,
        deterministic_share=0.67,
        warnings=["HAR had 3 truncated bodies"],
        usage=Usage(
            llm_calls=2, input_tokens=1200, output_tokens=300, cost_usd=0.0123, latency_ms=850
        ),
    )


def make_run() -> Run:
    return Run(
        run_id="run-42",
        source="playwright-trace",
        task="Buy a red kettle under $30",
        agent_answer="Ordered the kettle",
        actions=[
            ActionEvent(id="act#1", ts=100, name="goto", url="https://shop.test/"),
            ActionEvent(id="act#2", ts=900, name="click", selector="#buy", error="Timeout 5000ms"),
        ],
        network=[
            NetworkEvent(id="net#1", ts=150, method="GET", url="https://shop.test/", status=200),
            NetworkEvent(
                id="net#2", ts=1000, method="POST", url="https://shop.test/api/orders", status=500
            ),
        ],
        screenshots=[
            Screenshot(id="shot#1", ts=1100, mime_type="image/png", resource="s1.png"),
            Screenshot(id="shot#2", ts=1200, mime_type="image/png", resource="s2.png"),
        ],
        final_state=PageState(url="https://shop.test/cart", title="Cart", screenshot_id="shot#2"),
        captured={Channel.ACTIONS, Channel.NETWORK, Channel.SCREENSHOTS},
        blobs={"s1.png": PNG_1PX, "s2.png": PNG_1PX},
    )
