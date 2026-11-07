# Sports Court Booking Backend --- System Design Document

**Status:** Revised design for mentor review\
**Stack:** Python 3.11, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 15,
pytest, Ruff, mypy, Docker Compose

> **Decision policy:** This document records chosen defaults, not
> implemented features. The implementation snapshot in Section 15
> identifies what currently exists. Any change to a default requires a
> documented requirement or mentor-approved decision.

## 1. Problem, scope, and goals

The club rents courts by time slot across multiple venues. Members need
to view availability, create a short-lived hold, confirm it, cancel
bookings under a refund policy, and request weekly recurring bookings.
Staff need weekly court-utilization reports.

The core correctness requirement is that two overlapping active
reservations for the same court must never both commit, including under
concurrent requests.

Goals: - Expire unconfirmed holds after five minutes. - Make
confirmation retries idempotent. - Keep each database workflow atomic. -
Enforce at most three active future bookings per member. - Apply
venue-specific peak/off-peak pricing and the cancellation rule. - Prove
concurrency behavior against PostgreSQL 15.

### In scope

Reservation/hold state, availability, recurring occurrence scheduling,
pricing calculation, cancellation/refund eligibility calculation, and
utilization reporting.

### Out of scope

External payment-provider integration, actual money movement, payment
webhooks, and provider-specific retry/reconciliation. This service
records the cancellation/refund decision only.
Authentication/authorization is not fully designed yet; until it is, the
existing `member_id` request field is a development-only limitation and
must not be treated as a trusted production identity.

## 2. Technology and component responsibilities

-   **FastAPI:** HTTP endpoints and mapping domain errors to HTTP
    responses.
-   **Pydantic:** request validation and response schemas.
-   **Service layer:** booking rules and workflow coordination, separate
    from HTTP handlers.
-   **SQLAlchemy:** ORM and database sessions/transactions.
-   **PostgreSQL 15:** source of truth and final integrity/concurrency
    enforcement.
-   **Alembic:** versioned schema changes.
-   **pytest:** unit, integration, rollback, retry, and concurrency
    tests.
-   **Docker Compose:** repeatable local PostgreSQL environment.

## 3. System architecture

``` mermaid
flowchart TD
    Client["Member / Staff Client"]
    API["FastAPI Routes"]
    Schema["Pydantic Schemas"]
    Service["Booking Service Layer"]
    ORM["SQLAlchemy Session / Transaction"]
    DB[("PostgreSQL 15")]
    Migrations["Alembic Migrations"]
    Tests["pytest Integration Tests"]

    Client --> API
    API --> Schema
    API --> Service
    Service --> ORM
    ORM --> DB
    Migrations --> DB
    Tests --> API
    Tests --> DB
```

Routes validate and translate HTTP data but do not own business rules. A
service-level availability check improves errors; PostgreSQL remains the
final authority for conflicting writes.

## 4. Proposed database model

``` mermaid
erDiagram
    MEMBERS ||--o{ RESERVATIONS : makes
    VENUES ||--o{ COURTS : contains
    COURTS ||--o{ RESERVATIONS : receives
    VENUES ||--o{ PRICING_RULES : configures
    MEMBERS ||--o{ RECURRING_SERIES : requests
    COURTS ||--o{ RECURRING_SERIES : schedules
    RECURRING_SERIES ||--o{ RESERVATIONS : generates
    RESERVATIONS ||--o| REFUND_DECISIONS : records

    MEMBERS {
        int id PK
        string name
        string email UK
    }
    VENUES {
        int id PK
        string name
        string address
        string timezone
    }
    COURTS {
        int id PK
        string name
        string sport_type
        int venue_id FK
    }
    RESERVATIONS {
        int id PK
        int member_id FK
        int court_id FK
        timestamptz start_time
        timestamptz end_time
        string status
        timestamptz hold_expires_at
        numeric price
        int recurring_series_id FK
    }
    PRICING_RULES {
        int id PK
        int venue_id FK
        string rule_name
        time local_start_time
        time local_end_time
        numeric price_per_hour
    }
    RECURRING_SERIES {
        int id PK
        int member_id FK
        int court_id FK
        string recurrence_rule
        timestamp local_start
        date series_end_date
        string status
    }
    REFUND_DECISIONS {
        int id PK
        int reservation_id FK UK
        numeric eligible_amount
        string decision
        timestamptz decided_at
    }
```

**Current vs proposed:** `members`, `venues`, `courts`, and
`reservations` exist in the current schema. `PRICING_RULES`,
`RECURRING_SERIES`, and `REFUND_DECISIONS` are proposed and not yet
implemented. The diagram is a target design, not a claim about the
current database.

