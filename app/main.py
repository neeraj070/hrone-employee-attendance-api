"""Employee Attendance & Analytics API for the HROne engineering assignment.

Run from the repository root with: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB (a local .env is supported).
"""

import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Annotated, Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.collection import Collection
from pymongo.database import Database

load_dotenv()  # Does not override real environment variables.

MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
db: Database = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
EpochMillis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    """Create the query and uniqueness indexes; safe to run repeatedly."""
    db.employees.create_index([("emp_code", 1)], unique=True, name="uniq_employee_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_employee_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="department_joined_on")
    db.employees.create_index([("joined_on", 1)], name="employee_joined_on")

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="uniq_employee_attendance_date"
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)], name="attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", -1)], name="attendance_employee_date_desc"
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)], name="attendance_employee_punch_in"
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)], name="attendance_status_date_employee"
    )
    db.attendance_logs.create_index(
        [("date", 1), ("late_minutes", 1), ("emp_code", 1)], name="attendance_date_late_employee"
    )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class AttendanceStatus(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be epoch milliseconds when supplied")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMillis] = None

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be epoch milliseconds when supplied")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_supplied_timestamps(self):
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null when supplied")
        for field in ("punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must be epoch milliseconds when supplied")
        return self


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------
def parse_date(value: str, field_name: str = "date") -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid YYYY-MM-DD date")
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    try:
        first = date.fromisoformat(value + "-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="month must be a valid YYYY-MM")
    last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    if first.month == 12:
        next_month = date(first.year + 1, 1, 1)
    else:
        next_month = date(first.year, first.month + 1, 1)
    return first, last, next_month


def month_bounds(value: str) -> tuple[str, str, date, date]:
    first, last, next_month = parse_month(value)
    return first.isoformat(), next_month.isoformat(), first, last


def working_days(start: date, end: date) -> int:
    """Count Monday-Friday calendar days in an inclusive range."""
    if end < start:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def round_half_up(value, places: int = 2) -> float:
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    # Epoch values are validated as strict integer milliseconds by Pydantic.
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)).replace(microsecond=0)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # PyMongo returns naive BSON dates as UTC.
    else:
        value = value.astimezone(UTC)
    value = value.replace(microsecond=0)
    return int(value.timestamp() * 1000)


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def shift_time(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def attendance_date_for(punch_in: datetime, employee: dict) -> date:
    local = utc_datetime(punch_in).astimezone(IST)
    local_day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.timetz().replace(tzinfo=None) < shift_time(employee["shift_end"]):
        local_day -= timedelta(days=1)
    return local_day


def compute_late_minutes(punch_in: Optional[datetime], shift_start: str, attendance_day: date) -> int:
    if punch_in is None:
        return 0
    scheduled_start = datetime.combine(attendance_day, shift_time(shift_start), tzinfo=IST).astimezone(UTC)
    seconds_late = int((utc_datetime(punch_in) - scheduled_start).total_seconds())
    if seconds_late <= 600:
        return 0
    return max(0, seconds_late // 60)


def scheduled_shift_end(attendance_day: date, employee: dict) -> datetime:
    end_day = attendance_day
    if employee["shift_end"] <= employee["shift_start"]:
        end_day += timedelta(days=1)
    return datetime.combine(end_day, shift_time(employee["shift_end"]), tzinfo=IST).astimezone(UTC)


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((utc_datetime(punch_out) - utc_datetime(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: Optional[datetime], employee: dict, attendance_day: date) -> int:
    if punch_out is None:
        return 0
    seconds_after_end = int((utc_datetime(punch_out) - scheduled_shift_end(attendance_day, employee)).total_seconds())
    minutes = seconds_after_end // 60
    return int(minutes) if minutes >= 30 else 0


def derived_values(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], employee: dict, attendance_day: date) -> dict:
    if status not in PRESENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, employee["shift_start"], attendance_day)
    hours = compute_work_hours(punch_in, punch_out)
    overtime = compute_overtime(punch_out, employee, attendance_day) if punch_out else 0
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": hours is not None and hours < 4.50,
    }


def serialize_history(history: list) -> list:
    result = []
    for entry in history or []:
        changes_out = {}
        for field, change in (entry.get("changes") or {}).items():
            old = change.get("from")
            new = change.get("to")
            if field in ("punch_in", "punch_out"):
                old = datetime_to_epoch_ms(old) if isinstance(old, datetime) else old
                new = datetime_to_epoch_ms(new) if isinstance(new, datetime) else new
            changes_out[field] = {"from": old, "to": new}
        at = entry.get("at")
        result.append({
            "at": datetime_to_epoch_ms(at) if isinstance(at, datetime) else at,
            "by": entry.get("by", ""),
            "reason": entry.get("reason", ""),
            "changes": changes_out,
        })
    return result


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")) if isinstance(doc.get("punch_in"), datetime) else None,
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")) if isinstance(doc.get("punch_out"), datetime) else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history", [])),
    }


