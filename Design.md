# Sports Court Booking Backend --- System Design Document

**Status:** Design draft for mentor review\
**Stack:** Python 3.11, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 15,
pytest, Ruff, mypy, Docker Compose

> **Status convention:** Proposed design decisions are not claims that
> the feature is already implemented. The current implementation
> snapshot is separated in Section 15.

## 1. Problem and goals

The club rents courts by time slot across multiple venues. Members need
to view availability, create a short-lived hold, confirm it, cancel
bookings under a refund policy, and create weekly recurring bookings.
Staff need weekly court-utilization reports.

The central correctness requirement is that two overlapping reservations
for the same court must never both become active, even when requests
arrive concurrently.

Goals: - Prevent double-booking at the database level. - Expire
unconfirmed holds after five minutes. - Make confirmation retries
idempotent. - Keep multi-step database operations atomic. - Enforce a
maximum of three active future bookings per member. - Apply
venue-specific peak/off-peak prices and the 24-hour cancellation/refund
policy. - Verify concurrency behavior against PostgreSQL 15.

Open/non-goals for the initial design: payment provider,
authentication/authorization details, venue hours/time zones, exact peak
periods, and maximum advance-booking window need review.

## 2. Technology and component responsibilities

-   **FastAPI:** HTTP endpoints and mapping errors to HTTP responses.
-   **Pydantic:** request validation and response schemas.
-   **Service layer:** booking rules and workflow coordination, separate
    from HTTP handlers.
-   **SQLAlchemy:** ORM and database sessions/transactions.
-   **PostgreSQL 15:** source of truth and final integrity/concurrency
    enforcement.
-   **Alembic:** versioned database schema changes.
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

Routes should validate/translate HTTP data but should not own the core
business rules. A service-level availability check improves error
messages, but the database must still reject conflicting writes.

## 4. Proposed database model

``` mermaid
erDiagram
    MEMBERS ||--o{ RESERVATIONS : makes
    VENUES ||--o{ COURTS : contains
    COURTS ||--o{ RESERVATIONS : receives
    VENUES ||--o{ PRICING_RULES : configures
    RECURRING_SERIES ||--o{ RESERVATIONS : generates
    RESERVATIONS ||--o{ REFUND_RECORDS : may_have

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
        string local_start_time
        string local_end_time
        numeric price_per_hour
    }
    RECURRING_SERIES {
        int id PK
        int member_id FK
        int court_id FK
        string recurrence_rule
        timestamptz series_start
        timestamptz series_end
        string status
    }
    REFUND_RECORDS {
        int id PK
        int reservation_id FK
        numeric amount
        string status
        string idempotency_key UK
    }
```

**Current vs proposed:** `members`, `venues`, `courts`, and
`reservations` exist in the current schema. `PRICING_RULES`,
`RECURRING_SERIES`, and `REFUND_RECORDS` are proposed entities, not yet
implemented.

### Data rules

-   Require `start_time < end_time`.
-   Require timezone-aware timestamps and normalize stored timestamps to
    UTC.
-   Use venue-local time for schedules and pricing rules.
-   Use half-open intervals `[start_time, end_time)`, so 10:00--11:00
    and 11:00--12:00 do not overlap.
-   Store the calculated booking price as a snapshot on the confirmed
    reservation.
-   Use PostgreSQL `NUMERIC` and Python `Decimal` for money.
-   Enforce foreign keys and appropriate unique/check constraints.

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

-   `HELD`: temporary reservation with expiry timestamp.
-   `CONFIRMED`: successfully confirmed booking.
-   `EXPIRED`: hold was not confirmed before its deadline.
-   `CANCELLED`: confirmed booking was cancelled.

Allowed transitions: `HELD → CONFIRMED`, `HELD → EXPIRED`, and
`CONFIRMED → CANCELLED`. Other transitions are rejected. Availability
and confirmation must compare expiry with current time even if a cleanup
process has not updated the stored status.

## 6. Hold creation sequence

``` mermaid
sequenceDiagram
    actor Member
    participant API as FastAPI
    participant S as Booking Service
    participant DB as PostgreSQL

    Member->>API: POST /holds
    API->>API: Validate request and times
    API->>S: create_hold(...)
    S->>DB: Begin transaction / coordinate on court
    S->>DB: Check member, court, booking limit
    S->>DB: Expire stale holds for this court if needed
    S->>DB: Check overlapping active reservations
    alt Conflict or booking limit reached
        S->>DB: Roll back
        S-->>API: Domain error
        API-->>Member: 409 Conflict
    else Available
        S->>DB: Insert HELD reservation and expiry
        S->>DB: Commit
        S-->>API: Hold details
        API-->>Member: 201 Created
    end
```

Hold rules: 1. Server sets `hold_expires_at = now + 5 minutes`; client
does not choose expiry. 2. Validate timezone-aware timestamps and
`start_time < end_time`. 3. Confirm member and court exist. 4.
Provisionally count unexpired holds toward the three-booking limit,
subject to client confirmation. 5. Check active conflicts using the same
interval semantics as the database. 6. Insert the hold in a transaction
and return its stable ID and expiry.

