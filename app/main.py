"""
Employee Attendance & Analytics API - HROne Engineering Assignment v2.0
Authoritative specification: openapi.yaml, DATA_MODEL.md, PROBLEM_STATEMENT.docx
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
import time
from typing import Annotated, Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load environment variables without overriding actual process environment variables
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Database Indexes Setup
# --------------------------------------------------------------------------- #
def init_db_indexes() -> None:
    """Create all required indexes idempotently at startup."""
    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")

        # attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_att_emp_date")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_att_date_emp")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_att_emp_punch_in")
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)], name="idx_att_date_late")
        db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="idx_att_date_status")
    except Exception:
        # If MongoDB is currently unavailable at launch, indexes will be created upon first connection / health check
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def to_whole_seconds_ms(ms: int) -> int:
    """Truncates epoch milliseconds to whole seconds (R1)."""
    return (int(ms) // 1000) * 1000


def ms_to_utc_dt(ms: int) -> datetime:
    """Converts epoch milliseconds (truncated to whole seconds) to UTC datetime."""
    truncated = to_whole_seconds_ms(ms)
    return datetime.fromtimestamp(truncated / 1000.0, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts datetime to integer epoch milliseconds truncated to whole seconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def now_epoch_ms() -> int:
    return int(time.time()) * 1000


def now_utc_dt() -> datetime:
    return ms_to_utc_dt(now_epoch_ms())


def is_shift_overnight(shift_start: str, shift_end: str) -> bool:
    """Returns True if shift_end <= shift_start (e.g. 22:00 to 06:00)."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh, em) <= (sh, sm)


def compute_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    ist_dt = punch_in_dt.astimezone(IST)
    cal_date = ist_dt.date()
    if is_shift_overnight(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        punch_time = (ist_dt.hour, ist_dt.minute, ist_dt.second)
        if punch_time < (eh, em, 0):
            cal_date = cal_date - timedelta(days=1)
    return cal_date.isoformat()


def get_shift_start_dt(attendance_date_str: str, shift_start: str) -> datetime:
    """Returns shift_start as an IST-aware datetime on attendance date."""
    d = date.fromisoformat(attendance_date_str)
    h, m = map(int, shift_start.split(":"))
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)


def get_shift_end_dt(attendance_date_str: str, shift_start: str, shift_end: str) -> datetime:
    """Returns shift_end as an IST-aware datetime (next day if overnight shift)."""
    d = date.fromisoformat(attendance_date_str)
    h, m = map(int, shift_end.split(":"))
    if is_shift_overnight(shift_start, shift_end):
        d = d + timedelta(days=1)
    return datetime(d.year, d.month, d.day, h, m, 0, tzinfo=IST)


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R2: Late.
    Grace period is 10 minutes. A punch-in is late only if strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    shift_start_dt = get_shift_start_dt(attendance_date_str, shift_start)
    punch_in_ist = punch_in_dt.astimezone(IST)
    diff_sec = int((punch_in_ist - shift_start_dt).total_seconds())
    if diff_sec <= 600:
        return 0
    return diff_sec // 60


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R3: Overtime.
    Whole minutes (floor) from shift_end to punch_out, counted only when >= 30; otherwise 0.
    Handles overnight shifts where shift_end is on the following calendar day.
    """
    shift_end_dt = get_shift_end_dt(attendance_date_str, shift_start, shift_end)
    punch_out_ist = punch_out_dt.astimezone(IST)
    diff_sec = int((punch_out_ist - shift_end_dt).total_seconds())
    minutes = diff_sec // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = int((punch_out_dt - punch_in_dt).total_seconds())
    val = Decimal(diff_sec) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: Half day.
    After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: Rounding of reported numbers: half-up (2 decimals or 4 decimals for rates)."""
    if val is None:
        return None
    target = Decimal("10") ** -decimals
    return float(Decimal(str(val)).quantize(target, rounding=ROUND_HALF_UP))