### Data rules

-   Require `start_time < end_time`.
-   Require timezone-aware API timestamps; normalize stored reservation
    timestamps to UTC.
-   Store each venue's IANA timezone (for example, `Asia/Kolkata`);
    schedules and pricing rules are interpreted in venue-local time.
-   Use half-open intervals `[start_time, end_time)`, so 10:00--11:00
    and 11:00--12:00 do not overlap.
-   Store the calculated price snapshot on each reservation.
-   Use PostgreSQL `NUMERIC` and Python `Decimal` for money.
-   Enforce foreign keys, unique constraints, and checks in PostgreSQL
    as well as validating in the service.

## 5. Reservation lifecycle

``` mermaid
stateDiagram-v2
    [*] --> HELD: Create hold
    HELD --> CONFIRMED: Confirm before expiry
    HELD --> EXPIRED: Expiry reached
    CONFIRMED --> CANCELLED: Cancel booking
    EXPIRED --> [*]
    CANCELLED --> [*]
    CONFIRMED --> [*]: Booking completed
```

-   `HELD`: temporary reservation with server-set expiry.
-   `CONFIRMED`: confirmed booking.
-   `EXPIRED`: hold was not confirmed before its deadline.
-   `CANCELLED`: confirmed booking was cancelled.

Allowed transitions are `HELD → CONFIRMED`, `HELD → EXPIRED`, and
`CONFIRMED → CANCELLED`. Other transitions are rejected. A hold is
eligible for confirmation only when the database time is strictly
earlier than `hold_expires_at`. At or after expiry, confirmation fails
and the hold is treated as expired.

## 6. Hold creation sequence

``` mermaid
sequenceDiagram
    actor Member
    participant API as FastAPI
    participant S as Booking Service
    participant DB as PostgreSQL

    Member->>API: POST /holds
    API->>S: create_hold(...)
    S->>DB: Read IDs needed for locking (no row lock)
    S->>DB: Begin transaction
    S->>DB: Lock member, then court
    S->>DB: Expire stale holds for court
    S->>DB: Lock/recheck relevant reservation rows
    S->>DB: Check member limit and overlapping reservations
    alt Conflict or limit reached
        S->>DB: Roll back
        S-->>API: Domain error
        API-->>Member: 409 Conflict
    else Valid and available
        S->>DB: Insert HELD reservation and expiry
        S->>DB: Commit
        S-->>API: Hold details
        API-->>Member: 201 Created
    end
```

Hold rules: 1. Server sets `hold_expires_at = database_now + 5 minutes`;
the client cannot choose expiry. 2. Validate timezone-aware timestamps
and `start_time < end_time`. 3. Confirm member and court exist. 4. Count
each unexpired `HELD` reservation and each future `CONFIRMED`
reservation toward the three-booking cap. 5. Lock the member before the
court, then process reservation rows. 6. Expire stale holds for the
target court within the same transaction before checking conflicts. 7.
Insert the hold in the same transaction and return its stable ID and
expiry.

## 7. Confirmation, expiry, and idempotency

``` mermaid
sequenceDiagram
    actor Member
    participant API as FastAPI
    participant S as Booking Service
    participant DB as PostgreSQL

    Member->>API: POST /holds/{id}/confirm
    API->>S: confirm_hold(id)
    S->>DB: Read member_id/court_id without locking
    S->>DB: Lock member, then court, then reservation
    S->>DB: Re-read current status and expiry
    alt Status is CONFIRMED
        S-->>API: Return existing confirmed reservation
        API-->>Member: 200 OK, same reservation ID
    else Status is HELD and database time < expiry
        S->>DB: Update status to CONFIRMED
        S->>DB: Commit
        S-->>API: Confirmed reservation
        API-->>Member: 200 OK
    else Expired, cancelled, or unknown
        S->>DB: Mark stale HELD row EXPIRED when applicable
        S->>DB: Commit expiry update, or roll back invalid operation
        API-->>Member: 404 or 409
    end
```

**Idempotency decision:** `POST /holds/{id}/confirm` uses the
hold/reservation ID as the stable operation identity. If a retry finds
the reservation already `CONFIRMED`, it returns that same reservation
and does not insert another row. An expired or cancelled hold cannot be
confirmed.

**Confirm-versus-expiry winner:** both paths coordinate through the same
member → court → reservation lock order and re-read the row after
acquiring locks. The database clock decides eligibility: confirmation
can succeed only if `clock_timestamp() < hold_expires_at` at the guarded
state transition. At or after the expiry instant, expiry wins. If
confirmation commits while the hold is still unexpired, a later expiry
attempt sees `CONFIRMED` and must not change it to `EXPIRED`.