## 7. Confirmation and idempotency

``` mermaid
sequenceDiagram
    actor Member
    participant API as FastAPI
    participant S as Booking Service
    participant DB as PostgreSQL

    Member->>API: POST /holds/{id}/confirm
    API->>S: confirm_hold(id)
    S->>DB: Begin transaction and lock hold row
    S->>DB: Read status and expiry
    alt Already CONFIRMED
        S-->>API: Return existing result
        API-->>Member: 200 OK, same reservation ID
    else HELD and unexpired
        S->>DB: Update state to CONFIRMED
        S->>DB: Commit
        S-->>API: Confirmed reservation
        API-->>Member: 200 OK
    else Expired, cancelled, or unknown
        S-->>API: Domain error
        API-->>Member: 404 or 409
    end
```

A client timeout does not prove that the database operation failed. A
retry after a successful commit must return the same reservation result,
not create a new booking. Concurrent confirmations must coordinate on
the same row. Expired or cancelled holds cannot be confirmed.

## 8. Double-booking and concurrency design

An application-only `SELECT` availability check is insufficient: two
transactions can both observe an empty slot before either inserts.

Proposed protection: 1. Use PostgreSQL as the source of truth. 2. Use
half-open time intervals. 3. Evaluate a PostgreSQL GiST exclusion
constraint for overlapping active intervals on the same court;
`btree_gist` may be needed for court-ID equality plus range overlap. 4.
Coordinate hold creation on the relevant court row (for example,
`SELECT ... FOR UPDATE`) so requests for the same court serialize while
stale holds are processed. 5. Mark stale holds `EXPIRED` inside the
transaction before inserting a new hold. 6. Check expiry against current
time in availability and confirmation logic. 7. Translate database
conflict violations to `409 Conflict`. 8. Prove the behavior with
simultaneous PostgreSQL integration tests.

**Important PostgreSQL detail:** a partial exclusion predicate cannot
simply depend on changing `now()` to make a hold stop blocking at
expiry. The design must combine durable constraint protection with
transactional stale-hold handling and court-level coordination. The
exact constraint and locking design must be verified against PostgreSQL
15 before claiming concurrency safety.

## 9. Transactions and failure handling

``` mermaid
flowchart TD
    Begin["Begin transaction"]
    Validate["Validate entities and rules"]
    Conflict{"Conflict / invalid state?"}
    Write["Write state changes"]
    Commit["Commit"]
    Rollback["Rollback"]
    Success["Return success"]
    Error["Return mapped error"]

    Begin --> Validate --> Conflict
    Conflict -- Yes --> Rollback --> Error
    Conflict -- No --> Write --> Commit --> Success
```

-   Hold creation: one transaction.
-   Confirmation: lock/read, validate, state transition, and commit in
    one transaction.
-   Cancellation and internal refund-record creation: one database
    transaction.
-   All-or-nothing recurring series: all occurrences in one transaction;
    any conflict rolls the entire series back.
-   Do not hold a database transaction open while waiting for an
    external payment provider.
-   External payment/refund calls require persisted requests,
    idempotency keys, retry/reconciliation, because database rollback
    cannot reverse an external side effect.

Failure acceptance criteria: no reservation remains after failed
creation; no partial recurring series; failed confirmation leaves a
valid prior state; timeout retries do not duplicate a booking; internal
errors do not leak SQL details to clients.

## 10. API contracts

### `POST /holds`

Creates a temporary hold. Proposed request:

``` json
{
  "court_id": 12,
  "start_time": "2026-11-05T10:00:00Z",
  "end_time": "2026-11-05T11:00:00Z"
}
```

The authenticated member should ideally be derived from authentication
rather than trusting a client-supplied `member_id`; authentication is
still an open design decision.

Success: `201 Created`, including hold ID, status, times, and expiry.
Errors: `422` invalid input; `404` unknown entity; `409` overlap or
booking limit reached.

### `POST /holds/{id}/confirm`

Success: `200 OK`, stable reservation ID and confirmed details. `404`
unknown hold; `409` expired/invalid hold. Successful retries return the
same result.

### `POST /bookings/{id}/cancel`

Success: cancellation status and refund eligibility/amount/status. `404`
unknown booking; `409` invalid state. Cancellation at least 24 hours
before start qualifies for a full refund; less than 24 hours qualifies
for no refund. Exact boundary and external refund processing need
agreement.

### `GET /courts/{id}/availability`

Returns available intervals for a court/date range, excluding confirmed
bookings and unexpired holds. Cancelled bookings and expired holds do
not block availability. Availability is a snapshot; a later hold can
still fail, with the database as final authority.

## 11. Pricing, cancellation, recurrence, limits, reporting

### Pricing

-   Configure rules per venue and evaluate them using venue-local time.
-   Decide how a booking crossing a peak/off-peak boundary is priced.
-   Store price snapshot on confirmation; use `Decimal`/`NUMERIC`.
-   Tax, fees, rounding, and rule precedence need review.

### Cancellation/refunds

-   Lock/read the reservation and reject invalid state transitions.
-   Apply the 24-hour rule based on time until start.
-   Record refund eligibility separately from external refund-processing
    status.
