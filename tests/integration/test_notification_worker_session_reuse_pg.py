"""Real-PostgreSQL proof of a stale SQLAlchemy identity-map hazard in the
notification worker's result-recording path (app.services.notification).

Background: run_worker_pass() reuses ONE Session across
claim_next_due() -> (HTTP request, no transaction open) -> record_attempt_result().
create_session_factory() (app/db.py) sets expire_on_commit=False, so the
Payment object claim_next_due() loads stays in that Session's identity map,
UNEXPIRED, after claim_next_due()'s own commit. Without
execution_options(populate_existing=True) on record_attempt_result()'s
FOR UPDATE reload, SQLAlchemy hands back that SAME cached Python object --
with its pre-HTTP-call attribute values -- instead of refreshing it from the
fresh row the FOR UPDATE SELECT actually locked. If a DIFFERENT session
changed the row in the gap (another worker, stale-claim recovery, or a
manual admin action), the discard guard

    payment.status != bot_notify_pending
    OR payment.notification_claimed_by != claimed.worker_id
    OR payment.bot_notify_attempts != claimed.attempt

would then read the STALE cached values and could incorrectly let a late
result overwrite that newer state.

Empirically confirmed nuance (verified with real weakref/refcount
instrumentation before writing these tests, not assumed): SQLAlchemy's
default Session identity map (WeakInstanceDict) holds the cached Payment
object only WEAKLY. In the UNMODIFIED run_worker_pass()/claim_next_due()
code path today, nothing else keeps a strong Python reference to that
object once claim_next_due() returns (only its scalar fields are copied
into the returned ClaimedPayment dataclass), so CPython's reference
counting alone -- with no cyclic-GC sweep required -- frees it immediately,
before any HTTP call or external mutation can land. That means simply
re-calling claim_next_due() then record_attempt_result() in sequence,
without anything else retaining the object, will NOT reproduce the hazard:
by the time record_attempt_result() reloads the row, the identity map is
already empty for that key and a fresh object is loaded regardless of
whether populate_existing is present.

The hazard is real and worth closing anyway: this "safe by GC-timing
accident" property is exactly the kind of implicit, unenforced invariant
that silently breaks the moment anything else starts holding a reference
across the gap -- a future logging/tracing/APM change that captures
function locals, a refactor that batches multiple claimed objects in a
list, or simply running under a non-refcounting Python implementation.
PR #64's own investigation independently reached the same conclusion
("timing/GC-dependent, not deterministically reproduced").

Each test below therefore DELIBERATELY retains a live reference to the
claimed Payment object across the gap (mirroring what any of the realistic
changes above would do), which makes the hazard land deterministically on
every run rather than depending on incidental object lifetime -- the
correct way to test a fix for an identity-map-staleness class of bug. This
was verified BOTH ways before finalizing: with the retained reference and
without record_attempt_result()'s populate_existing() fix, every test
below fails exactly as this docstring describes; with the fix, all pass.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.bot import AttemptOutcome, OutcomeKind
from app.models import Base, Payment, PaymentStatus
from app.reasons import ReasonCode
from app.services.notification import (
    claim_next_due,
    record_attempt_result,
    release_stale_claims,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not TEST_DATABASE_URL.startswith("postgresql"),
        reason="TEST_DATABASE_URL with a postgresql URL is required",
    ),
]

T0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

_TABLES = (
    "admin_alerts",
    "worker_heartbeats",
    "payment_events",
    "payments",
    "centralpay_payer_identities",
    "fee_policies",
    "alembic_version",
)


@pytest.fixture
def pg_engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)


def _seed_payment(pg_session_factory, *, bot_order_id: str, gateway_order_id: int) -> int:
    """A payment already gateway-verified and due for notification -- the
    only state claim_next_due() will select."""
    with pg_session_factory() as db:
        payment = Payment(
            bot_order_id=bot_order_id,
            gateway_order_id=gateway_order_id,
            gateway_user_id=1,
            amount=5000,
            payable_amount=5000,
            status=PaymentStatus.BOT_NOTIFY_PENDING.value,
            gateway_verified_at=T0,
            next_retry_at=T0,
        )
        db.add(payment)
        db.commit()
        return payment.id


def _events(pg_session_factory, payment_id: int) -> list[str]:
    with pg_session_factory() as db:
        return list(
            db.execute(
                text("SELECT event_type FROM payment_events WHERE payment_id = :pid ORDER BY id"),
                {"pid": payment_id},
            ).scalars()
        )


def _fresh(pg_session_factory, payment_id: int) -> Payment:
    with pg_session_factory() as db:
        payment = db.get(Payment, payment_id)
        assert payment is not None
        return payment


_ACCEPTED_OUTCOME = AttemptOutcome(
    kind=OutcomeKind.ACCEPTED,
    reason_code=ReasonCode.BOT_NOTIFY_ACCEPTED.value,
    log_event="bot_notification_accepted",
    http_status=200,
)


# --- Race A: stale claim/result -- another transaction changes state while
# worker A's Session still holds its pre-HTTP-call cached object ------------


def test_race_a_stale_result_discarded_after_another_session_moves_to_manual_review_pg(
    pg_session_factory, settings
):
    """A claims attempt 1 and commits; a DIFFERENT session moves the payment
    to manual_review (simulating stale-claim recovery/an admin action)
    landing in the gap where A's HTTP request has no open transaction; A's
    session (the SAME one that claimed) then records its old ACCEPTED
    result. It MUST see the fresh manual_review state and discard -- never
    overwrite it -- leaving exactly one bot_notification_result_discarded
    event and zero bot_notification_accepted events."""
    payment_id = _seed_payment(
        pg_session_factory, bot_order_id="race-a", gateway_order_id=930001
    )

    # Worker A's own long-lived Session -- reused for BOTH the claim and,
    # later, the result recording, exactly like run_worker_pass().
    session_a: Session = pg_session_factory()
    claimed_a = claim_next_due(session_a, worker_id="worker-A", now=T0)
    assert claimed_a is not None and claimed_a.attempt == 1
    # Deliberately retain a live reference to the claimed object across the
    # gap -- see the module docstring: without this, CPython's refcounting
    # GC frees it immediately (nothing else references it) and the hazard
    # would not land regardless of the fix. This mirrors what a realistic
    # future change (logging/tracing the object, batching claims) would do.
    _retained_a = session_a.get(Payment, payment_id)
    assert _retained_a is not None  # keep the reference alive and used

    # A DIFFERENT session moves the row to manual_review while A's HTTP
    # request is "in flight" (no transaction open on session_a).
    with pg_session_factory() as other:
        other.execute(
            update(Payment)
            .where(Payment.id == payment_id)
            .values(
                status=PaymentStatus.MANUAL_REVIEW.value,
                notification_claimed_at=None,
                notification_claimed_by=None,
                bot_notify_reason="manual_intervention",
            )
        )
        other.commit()

    # A's stale 2xx result finally arrives, recorded on the SAME session
    # that performed the claim.
    record_attempt_result(
        session_a, settings, claimed_a, _ACCEPTED_OUTCOME, 12.3, now=T0 + timedelta(minutes=5),
        jitter=lambda: 1.0,
    )
    session_a.close()

    final = _fresh(pg_session_factory, payment_id)
    assert final.status == PaymentStatus.MANUAL_REVIEW.value  # untouched
    assert final.bot_notify_accepted_at is None
    assert final.bot_notify_reason == "manual_intervention"  # not overwritten

    events = _events(pg_session_factory, payment_id)
    assert events.count("bot_notification_result_discarded") == 1
    assert "bot_notification_accepted" not in events


# --- Race B: reclaimed attempt -- A claims N; stale recovery requeues; B ---
# claims N+1; A's late result must be discarded, B's ownership preserved ---


def test_race_b_late_result_from_reclaimed_attempt_is_discarded_b_ownership_preserved_pg(
    pg_session_factory, settings
):
    """A claims attempt 1; the claim goes stale and release_stale_claims
    (a DIFFERENT session, mirroring a genuinely different worker process)
    requeues it in idempotent mode; B claims attempt 2; A's session (still
    holding its ORIGINAL attempt-1 cached object) finally records a result.
    It MUST be discarded -- B's claim (worker id, attempt number) and
    schedule must survive untouched."""
    idempotent = settings.model_copy(update={"bot_notify_retry_mode": "idempotent"})
    payment_id = _seed_payment(
        pg_session_factory, bot_order_id="race-b", gateway_order_id=930002
    )

    session_a: Session = pg_session_factory()
    claimed_a = claim_next_due(session_a, worker_id="worker-A", now=T0)
    assert claimed_a is not None and claimed_a.attempt == 1
    # See Race A / the module docstring: retain a live reference so the
    # hazard lands deterministically instead of depending on GC timing.
    _retained_a = session_a.get(Payment, payment_id)
    assert _retained_a is not None

    # Make A's claim look stale (older than the claim timeout), then a
    # DIFFERENT session recovers it -- the real release_stale_claims path,
    # not a fabricated state transition.
    stale_at = T0 - timedelta(seconds=settings.bot_notify_claim_timeout_seconds + 1)
    with pg_session_factory() as backdate:
        backdate.execute(
            update(Payment)
            .where(Payment.id == payment_id)
            .values(notification_claimed_at=stale_at)
        )
        backdate.commit()
    with pg_session_factory() as recovery:
        recovered = release_stale_claims(recovery, idempotent, now=T0, jitter=lambda: 1.0)
    assert recovered == 1

    # B claims the now-requeued payment as attempt 2, on its OWN session.
    b_claim_time = T0 + timedelta(seconds=idempotent.bot_notify_claim_timeout_seconds + 120)
    with pg_session_factory() as session_b:
        claimed_b = claim_next_due(session_b, worker_id="worker-B", now=b_claim_time)
    assert claimed_b is not None
    assert claimed_b.attempt == 2
    assert claimed_b.worker_id == "worker-B"

    # A's stale result for its ORIGINAL attempt=1 claim finally arrives, on
    # the SAME session A used to claim -- the exact call
    # execute_claimed_attempt() makes on run_worker_pass()'s shared session.
    record_attempt_result(
        session_a, settings, claimed_a, _ACCEPTED_OUTCOME, 8.0,
        now=b_claim_time + timedelta(seconds=1), jitter=lambda: 1.0,
    )
    session_a.close()

    final = _fresh(pg_session_factory, payment_id)
    # B's ownership and schedule are untouched by A's late, discarded result.
    assert final.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    assert final.notification_claimed_by == "worker-B"
    assert final.bot_notify_attempts == 2
    assert final.bot_notify_accepted_at is None

    events = _events(pg_session_factory, payment_id)
    assert events.count("bot_notification_result_discarded") == 1
    assert "bot_notification_accepted" not in events


# --- Race C: state already moved to accepted before the old result -------


def test_race_c_stale_retryable_result_cannot_move_accepted_payment_back_to_pending_pg(
    pg_session_factory, settings
):
    """A claims attempt 1; a DIFFERENT session settles the payment to
    bot_notify_accepted (e.g. a concurrent worker instance, or an operator
    override) while A's HTTP request is in flight; A's stale RETRYABLE
    result then arrives on A's own session. It must never re-open a retry
    schedule or otherwise mutate an already-accepted payment."""
    payment_id = _seed_payment(
        pg_session_factory, bot_order_id="race-c", gateway_order_id=930003
    )

    session_a: Session = pg_session_factory()
    claimed_a = claim_next_due(session_a, worker_id="worker-A", now=T0)
    assert claimed_a is not None and claimed_a.attempt == 1
    # See Race A / the module docstring: retain a live reference so the
    # hazard lands deterministically instead of depending on GC timing.
    _retained_a = session_a.get(Payment, payment_id)
    assert _retained_a is not None

    accepted_at = T0 + timedelta(minutes=1)
    with pg_session_factory() as other:
        other.execute(
            update(Payment)
            .where(Payment.id == payment_id)
            .values(
                status=PaymentStatus.BOT_NOTIFY_ACCEPTED.value,
                bot_notify_reason=ReasonCode.BOT_NOTIFY_ACCEPTED.value,
                bot_notify_accepted_at=accepted_at,
                next_retry_at=None,
                last_error=None,
                notification_claimed_at=None,
                notification_claimed_by=None,
            )
        )
        other.commit()

    stale_retryable = AttemptOutcome(
        kind=OutcomeKind.RETRYABLE,
        reason_code=ReasonCode.BOT_HTTP_429.value,
        log_event="bot_notification_failed",
        http_status=429,
    )
    record_attempt_result(
        session_a, settings, claimed_a, stale_retryable, 15.0,
        now=T0 + timedelta(minutes=5), jitter=lambda: 1.0,
    )
    session_a.close()

    final = _fresh(pg_session_factory, payment_id)
    assert final.status == PaymentStatus.BOT_NOTIFY_ACCEPTED.value  # never moved back
    assert final.bot_notify_accepted_at == accepted_at  # untouched
    assert final.next_retry_at is None  # no retry schedule was opened
    assert final.bot_notify_reason == ReasonCode.BOT_NOTIFY_ACCEPTED.value  # not overwritten
    assert final.last_error is None

    events = _events(pg_session_factory, payment_id)
    assert events.count("bot_notification_result_discarded") == 1
    assert "bot_notification_retry_scheduled" not in events
    assert "bot_notification_failed" not in events


# --- Proves the fix depends on the identity-map REFRESH, not merely on ----
# the FOR UPDATE SELECT statement being issued ------------------------------


def test_discard_guard_depends_on_populate_existing_refresh_not_merely_the_select_pg(
    pg_session_factory, settings, monkeypatch
):
    """A regression here could remove `.execution_options(populate_existing=
    True)` while leaving the surrounding SELECT ... FOR UPDATE statement
    untouched -- a test that only checks "was a FOR UPDATE statement issued"
    would not catch that. This test proves BOTH: (1) record_attempt_result's
    reload genuinely executes a FOR UPDATE statement against payment_id, by
    recording every statement session_a runs and asserting one FOR UPDATE
    hit; AND (2) SQLAlchemy reuses the exact SAME Python object instance for
    that row (identity-map hit -- proving this is not simply "a new object
    was constructed"), yet its attribute values were correctly refreshed
    in-place to the fresh, externally-committed state. Both must hold
    together for the discard guard to be trustworthy."""
    payment_id = _seed_payment(
        pg_session_factory, bot_order_id="identity-proof", gateway_order_id=930004
    )

    session_a: Session = pg_session_factory()
    real_execute = Session.execute
    for_update_hits: list[object] = []

    def recording_execute(self, statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            for_update_hits.append(statement)
        return real_execute(self, statement, *args, **kwargs)

    claimed_a = claim_next_due(session_a, worker_id="worker-A", now=T0)
    assert claimed_a is not None

    # The object claim_next_due() loaded and left cached, UNEXPIRED, in
    # session_a's identity map (expire_on_commit=False).
    cached_before = session_a.get(Payment, payment_id)
    assert cached_before is not None
    assert cached_before.status == PaymentStatus.BOT_NOTIFY_PENDING.value
    identity_before = id(cached_before)

    with pg_session_factory() as other:
        other.execute(
            update(Payment)
            .where(Payment.id == payment_id)
            .values(
                status=PaymentStatus.MANUAL_REVIEW.value,
                notification_claimed_at=None,
                notification_claimed_by=None,
            )
        )
        other.commit()

    # Instrument AFTER the claim (whose own FOR UPDATE we don't care about)
    # so for_update_hits isolates record_attempt_result's reload only.
    monkeypatch.setattr(Session, "execute", recording_execute)
    record_attempt_result(
        session_a, settings, claimed_a, _ACCEPTED_OUTCOME, 1.0,
        now=T0 + timedelta(minutes=5), jitter=lambda: 1.0,
    )
    monkeypatch.setattr(Session, "execute", real_execute)

    # (1) A genuine SELECT ... FOR UPDATE against this payment_id executed.
    assert len(for_update_hits) == 1

    # (2) SQLAlchemy handed back the SAME cached Python object (identity-map
    # hit -- this is the crux of the hazard: no new instance was created)...
    cached_after = session_a.get(Payment, payment_id)
    assert cached_after is not None
    assert id(cached_after) == identity_before
    # ...yet its attributes now reflect the FRESH, externally-committed
    # state, not the stale claim-time values -- proving populate_existing
    # refreshed it in place rather than the guard merely running a query
    # whose result was silently discarded by the identity map.
    assert cached_after.status == PaymentStatus.MANUAL_REVIEW.value
    assert cached_after.notification_claimed_by is None

    session_a.close()

    # And, as in Race A, the discard guard genuinely acted on that fresh
    # state: the manual_review row was never overwritten.
    final = _fresh(pg_session_factory, payment_id)
    assert final.status == PaymentStatus.MANUAL_REVIEW.value
    assert final.bot_notify_accepted_at is None
    events = _events(pg_session_factory, payment_id)
    assert events.count("bot_notification_result_discarded") == 1