## 8. Double-booking constraint and exact lock order

An application-only availability query is not sufficient. Two requests
can both see an empty slot before either inserts.

### Exact database exclusion constraint

Enable `btree_gist` and add the following PostgreSQL constraint in an
Alembic migration:

``` sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE reservations
ADD CONSTRAINT reservations_no_active_overlap
EXCLUDE USING gist (
    court_id WITH =,
    tstzrange(start_time, end_time, '[)') WITH &&
)
WHERE (status IN ('HELD', 'CONFIRMED'));
```

This forbids overlapping `HELD` or `CONFIRMED` intervals for the same
court. Adjacent half-open intervals are allowed. The migration must
verify that the existing `status` column and values match the predicate
before applying the constraint.

**Expiry limitation:** do not put `now()` or `clock_timestamp()` in the
partial exclusion predicate. Index predicates must be based on immutable
expressions; time passing does not automatically remove an old `HELD`
row from the index. Before checking/inserting a hold for a court, the
transaction locks that court and changes its stale `HELD` rows to
`EXPIRED`. The exclusion constraint remains the final guard against
overlapping active rows.

### Exact lock order

All operations that mutate a reservation follow this order:

1.  **Member row:**
    `SELECT ... FROM members WHERE id = :member_id FOR UPDATE`.
2.  **Court row:**
    `SELECT ... FROM courts WHERE id = :court_id FOR UPDATE`.
3.  **Reservation row(s):** lock the target reservation by ID; if
    locking several reservation rows, lock them in ascending reservation
    ID order.
4.  Re-read the relevant state, check expiry/limit/availability, then
    write and commit.

For confirmation/cancellation, first perform a non-locking lookup of the
reservation's `member_id` and `court_id` to learn which parent rows to
lock; then acquire locks in the order above and re-read the reservation
under lock. The initial lookup is not trusted for the final decision.

For hold creation, lock the known member, then court, then expire stale
holds and check/insert. The member lock serializes two simultaneous hold
requests from the same member, so the second request sees the first
committed hold when counting the limit. The court lock serializes
same-court creation/expiry checks. The exclusion constraint protects
against any missed application-level race.

Expiry cleanup must not lock reservation rows before the court row. It
operates only after the relevant court lock has been acquired and
locks/updates reservations in ascending ID order.

**Failure behavior:** a PostgreSQL exclusion violation or booking-limit
violation is rolled back and mapped to `409 Conflict`. No conflicting
reservations may both commit.

## 9. Transaction and failure handling

``` mermaid
flowchart TD
    Begin["Begin transaction"]
    Lock["Acquire locks in defined order"]
    Validate["Re-read state and validate rules"]
    Conflict{"Conflict / invalid state?"}
    Write["Write all database changes"]
    Commit["Commit"]
    Rollback["Rollback"]
    Success["Return success"]
    Error["Return mapped error"]

    Begin --> Lock --> Validate --> Conflict
    Conflict -- Yes --> Rollback --> Error
    Conflict -- No --> Write --> Commit --> Success
```

-   Hold creation: one transaction.
-   Confirmation: lock/read, expiry check, state transition, and commit
    in one transaction.
-   Cancellation/refund eligibility decision: reservation state change
    and internal decision record in one transaction.
-   Recurring series: create only the occurrences that fit the member
    cap, with each occurrence committed atomically alongside its
    reservation. A failed occurrence does not leave a half-written
    reservation for that occurrence; other valid occurrences can remain.
-   No external payment-provider call is made by this service.

Failure acceptance criteria: no reservation remains after failed
creation; confirmation retry does not duplicate a booking; a failed
transaction leaves no partial writes from that transaction; database
integrity violations are mapped to stable API errors.

## 10. API contracts

### `POST /holds`

Creates a temporary hold.

``` json
{
  "court_id": 12,
  "start_time": "2026-11-05T10:00:00Z",
  "end_time": "2026-11-05T11:00:00Z"
}
```

Success: `201 Created`, including hold ID, status, times, and expiry.
Errors: `422` invalid input; `404` unknown entity; `409` overlap or
booking limit reached.

The current implementation accepts `member_id` in the request. The
production design should derive member identity from authentication;
until authentication is designed, this is a documented development
limitation.

### `POST /holds/{id}/confirm`

Success: `200 OK`, stable reservation ID and confirmed details. `404`
unknown hold; `409` expired/invalid hold. A successful retry returns the
same reservation.

