
# Sports Court Booking Backend — Design Document

## 1. Overview

The Sports Court Booking Backend is a REST API system
for managing sports court reservations across multiple venues.

The system will allow members to:
- Check court availability.
- Temporarily reserve a court using a hold.
- Confirm a hold to create a booking.
- Cancel bookings according to the refund policy.
- Create weekly recurring bookings.

The system will also support venue-specific pricing,
active booking limits, and weekly court utilization reports
for staff.

## 2. Technology Stack

- Python 3.11
- FastAPI
- PostgreSQL 15
- SQLAlchemy 2
- Alembic
- pytest
- Ruff
- mypy
- Docker Compose

## 3. Core Requirements

1. Prevent overlapping active reservations for the same court.
2. Allow members to reserve a slot temporarily before confirming.
3. Expire unconfirmed holds automatically from the booking logic.
4. Make confirmation retries safe and prevent duplicate bookings.
5. Support booking cancellation and the 24-hour refund policy.
6. Support weekly recurring bookings.
7. Apply peak and off-peak pricing per venue.
8. Limit members to three active future bookings.
9. Provide weekly court utilization reports for staff.
10. Ensure failed multi-step database operations leave no partial state.

## 4. Mandatory API Endpoints

- POST /holds
- POST /holds/{id}/confirm
- POST /bookings/{id}/cancel
- GET /courts/{id}/availability

## 5. Initial Design Principles

- Enforce booking conflict rules at the database level.
- Use database transactions for multi-step operations.
- Make confirmation idempotent so retries do not create duplicates.
- Treat expired holds as unavailable for confirmation and
  available for new reservations, when no other conflict exists.
- Store timestamps consistently and handle venue time zones explicitly.
- Test concurrency guarantees against PostgreSQL.
- Keep business rules testable and separate from HTTP handlers.

## 6. Status

Initial requirements analysis and design planning are in progress.

Detailed assumptions, database constraints, API contracts,
failure handling, and rejected alternatives will be documented
before implementation.


## 7. Assumptions

The following assumptions will be used for the initial design.
They are provisional and may change after mentor or client review.

1. Hold Duration
   - A hold will expire after 5 minutes.
   - The expiry time will be calculated by the server.

2. Time Handling
   - Timestamps will be stored consistently in UTC.
   - Venue-local time zones will be used for displaying schedules
     and applying venue-specific pricing rules.
   - Booking intervals will use the half-open convention [start, end),
     so a booking ending at 11:00 does not conflict with one
     starting at 11:00.

3. Booking Conflicts
   - Confirmed bookings and unexpired holds block overlapping slots.
   - Cancelled bookings and expired holds do not block availability.
   - Database-level protection will prevent conflicting reservations
     even when requests arrive concurrently.

4. Confirmation
   - A hold can be confirmed only if it is valid and unexpired.
   - Retrying a successful confirmation must not create another booking.
   - The original confirmation result should be recoverable on retry.

5. Cancellation and Refunds
   - Cancellation at least 24 hours before the booking starts
     qualifies for a full refund.
   - Cancellation less than 24 hours before the start time
     qualifies for no refund.
   - Refund processing status will be recorded separately from
     booking cancellation status if an external payment provider is used.

6. Active Booking Limit
   - A member may have at most 3 active future bookings.
   - Unexpired holds will provisionally count toward this limit.
   - The exact counting rules will be reviewed with the client.

7. Recurring Bookings
   - A weekly series will provisionally be treated as one
     all-or-nothing operation.
   - If any occurrence conflicts with an existing reservation,
     the entire series will be rejected without partial database state.

8. Pricing
   - Pricing rules will be configured per venue.
   - The calculated price will be stored with each confirmed booking
     so later pricing changes do not alter existing booking prices.

## 8. Open Questions for the Client

1. Is a 5-minute hold duration acceptable?
2. Should temporary holds count toward the 3-booking limit?
3. Should recurring bookings be all-or-nothing, or should
   available dates be booked even if some dates conflict?
4. Which payment provider will be used, if any?
5. Should a refund be issued immediately or processed asynchronously?
6. How should peak and off-peak periods be configured?
7. What are each venue's operating hours and time zone?
8. Should staff utilization include cancelled bookings or only
   actual confirmed court usage?
9. How far into the future can members book?
10. How should the system handle a requirement change to booking
    limits, hold duration, or pricing rules?

## 9. Initial Design Decisions

1. PostgreSQL will be the source of truth for booking state.
2. Booking conflict prevention will be enforced by the database,
   not only by application-level availability checks.
3. Multi-step database operations will use transactions.
4. Confirmation retries will be handled idempotently.
5. Automated tests will cover concurrent requests, retries,
   rollback behavior, expiry, cancellation, and recurring bookings.
6. PostgreSQL integration tests will be used to verify database
   concurrency behavior.

## 10. Alternatives Considered

1. Application-level availability checks only
   - Rejected because concurrent requests can pass the same check
     before either request saves its reservation.

2. Relying only on a background cleanup job for expired holds
   - Rejected because an expired hold must stop blocking availability
     even if cleanup has not run yet.