def validate_status_query(status: Optional[str]) -> Optional[str]:
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(ALL_STATUSES)}")
    return status


def build_attendance_filter(emp_code=None, date_from=None, date_to=None, status=None) -> dict:
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


def validate_range_dates(date_from: Optional[str], date_to: Optional[str]) -> None:
    from_day = parse_date(date_from, "date_from") if date_from is not None else None
    to_day = parse_date(date_to, "date_to") if date_to is not None else None
    if from_day and to_day and from_day > to_day:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")


# ---------------------------------------------------------------------------
# System and employee endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    created_at = datetime.now(UTC).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = created_at
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(created_at)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        item["created_at"] = datetime_to_epoch_ms(item.get("created_at")) if isinstance(item.get("created_at"), datetime) else item.get("created_at")
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance endpoints
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    day = attendance_date_for(ts, employee)
    day_str = day.isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": day_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, employee["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="attendance record already punched out")

    punch_in_at = utc_datetime(record["punch_in"])
    elapsed = (ts - punch_in_at).total_seconds()
    if elapsed <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if elapsed > 24 * 60 * 60:
        raise HTTPException(status_code=422, detail="punch-out must be within 24 hours of punch-in")

    day = parse_date(record["date"], "attendance date")
    hours = compute_work_hours(punch_in_at, ts)
    overtime = compute_overtime(ts, employee, day)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": hours,
            "overtime_minutes": overtime,
            "half_day": hours is not None and hours < 4.50,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    validate_range_dates(date_from, date_to)
    validate_status_query(status)
    query = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    docs = list(
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_attendance(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date, "date")
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    supplied = body.model_fields_set
    old_status = record.get("status", "PRESENT")
    new_status = body.status.value if body.status is not None else old_status
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot include punch times")
        new_in = None
        new_out = None
    else:
        new_in = epoch_ms_to_datetime(body.punch_in) if "punch_in" in supplied else old_in
        new_out = epoch_ms_to_datetime(body.punch_out) if "punch_out" in supplied else old_out
        if new_in is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if attendance_date_for(new_in, employee).isoformat() != day.isoformat():
            raise HTTPException(status_code=422, detail="punch_in must belong to the attendance date")
        if new_out is not None:
            elapsed = (utc_datetime(new_out) - utc_datetime(new_in)).total_seconds()
            if elapsed <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if elapsed > 24 * 60 * 60:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    derived = derived_values(new_status, new_in, new_out, employee, day)
    proposed = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived,
    }
    fields_for_history = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for field in fields_for_history:
        old_value = record.get(field)
        if field == "late_minutes":
            old_value = int(old_value or 0)
        elif field == "overtime_minutes":
            old_value = int(old_value or 0)
        elif field == "half_day":
            old_value = bool(old_value or False)
        elif field == "work_hours":
            old_value = old_value
        new_value = proposed[field]
        if isinstance(old_value, datetime):
            old_cmp = utc_datetime(old_value)
        else:
            old_cmp = old_value
        if isinstance(new_value, datetime):
            new_cmp = utc_datetime(new_value)
        else:
            new_cmp = new_value
        if old_cmp != new_cmp:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(status_code=422, detail="regularization does not change anything")

    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    optimistic_filter = {"_id": record["_id"]}
    if "history" in record:
        optimistic_filter["history"] = record.get("history", [])
    else:
        optimistic_filter["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        optimistic_filter,
        {"$set": proposed, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently; retry")
    return serialize_attendance(updated)


# ---------------------------------------------------------------------------
# Aggregation pipeline builders (shared by real endpoints and explain)
# ---------------------------------------------------------------------------
def employee_monthly_pipeline(emp_code: str, month_start: str, next_month: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lt": next_month}}},
        {"$addFields": {
            "_day": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
            "_status": {"$ifNull": ["$status", ""]},
            "_late": {"$ifNull": ["$late_minutes", 0]},
            "_overtime": {"$ifNull": ["$overtime_minutes", 0]},
            "_half": {"$ifNull": ["$half_day", False]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$_status", list(PRESENCE_STATUSES)]},
                    {"$gte": [{"$dayOfWeek": "$_day"}, 2]},
                    {"$lte": [{"$dayOfWeek": "$_day"}, 6]},
                ]},
                {"$cond": ["$_half", 0.5, 1]}, 0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$_status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late"},
            "total_overtime_minutes": {"$sum": "$_overtime"},
        }},
    ]


def department_summary_pipeline(month_start: str, next_month: str, month_end: str, department: Optional[str]) -> list:
    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", month_start]},
                    {"$lt": ["$date", next_month]},
                ]}}},
            ],
            "as": "_logs",
        }},
        {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "_status": {"$ifNull": ["$_logs.status", ""]},
            "_late": {"$ifNull": ["$_logs.late_minutes", 0]},
            "_overtime": {"$ifNull": ["$_logs.overtime_minutes", 0]},
            "_half": {"$ifNull": ["$_logs.half_day", False]},
            "_work_hours": "$_logs.work_hours",
            "_log_day": {"$dateFromString": {"dateString": "$_logs.date", "onError": None, "onNull": None}},
        }},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "headcount": {"$max": 1},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$_status", list(PRESENCE_STATUSES)]},
                    {"$ne": ["$_log_day", None]},
                    {"$gte": [{"$cond": [{"$ne": ["$_log_day", None]}, {"$dayOfWeek": "$_log_day"}, 0]}, 2]},
                    {"$lte": [{"$cond": [{"$ne": ["$_log_day", None]}, {"$dayOfWeek": "$_log_day"}, 0]}, 6]},
                ]},
                {"$cond": ["$_half", 0.5, 1]}, 0,
            ]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$_status", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$_work_hours", None]}, None]},
                ]}, "$_work_hours", 0,
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$_status", list(PRESENCE_STATUSES)]},
                    {"$ne": [{"$ifNull": ["$_work_hours", None]}, None]},
                ]}, 1, 0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$_status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$_status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$_id.department",
            "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "work_hours_sum": 1,
            "work_hours_count": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month_start: str, next_month: str, department: Optional[str], limit: int) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lt": next_month}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "_employee"}},
        {"$unwind": "$_employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})
    pipeline.extend([
        {"$group": {
            "_id": {"emp_code": "$emp_code", "name": "$_employee.name", "department": "$_employee.department"},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": 1},
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id.emp_code",
            "name": "$_id.name",
            "department": "$_id.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def department_trend_pipeline(department: str, from_day: date, to_day: date) -> list:
    start_dt = datetime.combine(from_day, time.min, tzinfo=UTC)
    end_dt = datetime.combine(to_day, time.min, tzinfo=UTC)
    return [
        {"$documents": [{"department": department, "start": start_dt, "end": end_dt}]},
        {"$set": {"dates": {"$map": {
            "input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": "$start", "endDate": "$end", "unit": "day"}}, 1]}]},
            "as": "offset",
            "in": {"$dateAdd": {"startDate": "$start", "unit": "day", "amount": "$$offset"}},
        }}}},
        {"$unwind": "$dates"},
        {"$set": {
            "date_obj": "$dates",
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates", "timezone": "UTC"}},
            "_weekday_num": {"$dayOfWeek": "$dates"},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day_string": "$date", "dept": "$department"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", "$$dept"]},
                    {"$lte": ["$joined_on", "$$day_string"]},
                ]}}},
                {"$count": "count"},
            ],
            "as": "_headcount_docs",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day_string": "$date", "dept": "$department"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day_string"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "_employee"}},
                {"$unwind": "$_employee"},
                {"$match": {"$expr": {"$eq": ["$_employee.department", "$$dept"]}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
            "as": "_daily_docs",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount_docs.count", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$_daily_docs.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$_daily_docs.late_count", 0]}, 0]},
            "is_working_day": {"$and": [{"$gte": ["$_weekday_num", 2]}, {"$lte": ["$_weekday_num", 6]}]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date_obj": 1},
            "output": {"_moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$ne": ["$attendance_rate", None]},
                {"$divide": [{"$floor": {"$add": [{"$multiply": ["$attendance_rate", 10000]}, 0.5]}}, 10000]},
                None,
            ]},
            "moving_avg_7d": {"$cond": [
                {"$ne": ["$_moving_avg", None]},
                {"$divide": [{"$floor": {"$add": [{"$multiply": ["$_moving_avg", 10000]}, 0.5]}}, 10000]},
                None,
            ]},
        }},
        {"$project": {
            "_id": 0, "department": 0, "start": 0, "end": 0, "dates": 0, "date_obj": 0,
            "_weekday_num": 0, "_headcount_docs": 0, "_daily_docs": 0, "_moving_avg": 0,
        }},
        {"$sort": {"date": 1}},
    ]