### `POST /bookings/{id}/cancel`

Success: cancellation status and internal refund eligibility decision.
`404` unknown booking; `409` invalid state. Cancellation **at least 24
hours before** the booking start qualifies for a full refund; less than
24 hours before start qualifies for no refund. Exactly 24 hours is
included in the full-refund case. This endpoint records eligibility
only; it does not move money.

### `GET /courts/{id}/availability`

Returns available intervals for a court/date range, excluding confirmed
bookings and unexpired holds. Cancelled bookings and expired holds do
not block availability. Availability is a snapshot; a later hold may
still fail, with PostgreSQL as final authority.

## 11. Chosen business rules

### 11.1 Maximum three active future bookings and recurring series

**Decision:** each occurrence of a recurring series is an individual
booking and counts separately toward the maximum of three active future
bookings. An unexpired `HELD` reservation also counts, to prevent
concurrent holds from bypassing the cap.

A recurring-series definition may describe more than three weeks, but
the system materializes/reserves only as many future occurrences as the
member's remaining capacity allows, up to three active future bookings
total across all individual and recurring bookings. As a counted booking
stops being future/active, the scheduler may attempt to materialize the
next occurrence. Each attempted occurrence must re-check the member cap,
court availability, and pricing in a transaction.

A conflicting occurrence is skipped and recorded as not created; it does
not roll back already-created valid occurrences and does not block later
occurrences. The member is not promised a court for an occurrence until
that occurrence has a reservation row. This default would change only if
the product explicitly requires all-or-nothing series reservations or
requires every week to be guaranteed at series-creation time; either
change must be reconciled with the three-booking cap.

### 11.2 Cancellation boundary

**Decision from the brief:** full refund eligibility applies when
`booking_start - cancellation_time >= 24 hours`. No refund applies when
the remaining time is `< 24 hours`. Exactly 24 hours qualifies for a
full refund. The service records the eligibility/amount only; external
refund execution is out of scope.

### 11.3 Daylight saving and recurring local times

**Decision:** venue timezone is an IANA timezone. A weekly series is
defined by venue-local wall-clock day and time, not by adding exactly
168 hours to a UTC timestamp. Each occurrence is converted to UTC for
storage.

For a venue that observes daylight saving: - If a scheduled local time
does not exist during a spring-forward transition, move that occurrence
to the first valid local time after the DST gap. - If a local time
occurs twice during a fall-back transition, choose the earlier
occurrence (the first instant). - Persist each materialized occurrence's
UTC start/end; do not silently rewrite already-created reservations if
timezone rules later change.

This default would change only if venue operations require rejecting
invalid local times or selecting the later fall-back occurrence.

### 11.4 Peak/off-peak pricing across a boundary

**Decision:** split a booking interval at each configured peak/off-peak
boundary in the venue's local schedule. Calculate each segment as
`segment_duration_in_hours × applicable_price_per_hour`, then sum the
segments and round the final total to two decimal places using a
documented decimal rounding mode (`ROUND_HALF_UP`). The stored
reservation price is a snapshot.

Pricing rules for a venue must be non-overlapping and must cover the
full bookable schedule; otherwise the booking is rejected as
unpriceable. A booking that crosses a boundary pays the applicable rate
for each segment, not a single rate for the whole booking. This default
would change only if the club explicitly adopts a start-time rate or
whole-booking peak rate.

### 11.5 Hold expiry and active overlap

**Decision:** a hold lasts five minutes. At or after the expiry
timestamp it cannot be confirmed. Before creating another hold for the
same court, stale holds are marked `EXPIRED` under the court lock in the
same transaction. The database exclusion constraint applies to stored
`HELD` and `CONFIRMED` states; it is not time-dependent.

## 12. Staff utilization report

**Decision:** weekly utilization means confirmed reserved court-hours
divided by available operating court-hours, multiplied by 100. Cancelled
bookings and expired holds are excluded. Court closures and configured
operating hours are excluded from the denominator. Actual physical usage
is out of scope because the system has no check-in/sensor requirement in
the brief.

