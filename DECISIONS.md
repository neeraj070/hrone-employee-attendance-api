# DECISIONS.md

## 1. Indexes

I created a unique index on employee codes to prevent duplicate employees. For attendance, a unique compound index on employee code and date prevents multiple daily records. Other indexes support department filtering, date-range queries, employee attendance history, and late-arrival analytics. I rejected a separate attendance index on employee code because the unique compound index already starts with that field.

## 2. Punch-in race condition

If two identical punch-in requests arrive together, both may pass the employee-existence check. MongoDB's unique employee-code-and-date index allows only one attendance record. The other insert raises `DuplicateKeyError`, which the API converts to HTTP 409 Conflict.

## 3. Leaderboard ties

The leaderboard uses MongoDB's `$rank`, so employees with equal late minutes receive the same rank. Filtering by rank rather than simply limiting the number of records can return more employees than the requested limit when a tie occurs at the cutoff. Results are then sorted by late minutes and employee code.

## 4. Department headcount

The department summary starts with employees and uses a lookup for attendance records. `$unwind` preserves employees without matching attendance logs, so they remain included in headcount.

## 5. Scaling to 100x data

I would benchmark with realistic data volumes, review index usage and write overhead, and optimize repeated aggregation lookups. If analytics became expensive, I would consider pre-aggregating frequently requested metrics.