def compute_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Mon-Fri working days in the month, counted from joined_on for mid-month joiners.
    """
    year, month = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, month)
    start_d = date(year, month, 1)
    end_d = date(year, month, last_day)
    join_d = date.fromisoformat(joined_on_str)
    if join_d > end_d:
        return 0
    eff_start = max(start_d, join_d)
    count = 0
    cur = eff_start
    while cur <= end_d:
        if cur.weekday() < 5:  # Monday=0 ... Friday=4
            count += 1
        cur += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Schemas & Validation
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts_and_date(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date YYYY-MM-DD")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_entries = []
    for h in doc.get("history", []):
        changes = {}
        for field, change in h.get("changes", {}).items():
            from_v = change.get("from")
            to_v = change.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(from_v, datetime):
                    from_v = dt_to_epoch_ms(from_v)
                if isinstance(to_v, datetime):
                    to_v = dt_to_epoch_ms(to_v)
            changes[field] = {"from": from_v, "to": to_v}
        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_val = dt_to_epoch_ms(at_val)
        history_entries.append({
            "at": at_val,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_entries,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# 1. GET /health
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        init_db_indexes()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# 2. POST /employees
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    created_at = now_utc_dt()
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": created_at,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return format_employee_doc(doc)


# 3. GET /employees
@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# 4. POST /attendance/punch-in
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    punch_in_dt = ms_to_utc_dt(body.punched_at) if body.punched_at is not None else now_utc_dt()
    att_date = compute_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], emp["shift_end"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for that date")

    return format_attendance_doc(doc)


# 5. POST /attendance/punch-out
@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    punch_out_dt = ms_to_utc_dt(body.punched_at) if body.punched_at is not None else now_utc_dt()

    # Find employee's most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="That record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly after punch_in and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    half_day = compute_half_day(work_hours)
    overtime_min = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])

    # Atomic conditional update ensuring concurrent punch-out requests yield exactly one 200 and others 409
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="That record is already punched out")

    return format_attendance_doc(updated)


# 6. GET /attendance
@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not exceed date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# 7. PATCH /attendance/{emp_code}/{date}
@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="No attendance record for that date")

    cur_status = record["status"]
    cur_punch_in = record.get("punch_in")
    if cur_punch_in and cur_punch_in.tzinfo is None:
        cur_punch_in = cur_punch_in.replace(tzinfo=timezone.utc)
    cur_punch_out = record.get("punch_out")
    if cur_punch_out and cur_punch_out.tzinfo is None:
        cur_punch_out = cur_punch_out.replace(tzinfo=timezone.utc)
    cur_work_hours = record.get("work_hours")
    cur_late_minutes = record.get("late_minutes", 0)
    cur_overtime_minutes = record.get("overtime_minutes", 0)
    cur_half_day = bool(record.get("half_day", False))

    target_status = body.status if body.status is not None else cur_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Setting status ABSENT or LEAVE clears both punch times; supplying punch times is invalid",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        new_punch_in = ms_to_utc_dt(body.punch_in) if body.punch_in is not None else cur_punch_in
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        calc_date = compute_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], emp["shift_end"], date)

        new_punch_out = ms_to_utc_dt(body.punch_out) if body.punch_out is not None else cur_punch_out
        if new_punch_out is not None:
            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

    # Build changes dictionary comparing old vs new
    changes = {}
    if cur_status != target_status:
        changes["status"] = {"from": cur_status, "to": target_status}
    if cur_punch_in != new_punch_in:
        changes["punch_in"] = {"from": cur_punch_in, "to": new_punch_in}
    if cur_punch_out != new_punch_out:
        changes["punch_out"] = {"from": cur_punch_out, "to": new_punch_out}
    if cur_work_hours != new_work_hours:
        changes["work_hours"] = {"from": cur_work_hours, "to": new_work_hours}
    if cur_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": cur_late_minutes, "to": new_late_minutes}
    if cur_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": cur_overtime_minutes, "to": new_overtime_minutes}
    if cur_half_day != new_half_day:
        changes["half_day"] = {"from": cur_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    correction_time = now_utc_dt()
    history_entry = {
        "at": correction_time,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking on history array length to guarantee concurrent edits do not lose history
    existing_history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{existing_history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification conflict; please retry")

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines (Pure MongoDB Aggregations)
# --------------------------------------------------------------------------- #

# Helper for employee_monthly pipeline
def build_employee_monthly_pipeline(emp_code: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


# 8. GET /analytics/employees/{emp_code}/monthly
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    year, m_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m_num)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    working_days = compute_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late = int(res.get("total_late_minutes", 0))
        total_overtime = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_overtime = 0

    if working_days > 0:
        pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal(100)
        attendance_pct = float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_overtime,
        "attendance_pct": attendance_pct,
    }


# Helper for department_summary pipeline
def build_department_summary_pipeline(
    start_date: str, end_date: str, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "emp_stats": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": {
                            "present_days": 0.0,
                            "sum_work_hours": 0.0,
                            "count_work_hours": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0,
                        },
                        "in": {
                            "present_days": {
                                "$add": [
                                    "$$value.present_days",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {
                                                        "$lte": [
                                                            {
                                                                "$isoDayOfWeek": {
                                                                    "$dateFromString": {"dateString": "$$this.date"}
                                                                }
                                                            },
                                                            5,
                                                        ]
                                                    },
                                                ]
                                            },
                                            {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1.0]},
                                            0.0,
                                        ]
                                    },
                                ]
                            },
                            "sum_work_hours": {
                                "$add": [
                                    "$$value.sum_work_hours",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$$this.work_hours", None]},
                                                ]
                                            },
                                            "$$this.work_hours",
                                            0.0,
                                        ]
                                    },
                                ]
                            },
                            "count_work_hours": {
                                "$add": [
                                    "$$value.count_work_hours",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$$this.work_hours", None]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    },
                                ]
                            },
                            "late_count": {
                                "$add": [
                                    "$$value.late_count",
                                    {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]},
                                ]
                            },
                            "total_late_minutes": {
                                "$add": [
                                    "$$value.total_late_minutes",
                                    {"$ifNull": ["$$this.late_minutes", 0]},
                                ]
                            },
                            "leave_count": {
                                "$add": [
                                    "$$value.leave_count",
                                    {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]},
                                ]
                            },
                            "on_duty_count": {
                                "$add": [
                                    "$$value.on_duty_count",
                                    {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]},
                                ]
                            },
                        },
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_stats.present_days"},
                "sum_work_hours": {"$sum": "$emp_stats.sum_work_hours"},
                "count_work_hours": {"$sum": "$emp_stats.count_work_hours"},
                "late_count": {"$sum": "$emp_stats.late_count"},
                "total_late_minutes": {"$sum": "$emp_stats.total_late_minutes"},
                "leave_count": {"$sum": "$emp_stats.leave_count"},
                "on_duty_count": {"$sum": "$emp_stats.on_duty_count"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": "$headcount",
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$count_work_hours", 0]},
                        {"$divide": ["$sum_work_hours", "$count_work_hours"]},
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
    ]


# 9. GET /analytics/departments/summary
@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m_num)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline = build_department_summary_pipeline(start_date, end_date, department)
    results = list(db.employees.aggregate(pipeline))

    formatted_items = []
    for item in results:
        avg_wh = item.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
        formatted_items.append({
            "department": item["department"],
            "headcount": int(item["headcount"]),
            "present_days": float(item["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(item["late_count"]),
            "total_late_minutes": int(item["total_late_minutes"]),
            "leave_count": int(item["leave_count"]),
            "on_duty_count": int(item["on_duty_count"]),
        })

    return {
        "month": month,
        "items": formatted_items,
    }


# Helper for late_leaderboard pipeline
def build_late_leaderboard_pipeline(
    start_date: str, end_date: str, limit: int, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "_id": ASCENDING,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
    ])
    return pipeline


# 10. GET /analytics/leaderboard/late
@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m_num)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    pipeline = build_late_leaderboard_pipeline(start_date, end_date, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


# Helper for department_trend pipeline
def build_department_trend_pipeline(department: str, from_str: str, to_str: str) -> List[Dict[str, Any]]:
    from_date_dt = datetime.strptime(from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    to_date_dt = datetime.strptime(to_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    upper_bound_dt = to_date_dt + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date_dt": {"$literal": from_date_dt}}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_date_dt, upper_bound_dt],
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_docs",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$cur_date"]},
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "emp.department": department,
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                                }
                            },
                        }
                    },
                ],
                "as": "log_stats",
            }
        },
        {
            "$project": {
                "date": "$date_str",
                "is_working_day": "$is_working_day",
                "headcount": {"$ifNull": [{"$first": "$hc_docs.count"}, 0]},
                "present_count": {"$ifNull": [{"$first": "$log_stats.present_count"}, 0.0]},
                "late_count": {"$ifNull": [{"$first": "$log_stats.late_count"}, 0]},
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
    ]


# 11. GET /analytics/departments/{department}/trend
@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    emp_check = db.employees.find_one({"department": department})
    if not emp_check:
        raise HTTPException(status_code=404, detail="Unknown department")

    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be greater than or equal to 'from' date")

    day_span = (d_to - d_from).days + 1
    if day_span > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds maximum allowed limit of 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db.employees.aggregate(pipeline))

    items = []
    for row in raw_items:
        rate = row.get("attendance_rate")
        if rate is not None:
            rate = round_half_up(rate, 4)
        mavg = row.get("moving_avg_7d")
        if mavg is not None:
            mavg = round_half_up(mavg, 4)

        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": mavg,
        })

    return {
        "department": department,
        "items": items,
    }


# 12. GET /admin/explain/{endpoint}
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        if endpoint == "attendance_list":
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from must not exceed date_to")
            q: Dict[str, Any] = {}
            if emp_code:
                q["emp_code"] = emp_code
            if date_from or date_to:
                q["date"] = {}
                if date_from:
                    q["date"]["$gte"] = date_from
                if date_to:
                    q["date"]["$lte"] = date_to
            if status:
                q["status"] = status
            skip = (page - 1) * page_size
            explain_cmd = {
                "explain": {
                    "find": "attendance_logs",
                    "filter": q,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": skip,
                    "limit": page_size,
                },
                "verbosity": "executionStats",
            }
            res = db.command(explain_cmd)
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
            year, m_num = map(int, month.split("-"))
            _, last_day = calendar.monthrange(year, m_num)
            start_date = f"{month}-01"
            end_date = f"{month}-{last_day:02d}"
            pipeline = build_employee_monthly_pipeline(emp_code, start_date, end_date)
            explain_cmd = {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            res = db.command(explain_cmd)
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="month is required for department_summary")
            year, m_num = map(int, month.split("-"))
            _, last_day = calendar.monthrange(year, m_num)
            start_date = f"{month}-01"
            end_date = f"{month}-{last_day:02d}"
            pipeline = build_department_summary_pipeline(start_date, end_date, department)
            explain_cmd = {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            res = db.command(explain_cmd)
            return {"endpoint": endpoint, "collection": "employees", "explain": res}

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
            year, m_num = map(int, month.split("-"))
            _, last_day = calendar.monthrange(year, m_num)
            start_date = f"{month}-01"
            end_date = f"{month}-{last_day:02d}"
            pipeline = build_late_leaderboard_pipeline(start_date, end_date, limit, department)
            explain_cmd = {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            res = db.command(explain_cmd)
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

        elif endpoint == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(
                    status_code=422,
                    detail="department, from and to are required for department_trend",
                )
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
            if d_to < d_from:
                raise HTTPException(status_code=422, detail="'to' date must be greater than or equal to 'from' date")
            if (d_to - d_from).days + 1 > 92:
                raise HTTPException(status_code=422, detail="Date range exceeds 92 days")
            pipeline = build_department_trend_pipeline(department, from_date, to_date)
            explain_cmd = {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
            res = db.command(explain_cmd)
            return {"endpoint": endpoint, "collection": "employees", "explain": res}

        else:
            raise HTTPException(status_code=422, detail="Unknown endpoint for explain")

    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=str(e))
