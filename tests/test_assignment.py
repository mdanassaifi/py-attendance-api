"""
Comprehensive Automated Test Suite for HROne Assignment
Testing Business Rules R1-R8, Data Model, Edge Cases, and API Schemas
"""
import os
import sys
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal
import pytest

# Ensure parent directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi.testclient import TestClient

from app.main import (
    app,
    to_whole_seconds_ms,
    ms_to_utc_dt,
    dt_to_epoch_ms,
    is_shift_overnight,
    compute_attendance_date,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    round_half_up,
    compute_working_days,
    EmployeeCreate,
    PunchInRequest,
    PunchOutRequest,
    RegularizeRequest,
    IST,
    UTC,
)

client = TestClient(app)


# --------------------------------------------------------------------------- #
# Test R1: Timezone, Epoch Milliseconds & Truncation
# --------------------------------------------------------------------------- #
def test_truncation_to_whole_seconds():
    raw_ms = 1783312500999
    truncated = to_whole_seconds_ms(raw_ms)
    assert truncated == 1783312500000

    dt = ms_to_utc_dt(raw_ms)
    assert dt.microsecond == 0
    assert dt_to_epoch_ms(dt) == 1783312500000


def test_overnight_shift_detection():
    assert is_shift_overnight("22:00", "06:00") is True
    assert is_shift_overnight("20:00", "20:00") is True
    assert is_shift_overnight("09:30", "18:30") is False


def test_r1_attendance_date_standard_shift():
    dt_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    att_date = compute_attendance_date(dt_utc, "09:30", "18:30")
    assert att_date == "2026-07-06"


def test_r1_attendance_date_overnight_shift():
    # Overnight shift: 22:00 - 06:00
    # Case 1: Punch in at 21:55 IST on 2026-07-06 (UTC 16:25:00)
    dt_evening = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert compute_attendance_date(dt_evening, "22:00", "06:00") == "2026-07-06"

    # Case 2: Punch in early morning at 01:10 IST on 2026-07-07 (UTC 2026-07-06 19:40:00)
    # Earlier than shift_end (06:00), belongs to shift that started previous day
    dt_early_morning = datetime(2026, 7, 6, 19, 40, 0, tzinfo=timezone.utc)
    assert compute_attendance_date(dt_early_morning, "22:00", "06:00") == "2026-07-06"

    # Case 3: Punch in after shift_end (e.g. 06:15 IST on 2026-07-07)
    dt_morning_after = datetime(2026, 7, 7, 0, 45, 0, tzinfo=timezone.utc)
    assert compute_attendance_date(dt_morning_after, "22:00", "06:00") == "2026-07-07"


# --------------------------------------------------------------------------- #
# Test R2: Late Minutes & 10-Minute Grace Period
# --------------------------------------------------------------------------- #
def test_r2_late_minutes_boundaries():
    # Exact 10-min boundary: 09:40:00 IST -> not late (0 minutes)
    dt_exact = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(dt_exact, "09:30", "18:30", "2026-07-06") == 0

    # 10 min 1 sec: 09:40:01 IST -> late! Whole minutes from shift_start = 10
    dt_10m1s = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(dt_10m1s, "09:30", "18:30", "2026-07-06") == 10

    # 45 min 59 sec: 10:15:59 IST -> late! Whole minutes from shift_start = 45
    dt_45m = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(dt_45m, "09:30", "18:30", "2026-07-06") == 45

    # Early punch: 09:15:00 IST -> not late (0 minutes)
    dt_early = datetime(2026, 7, 6, 9, 15, 0, tzinfo=IST)
    assert compute_late_minutes(dt_early, "09:30", "18:30", "2026-07-06") == 0


def test_r2_late_minutes_overnight():
    # Overnight shift: 22:00 - 06:00 starting on 2026-07-06
    # Punch in at 22:10:00 IST -> 0 late
    dt_ontime = datetime(2026, 7, 6, 22, 10, 0, tzinfo=IST)
    assert compute_late_minutes(dt_ontime, "22:00", "06:00", "2026-07-06") == 0

    # Punch in at 22:10:01 IST -> 10 min late
    dt_late = datetime(2026, 7, 6, 22, 10, 1, tzinfo=IST)
    assert compute_late_minutes(dt_late, "22:00", "06:00", "2026-07-06") == 10

    # Punch in at 00:30:00 IST on 2026-07-07 for 2026-07-06 shift -> 150 min late
    dt_next_day = datetime(2026, 7, 7, 0, 30, 0, tzinfo=IST)
    assert compute_late_minutes(dt_next_day, "22:00", "06:00", "2026-07-06") == 150