def run_aggregate_explain(collection_name: str, pipeline: list) -> dict:
    command = {
        "explain": {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}},
        "verbosity": "executionStats",
    }
    return db.command(command)


def run_database_aggregate_explain(pipeline: list) -> dict:
    command = {
        "explain": {"aggregate": 1, "pipeline": pipeline, "cursor": {}},
        "verbosity": "executionStats",
    }
    return db.command(command)


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    month_start, next_month, first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month_start, next_month)))
    metrics = rows[0] if rows else {}
    joined_on = parse_date(employee["joined_on"], "joined_on")
    start = max(first, joined_on)
    days = working_days(start, last) if start <= last else 0
    present_days = float(metrics.get("present_days", 0) or 0)
    attendance_pct = round_half_up(Decimal(str(present_days)) / Decimal(days) * Decimal(100), 4) if days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": present_days,
        "leave_days": int(metrics.get("leave_days", 0) or 0),
        "late_count": int(metrics.get("late_count", 0) or 0),
        "total_late_minutes": int(metrics.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(metrics.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, next_month, _first, last = month_bounds(month)
    pipeline = department_summary_pipeline(month_start, next_month, last.isoformat(), department)
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for row in rows:
        count = int(row.get("work_hours_count", 0) or 0)
        avg_hours = round_half_up(Decimal(str(row.get("work_hours_sum", 0))) / Decimal(count), 2) if count else None
        items.append({
            "department": row["department"],
            "headcount": int(row.get("headcount", 0) or 0),
            "present_days": float(row.get("present_days", 0) or 0),
            "avg_work_hours": avg_hours,
            "late_count": int(row.get("late_count", 0) or 0),
            "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
            "leave_count": int(row.get("leave_count", 0) or 0),
            "on_duty_count": int(row.get("on_duty_count", 0) or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, next_month, _first, _last = month_bounds(month)
    pipeline = leaderboard_pipeline(month_start, next_month, department, limit)
    items = list(db.attendance_logs.aggregate(pipeline))
    for item in items:
        item["rank"] = int(item["rank"])
        item["total_late_minutes"] = int(item["total_late_minutes"])
        item["late_count"] = int(item["late_count"])
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_day: str = Query(alias="from"),
    to_day: str = Query(alias="to"),
):
    start = parse_date(from_day, "from")
    end = parse_date(to_day, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="department not found")
    pipeline = department_trend_pipeline(department, start, end)
    items = list(db.aggregate(pipeline))
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Query explain endpoint
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_day: Optional[str] = Query(default=None, alias="from"),
    to_day: Optional[str] = Query(default=None, alias="to"),
):
    if endpoint == "attendance_list":
        validate_range_dates(date_from, date_to)
        validate_status_query(status)
        query = build_attendance_filter(emp_code, date_from, date_to, status)
        explain = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint in ("employee_monthly", "department_summary", "late_leaderboard"):
        if month is None:
            raise HTTPException(status_code=422, detail="month is required for this endpoint")
        month_start, next_month, _first, last = month_bounds(month)
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise HTTPException(status_code=422, detail="emp_code is required for this endpoint")
            pipeline = employee_monthly_pipeline(emp_code, month_start, next_month)
            collection = "attendance_logs"
        elif endpoint == "department_summary":
            pipeline = department_summary_pipeline(month_start, next_month, last.isoformat(), department)
            collection = "employees"
        else:
            pipeline = leaderboard_pipeline(month_start, next_month, department, limit)
            collection = "attendance_logs"
        return {"endpoint": endpoint, "collection": collection, "explain": run_aggregate_explain(collection, pipeline)}

    if endpoint == "department_trend":
        if department is None or from_day is None or to_day is None:
            raise HTTPException(status_code=422, detail="department, from, and to are required for this endpoint")
        start = parse_date(from_day, "from")
        end = parse_date(to_day, "to")
        if end < start:
            raise HTTPException(status_code=422, detail="to must be on or after from")
        if (end - start).days + 1 > 92:
            raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
        pipeline = department_trend_pipeline(department, start, end)
        return {"endpoint": endpoint, "collection": "employees + attendance_logs", "explain": run_database_aggregate_explain(pipeline)}

    # The Path constraint normally catches this before reaching the function.
    raise HTTPException(status_code=422, detail="unsupported endpoint")