3. Creating recurring bookings through independent commits
   - Rejected because a failure midway could leave a partial series.

4. Using SQLite as proof of PostgreSQL concurrency guarantees
   - Rejected because SQLite does not reproduce PostgreSQL's
     locking and exclusion-constraint behavior.

## 11. Design Review Status

This is an initial design draft.
The assumptions and proposed decisions require review before
implementation. Database constraints, transaction boundaries,
API schemas, and failure-handling details will be finalized
during the design review.


## 12. Database Model

The initial database design will include the following entities:

1. Members
   - Stores member identity and account information.

2. Venues
   - Stores venue details, configuration, and time zone.

3. Courts
   - Belongs to a venue.
   - Stores court type and operational configuration.

4. Reservations
   - Stores member, court, start time, end time, status,
     hold expiry, and price snapshot.
   - A reservation transitions from HELD to CONFIRMED,
     or to EXPIRED or CANCELLED as appropriate.
   - The reservation ID will remain stable during confirmation
     so a retry can identify the same operation.

5. Pricing Rules
   - Stores venue-specific peak and off-peak pricing periods.

6. Recurring Series
   - Stores the weekly recurrence definition and links
     the individual reservations to the series.

7. Refund Records
   - Stores refund amount, status, and an idempotency reference
     if payment-provider integration is required.

## 13. Double-Booking Prevention

PostgreSQL will be the source of truth for reservation conflicts.

The design will use a database exclusion constraint to reject
overlapping active reservations for the same court.

The reservation time interval will use half-open semantics:
[start_time, end_time).

A PostgreSQL range representation and GiST exclusion constraint
will be evaluated for enforcing this rule. The btree_gist
extension may be required to combine court identity with
range overlap in the constraint.

Reservation writes will use database transactions and
coordinate on the relevant court row.

Before creating a new hold, the transaction will mark expired
holds for that court as EXPIRED. Availability and confirmation
logic will also check hold expiry against the current server
time.

A background cleanup task may expire old holds, but correctness
will not depend on that task running on time.

Concurrency integration tests will run against PostgreSQL 15
and verify that conflicting requests cannot both succeed.

## 14. Confirmation Idempotency

Confirmation will use the existing reservation/hold identifier.

The hold state transition and confirmation result will be
persisted atomically.

A retry after a successful confirmation will return the existing
booking result rather than creating a second booking.

Expired, cancelled, or otherwise invalid holds will not be
confirmed successfully.

Tests will cover concurrent confirmation attempts and retries
after simulated client timeouts.

## 15. Transaction Safety

Multi-step database operations will use transactions.

If a recurring series is configured as all-or-nothing and
one occurrence cannot be reserved, all database changes from
that series operation will be rolled back.

Tests will verify that failed operations leave no partial
reservations or inconsistent database state.

External payment operations cannot be rolled back by a database
transaction alone and will require separate failure handling.


## 16. Reservation Lifecycle

The reservation lifecycle will use explicit states:

- HELD: A temporary reservation with an expiry timestamp.
- CONFIRMED: The reservation has been successfully confirmed.
- EXPIRED: A hold was not confirmed before its deadline.
- CANCELLED: A confirmed booking was cancelled.

Allowed transitions:

- HELD -> CONFIRMED
- HELD -> EXPIRED
- CONFIRMED -> CANCELLED

Invalid transitions will be rejected.

A hold cannot be confirmed at or after its expiry timestamp.
The server and database will use consistent time semantics.

## 17. API Contracts

### POST /holds

Creates a temporary reservation.

Input:
- court_id
- start_time
- end_time

Success:
- Returns the hold/reservation ID, status, and expires_at.

Failures:
- Invalid time range: 422
- Conflicting reservation: 409
- Booking limit reached: 409

### POST /holds/{id}/confirm

Confirms an existing hold.

Success:
- Returns the stable reservation ID and confirmed booking details.

Failures:
- Unknown hold: 404
- Expired or invalid hold: 409

Retry behavior:
- A repeated successful confirmation returns the existing result
  and does not create a second booking.

### POST /bookings/{id}/cancel

Cancels a confirmed booking.

Success:
- Returns cancellation status and the applicable refund amount
  and processing status.

Failures:
- Unknown booking: 404
- Invalid cancellation state: 409

### GET /courts/{id}/availability

Returns available intervals for a court over a requested date range.

The response will exclude confirmed bookings and valid,
unexpired holds.

The endpoint will validate the requested range and use the
same conflict rules as reservation creation.

## 18. Additional API Design

Additional endpoints may be introduced for:
- Member and venue management.
- Weekly recurring bookings.

- Pricing configuration.
- Staff utilization reports.
Authentication and authorization rules will be defined before
these endpoints are implemented.

## 19. Error Handling

API errors will use a consistent response structure with:
- A machine-readable error code.
- A human-readable message.
- Optional field-level validation details.

Database transactions will be rolled back when an operation
fails. Internal database errors will not expose sensitive
implementation details to API clients.