-   Make repeated cancellation calls safe from duplicate refunds.

### Weekly recurring bookings

-   Store a recurrence definition and individual occurrences.
-   Provisional policy is all-or-nothing: any conflict rejects the whole
    series.
-   Define recurrence end, daylight-saving behavior, venue-local time
    semantics, and booking horizon.

### Active booking limit

-   Provisional maximum is three active future bookings per member.
-   Provisionally count unexpired holds.
-   Coordinate concurrent requests for the same member so the limit
    cannot be exceeded.
-   Confirm how cancellations, expired holds, and recurring series
    count.

### Staff utilization

Agree whether utilization means reserved hours or actual usage, whether
cancellations are excluded, and how operating hours/closures affect the
denominator. A possible definition is eligible occupied court hours
divided by available operating court hours, multiplied by 100.

## 12. Testing strategy

  Area                      Acceptance criterion
  ------------------------- -------------------------------------------------------
  Time validation           Reject `start_time >= end_time`
  Time zones                Reject naive timestamps
  Expiry                    Cannot confirm at/after expiry
  Availability              Expired/cancelled holds do not block
  Overlap                   Conflicting intervals cannot both commit
  Adjacent intervals        10--11 and 11--12 both allowed
  Concurrent holds          At most one conflicting request succeeds
  Confirmation retry        Same reservation ID; no duplicate
  Concurrent confirmation   No duplicate state transition
  Booking limit             Concurrent requests cannot exceed limit
  Cancellation              Test just before, exactly at, and just after 24 hours
  Recurrence                Conflict causes no partial series
  Rollback                  Failed multi-step operation leaves no partial state
  Pricing                   Correct local-time rule and price snapshot
  Error mapping             Stable error codes and response format

Run concurrency tests against PostgreSQL 15, not SQLite, because
database locking and exclusion behavior are PostgreSQL-specific.

## 13. Alternatives considered

-   **Application-only overlap check --- rejected:** simultaneous
    requests can both pass.
-   **Background cleanup as the only expiry mechanism --- rejected:**
    correctness must not depend on cleanup timing.
-   **Independent commits for recurring occurrences --- rejected:** can
    leave a partial series.
-   **SQLite as proof of PostgreSQL concurrency --- rejected:** it does
    not reproduce PostgreSQL locking/exclusion behavior.
-   **External payment call inside DB transaction --- rejected:**
    database rollback cannot undo the external side effect and long
    calls hold resources.

## 14. Questions for design review

1.  Is a five-minute hold acceptable?
2.  Do holds count toward the three-booking limit?
3.  Are recurring series all-or-nothing?
4.  Which payment provider, if any?
5.  Are refunds immediate or asynchronous?
6.  How are peak/off-peak periods configured?
7.  What are each venue's time zone and operating hours?
8.  Does staff utilization mean reserved or actual usage?
9.  How far ahead may members book?
10. How should pricing work across a peak/off-peak boundary?
11. What authentication and staff authorization are required?
12. How should daylight-saving transitions affect recurring bookings?

## 15. Current implementation snapshot

This section is separate from the proposed design so unfinished work is
not presented as complete.

### Currently implemented (based on the current development session)

-   Python 3.11, FastAPI app, PostgreSQL 15 via Docker Compose.
-   SQLAlchemy models and Alembic migrations for `members`, `venues`,
    `courts`, and `reservations`.
-   `reservations.hold_expires_at` added and verified.
-   Initial `HoldCreate` / `HoldResponse` schemas and database session
    dependency.
-   Initial hold service and `POST /holds` route.
-   Application starts; Swagger UI is available; a successful request
    returned `201 Created`.

### Not yet fully implemented or verified

-   Database exclusion constraint and complete concurrency-safe
    double-booking protection.
-   `POST /holds/{id}/confirm` and idempotent retry behavior.
-   `POST /bookings/{id}/cancel` and refund workflow.
-   `GET /courts/{id}/availability`.
-   Pricing rules and peak/off-peak calculation.
-   Recurring-series logic and all-or-nothing transaction.
-   Three-booking limit under concurrency.
-   Staff utilization report.
-   Comprehensive concurrency/retry/rollback tests.
-   Authentication and authorization.

### Alignment items

-   The current request schema includes `member_id`; the proposed public
    contract should derive it from authentication once authentication is
    designed.
-   The current service uses a temporary zero-price placeholder; pricing
    is not implemented.
-   The current overlap query is only an initial application-level
    check, not a concurrency guarantee.
-   Current `ValueError` handling maps errors to `409`; final mapping
    should distinguish invalid input (`422`), missing entities (`404`),
    and conflicts (`409`).

## 16. Review exit criteria

Before calling the design approved, agree on: - Database constraint and
court-locking approach. - Hold expiry and transaction ordering. -
Confirmation idempotency contract. - Concurrent booking-limit
enforcement. - Recurring-series atomicity. - Cancellation/refund
boundary and external-payment workflow. - Venue-local pricing/time-zone
rules. - PostgreSQL acceptance tests for concurrency, retry, and
rollback.