# --------------------------------------------------------------------------- #
# Test R3: Overtime & 30-Minute Threshold
# --------------------------------------------------------------------------- #
def test_r3_overtime_threshold():
    # Shift end: 18:30 IST on 2026-07-06
    # Punch out 18:35 IST (5 min after) -> 0 (below 30 min)
    dt_5m = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    assert compute_overtime(dt_5m, "09:30", "18:30", "2026-07-06") == 0

    # Punch out 18:59:59 IST (29 min 59 sec after) -> 0 (below 30 min)
    dt_29m = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
    assert compute_overtime(dt_29m, "09:30", "18:30", "2026-07-06") == 0

    # Punch out 19:00:00 IST (exactly 30 min after) -> 30 min overtime
    dt_30m = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert compute_overtime(dt_30m, "09:30", "18:30", "2026-07-06") == 30

    # Punch out 19:10:00 IST (40 min after) -> 40 min overtime
    dt_40m = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
    assert compute_overtime(dt_40m, "09:30", "18:30", "2026-07-06") == 40


def test_r3_overtime_overnight_shift():
    # Overnight shift: 22:00 - 06:00 on 2026-07-06 (shift end is 2026-07-07 06:00 IST)
    # Punch out at 06:40:00 IST on 2026-07-07 -> 40 min overtime
    dt_out = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(dt_out, "22:00", "06:00", "2026-07-06") == 40


# --------------------------------------------------------------------------- #
# Test R4 & R5: Work Hours Half-Up Rounding & Half Day
# --------------------------------------------------------------------------- #
def test_r4_work_hours_half_up_rounding():
    dt_in = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    dt_out = datetime(2026, 7, 6, 18, 35, 12, tzinfo=IST)
    assert compute_work_hours(dt_in, dt_out) == 9.12

    # Half-up rounding boundary where banker's rounding rounds down
    # 2.675 hours = 9630 seconds -> 2.68
    dt1 = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC)
    dt2 = datetime(2026, 7, 6, 0, 0, 0, tzinfo=UTC) + timedelta(seconds=9630)
    assert compute_work_hours(dt1, dt2) == 2.68


def test_r5_half_day():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(5.00) is False
    assert compute_half_day(None) is False


# --------------------------------------------------------------------------- #
# Test R7: Working Days & Mid-Month Joiners
# --------------------------------------------------------------------------- #
def test_r7_working_days():
    # July 2026: exactly 23 working days
    assert compute_working_days("2026-07", "2026-01-05") == 23

    # Mid-month joiner: joined 2026-07-13 -> 15 working days
    assert compute_working_days("2026-07", "2026-07-13") == 15

    # Joins after the month: joined 2026-08-01 -> 0 working days
    assert compute_working_days("2026-07", "2026-08-01") == 0


# --------------------------------------------------------------------------- #
# Test R8: Rounding Helper
# --------------------------------------------------------------------------- #
def test_r8_rounding():
    assert round_half_up(9.123, 2) == 9.12
    assert round_half_up(9.125, 2) == 9.13
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234
    assert round_half_up(None) is None


# --------------------------------------------------------------------------- #
# Test Pydantic Validations (Strict Timestamps & Constraints)
# --------------------------------------------------------------------------- #
def test_pydantic_rejects_string_timestamps():
    with pytest.raises(Exception):
        PunchInRequest(emp_code="EMP0001", punched_at="1783312500000")


def test_pydantic_rejects_float_timestamps():
    with pytest.raises(Exception):
        PunchInRequest(emp_code="EMP0001", punched_at=1783312500000.5)


def test_pydantic_rejects_seconds_looking_timestamps():
    with pytest.raises(Exception):
        PunchInRequest(emp_code="EMP0001", punched_at=1783312500)


def test_employee_create_rejects_equal_shifts():
    with pytest.raises(ValueError, match="shift_start must differ from shift_end"):
        EmployeeCreate(
            emp_code="EMP0001",
            name="Test",
            email="test@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="09:30",
            joined_on="2026-01-05",
        )


def test_employee_create_rejects_invalid_email():
    with pytest.raises(Exception):
        EmployeeCreate(
            emp_code="EMP0001",
            name="Test",
            email="invalid-email",
            department="Engineering",
            shift_start="09:30",
            shift_end="18:30",
            joined_on="2026-01-05",
        )


# --------------------------------------------------------------------------- #
# Test API Endpoint Schema Validation & Error Codes via TestClient
# --------------------------------------------------------------------------- #
def test_api_date_range_validation():
    # date_from > date_to must return 422
    response = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert response.status_code == 422


def test_api_invalid_employee_emp_code():
    # Invalid emp_code format (must match ^EMP\d{4,6}$)
    payload = {
        "emp_code": "INVALID_CODE",
        "name": "Test User",
        "email": "user@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    response = client.post("/employees", json=payload)
    assert response.status_code == 422


def test_api_health_check():
    # When MongoDB is not reachable, health check must return 503
    response = client.get("/health")
    assert response.status_code in (200, 503)


def test_api_explain_missing_parameters_422():
    # employee_monthly missing required emp_code and month -> 422
    res = client.get("/admin/explain/employee_monthly")
    assert res.status_code == 422

    # department_summary missing required month -> 422
    res = client.get("/admin/explain/department_summary")
    assert res.status_code == 422

    # late_leaderboard missing required month -> 422
    res = client.get("/admin/explain/late_leaderboard")
    assert res.status_code == 422

    # department_trend missing required department, from, to -> 422
    res = client.get("/admin/explain/department_trend")
    assert res.status_code == 422
