# Employee Attendance & Analytics API (FastAPI + MongoDB)

HROne Software Engineer Trainee Engineering Assignment Implementation v2.0.

Provides employee identity management, race-safe punch-in/punch-out attendance recording, manual regularization audit tracking, and MongoDB-native analytics aggregations following the strict `openapi.yaml` contract.

---

## 1. Requirements

- **Python**: 3.11+ (Tested on Python 3.13)
- **MongoDB**: 6.0+ (Tested with MongoDB 7.0 / Atlas compatible; requires `$setWindowFields` and `$densify` support)

---

## 2. Environment Variables

Connection strings are read dynamically from environment variables, with process-level environment variables taking priority over local `.env` values:

| Variable | Default Value | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection URI (supports Atlas SRV) |
| `MONGO_DB` | `attendance_db` | Target MongoDB database name |

---

## 3. Installation & Setup

```bash
# 1. Create and activate virtual environment
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux / macOS:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Seed sample data (optional test dataset)
python sample_seed.py
```

---

## 4. Running the Application

Start the FastAPI application with Uvicorn from the repository root:

```bash
uvicorn app.main:app --port 8000
```

- API Docs (Swagger UI): `http://localhost:8000/docs`
- Health probe: `http://localhost:8000/health` (Returns HTTP 200 when MongoDB is connected, HTTP 503 otherwise).

All required database indexes are created automatically and idempotently at application startup.

---

## 5. Running Tests

Run the automated test suite using pytest:

```bash
pytest -v
```

Tests cover:
- Business rules R1–R8 (IST timezone handling, overnight shifts, 10-minute grace period boundaries, 30-minute overtime thresholds, half-up Decimal rounding, half-day flags, working days, mid-month joiners).
- Strict Pydantic timestamp validation (rejection of seconds, floats, and strings).
- Concurrency and schema validations.
- Health check and explain endpoint validations.

---

## 6. Implementation Status

All 12 endpoints from `openapi.yaml` are fully implemented:
1. `GET /health` — Live MongoDB ping probe.
2. `POST /employees` — Unique customer `emp_code` creation.
3. `GET /employees` — Deterministically sorted and filtered pagination.
4. `POST /attendance/punch-in` — Race-safe atomic punch-in with R1/R2 calculations.
5. `GET /attendance` — Fast indexed attendance queries with secondary sorting.
6. `POST /attendance/punch-out` — Atomic conditional update preventing duplicate punch-outs.
7. `PATCH /attendance/{emp_code}/{date}` — Audit trail regularization with optimistic concurrency.
8. `GET /analytics/employees/{emp_code}/monthly` — Monthly summary aggregation.
9. `GET /analytics/departments/summary` — Cross-collection aggregation including zero-log headcounts.
10. `GET /analytics/leaderboard/late` — Standard competition ranking (`1, 2, 2, 4`) via `$setWindowFields`.
11. `GET /analytics/departments/{department}/trend` — Complete calendar-day trend with MongoDB `$densify` gap filling and 7-day moving average.
12. `GET /admin/explain/{endpoint}` — Raw `executionStats` query explain plans verifying IXSCAN performance.

**Incomplete features:** None. All contract requirements, business rules R1–R10, and concurrency constraints are completely implemented.