## 13. Testing strategy

  -----------------------------------------------------------------------
  Area                                Acceptance criterion
  ----------------------------------- -----------------------------------
  Time validation                     Reject `start_time >= end_time` and
                                      naive timestamps

  Expiry                              Confirmation fails at or after
                                      expiry

  Availability                        Expired/cancelled reservations do
                                      not block

  Overlap                             Exclusion constraint prevents
                                      overlapping active reservations
                                      from both committing

  Adjacent intervals                  10:00--11:00 and 11:00--12:00 both
                                      allowed

  Concurrent holds                    Same-court conflicting requests: at
                                      most one succeeds

  Concurrent member holds             Two simultaneous requests for one
                                      member cannot exceed the
                                      three-booking cap

  Confirmation retry                  Same reservation ID; no duplicate

  Confirm vs expiry                   Only one valid terminal transition;
                                      expiry wins at/after deadline

  Cancellation                        Test just before, exactly at, and
                                      just after 24 hours

  Recurrence cap                      No more than three active future
                                      occurrences/bookings per member

  Recurrence conflict                 Failed occurrence creates no
                                      partial reservation; later
                                      occurrences are evaluated
                                      independently

  DST                                 Spring gap and fall overlap follow
                                      the documented policy

  Pricing                             A boundary-crossing booking is
                                      priced by segment

  Rollback                            Failed transaction leaves no
                                      partial writes from that
                                      transaction

  Error mapping                       Stable 404/409/422 responses
                                      without SQL details
  -----------------------------------------------------------------------

Run concurrency tests against PostgreSQL 15, not SQLite, because locking
and exclusion behavior are PostgreSQL-specific.

## 14. Alternatives considered and defaults that would change

-   **Application-only overlap check --- rejected:** simultaneous
    requests can both pass.
-   **Time-dependent exclusion predicate using `now()` --- rejected:**
    index predicates cannot depend on changing wall-clock time.
-   **Background cleanup as the only expiry mechanism --- rejected:**
    correctness cannot depend on cleanup timing.
-   **Counting only confirmed bookings toward the cap --- rejected:**
    concurrent unexpired holds could bypass the cap.
-   **All-or-nothing recurring series --- not selected:** it conflicts
    with the three-active-booking cap for long series. The chosen
    default materializes only occurrences that fit the cap; conflicting
    occurrences are skipped. Change only if product requirements
    explicitly prioritize all-or-nothing behavior.
-   **Adding 168 UTC hours for weekly recurrence --- rejected:** local
    wall-clock time may shift around DST.
-   **One rate for a whole booking crossing a peak boundary ---
    rejected:** chosen default prices each segment at its applicable
    rate.
-   **External payment provider integration --- out of scope:** no
    provider, webhook, or money-movement integration is designed or
    implemented.

## 15. Current implementation snapshot

This section separates the proposed design from implementation status.

### Currently implemented (based on the development session)

-   Python 3.11, FastAPI app, PostgreSQL 15 via Docker Compose.
-   SQLAlchemy models and Alembic migrations for `members`, `venues`,
    `courts`, and `reservations`.
-   `reservations.hold_expires_at` added and verified.
-   Initial `HoldCreate` / `HoldResponse` schemas and database session
    dependency.
-   Initial hold service and `POST /holds` route.
-   App starts, Swagger UI is available, and an initial request returned
    `201 Created`.

### Not yet fully implemented or verified

-   Exact exclusion constraint migration and PostgreSQL concurrency
    tests.
-   Final consistent locking protocol and member booking cap under
    concurrency.
-   `POST /holds/{id}/confirm` and retry idempotency.
-   `POST /bookings/{id}/cancel` and refund eligibility record.
-   `GET /courts/{id}/availability`.
-   Pricing rules and boundary-splitting calculation.
-   Recurring-series storage/materialization and DST behavior.
-   Staff utilization report.
-   Authentication and authorization.
-   Comprehensive rollback/retry/concurrency tests.

### Known implementation alignment items

-   Current request schema includes `member_id`; production identity
    must eventually come from authentication.
-   Current service uses a temporary zero-price placeholder; pricing is
    not implemented.
-   Current overlap query is only an initial application-level check,
    not a concurrency guarantee.
-   Current `ValueError` handling maps errors to `409`; final mapping
    should distinguish invalid input (`422`), missing entities (`404`),
    and conflicts (`409`).

## 16. Review exit criteria

Before implementing beyond `POST /holds`, the design review should
confirm: 1. The three-booking cap and rolling recurring-occurrence rule.
2. Exact exclusion constraint and migration prerequisites. 3. The member
→ court → reservation lock order and expiry race behavior. 4.
Confirmation retry contract and expiry boundary. 5. The exact 24-hour
cancellation rule. 6. DST and peak-boundary pricing defaults. 7.
Internal refund eligibility only, with payment-provider integration
excluded. 8. PostgreSQL integration tests for overlap, booking-cap
concurrency, expiry races, retry, and rollback.

**Implementation gate:** do not build endpoints beyond `POST /holds`
until this revised design is reviewed.
