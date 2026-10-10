# DECISIONS.md

1. **Indexes.**
We created unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` to strictly guarantee uniqueness and eliminate race conditions. For query performance, we created compound indexes `attendance_logs(date DESC, emp_code ASC)` for deterministic listing pagination, `attendance_logs(emp_code, punch_in DESC)` for O(log N) punch-out resolution, `employees(department, joined_on)` for department summary and trend queries, and `attendance_logs(date, late_minutes DESC)` for monthly and leaderboard aggregations. We considered an index on `(status, date)` but rejected it because `status` has very low cardinality (only 5 values), providing minimal selectivity compared to scanning the high-cardinality `date` index.

2. **Punch-in race.**
When two simultaneous punch-in requests arrive for the same employee and attendance date, both compute shift parameters in parallel and attempt atomic database insertion. MongoDB's WiredTiger engine serializes the unique compound index constraint on `(emp_code, date)`. Exactly one insert succeeds and returns HTTP 201. The concurrent insert fails with a `DuplicateKeyError`, which our handler catches to return HTTP 409, preventing duplicate records without any check-then-insert race window.

3. **Ties.**
We use MongoDB `$setWindowFields` with `$rank` (1-2-2-4 standard competition ranking). Filtering (`rank <= limit`) is executed strictly *after* window ranking. If multiple employees tie at or before the cutoff rank, all tied employees are retained, even if total rows exceed `limit`. Tied employees are sorted deterministically by `emp_code` ascending.

4. **Headcount.**
The department summary aggregation originates on the `employees` collection (matching `joined_on <= end_of_month`), performing a `$lookup` into `attendance_logs`. Because headcount groups and sums employee documents (`$sum: 1`), employees with zero attendance logs are accurately included in the headcount while contributing 0 to presence totals.

5. **One thing you would change for 100x data.**
At 100x data (~10M logs), we would maintain pre-aggregated daily and monthly department summary documents updated incrementally via MongoDB Change Streams or write-time atomic counters, enabling O(1) reads instead of runtime cross-collection aggregations.
