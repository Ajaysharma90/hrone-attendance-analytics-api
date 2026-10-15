import os
import re
import calendar
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import Response
from bson import json_util
from pydantic import BaseModel, ConfigDict, StrictInt, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = PRESENT_STATUSES + ["ABSENT", "LEAVE"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True, name="uq_employees_emp_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="ix_employees_department_emp_code")
    db.employees.create_index([("joined_on", 1)], name="ix_employees_joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_emp_code_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="ix_attendance_date_emp_code")
    db.attendance_logs.create_index([("status", 1), ("date", -1)], name="ix_attendance_status_date")
    db.attendance_logs.create_index([("date", 1), ("late_minutes", -1), ("emp_code", 1)], name="ix_attendance_late_date")


@app.on_event("startup")
def startup():
    ensure_indexes()


def utc_aware(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.replace(microsecond=0)


def epoch_ms(value):
    value = utc_aware(value)
    return int(value.timestamp() * 1000) if value else None


def convert_datetimes(value):
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, dict):
        return {k: convert_datetimes(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [convert_datetimes(v) for v in value]
    return value


def parse_epoch(value):
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    if type(value) is not int or value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, "Timestamp must be valid epoch milliseconds")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, "Invalid timestamp")


def validate_date(value, field="date"):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must use YYYY-MM-DD format")


def month_bounds(month):
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must use YYYY-MM format")
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return first, last


def working_days(start, end):
    if start > end:
        return 0
    return sum(1 for n in range((end - start).days + 1) if (start + timedelta(days=n)).weekday() < 5)



def compute_late_minutes(punch_in, shift_start, shift_date):
    local = utc_aware(punch_in).astimezone(IST)

    start_time = time.fromisoformat(shift_start)
    start_dt = datetime.combine(shift_date, start_time, tzinfo=IST)

    seconds = (local - start_dt).total_seconds()

    if seconds > 600:
        return int(seconds // 60)

    return 0



def compute_work_hours(punch_in, punch_out):
    seconds = Decimal(str((utc_aware(punch_out) - utc_aware(punch_in)).total_seconds()))
    return float((seconds / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))



def compute_overtime(punch_out, shift_end, date_str, shift_start):
    end_time = time.fromisoformat(shift_end)
    start_time = time.fromisoformat(shift_start)

    shift_day = date.fromisoformat(date_str)

    if end_time <= start_time:
        shift_day += timedelta(days=1)

    shift_end_dt = datetime.combine(
        shift_day, end_time, tzinfo=IST
    ).astimezone(UTC)

    print("DEBUG shift_end_dt:", shift_end_dt)
    print("DEBUG punch_out:", utc_aware(punch_out))

    seconds = (
        utc_aware(punch_out) - shift_end_dt
    ).total_seconds()

    if seconds >= 1800:
        return int(seconds // 60)

    return 0



def shift_date_for(local_ts, shift_start, shift_end):
    d = local_ts.date()
    start = time.fromisoformat(shift_start)
    end = time.fromisoformat(shift_end)
    if end <= start and local_ts.time().replace(tzinfo=None) < end:
        d -= timedelta(days=1)
    return d


class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v):
        validate_date(v, "joined_on")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        # Equal times are permitted and represent a 24-hour overnight shift by R1.
        pass
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return convert_datetimes(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return {"items": convert_datetimes(items), "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(404, "Employee not found")

    ts = parse_epoch(body.punched_at)
    local_ts = ts.astimezone(IST)

    attendance_day = shift_date_for(
        local_ts,
        emp["shift_start"],
        emp["shift_end"]
    ).isoformat()

    late = compute_late_minutes(
        ts,
        emp["shift_start"],
        date.fromisoformat(attendance_day)
    )

    record = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "Attendance already exists for this employee and date"
        )

    return convert_datetimes(record)




@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    ts = parse_epoch(body.punched_at)
    # Find the most recent open shift whose punch-in is not later than this punch-out.
    open_records = list(db.attendance_logs.find({"emp_code": body.emp_code, "punch_in": {"$ne": None}},
                                                {"_id": 1, "date": 1, "punch_in": 1, "punch_out": 1})
                        .sort([("date", -1)]).limit(3))
    candidates = []
    for rec in open_records:
        pin = utc_aware(rec.get("punch_in"))
        if pin and pin <= ts and ts <= pin + timedelta(hours=24):
            candidates.append((pin, rec))
    if not candidates:
        existing = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_out": {"$ne": None}, "punch_in": {"$lte": ts}})
        if existing:
            raise HTTPException(409, "Attendance record has already been punched out")
        raise HTTPException(404, "No punch-in found for this employee")
    pin, rec = max(candidates, key=lambda pair: pair[0])
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Attendance record has already been punched out")
    if ts <= pin:
        raise HTTPException(422, "punch_out must be later than punch_in")
    work_hours = compute_work_hours(pin, ts)
    overtime = compute_overtime(ts, emp["shift_end"], rec["date"], emp["shift_start"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": work_hours < 4.50}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "Attendance record has already been punched out")
    return convert_datetimes(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if status is not None:
        if status not in ALL_STATUSES: raise HTTPException(422, "Invalid status")
        q["status"] = status
    df = validate_date(date_from, "date_from") if date_from else None
    dt = validate_date(date_to, "date_to") if date_to else None
    if df and dt and df > dt: raise HTTPException(422, "date_from cannot be after date_to")
    if df or dt:
        q["date"] = {}
        if df: q["date"]["$gte"] = df.isoformat()
        if dt: q["date"]["$lte"] = dt.isoformat()
    total = db.attendance_logs.count_documents(q)
    records = list(db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": convert_datetimes(records), "total": total, "page": page, "page_size": page_size}


def normalize_dt(value):
    return utc_aware(value)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = validate_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None: raise HTTPException(404, "Employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None: raise HTTPException(404, "Attendance record not found")
    old = {"status": rec.get("status"), "punch_in": normalize_dt(rec.get("punch_in")), "punch_out": normalize_dt(rec.get("punch_out")),
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes", 0),
           "overtime_minutes": rec.get("overtime_minutes", 0), "half_day": rec.get("half_day", False)}
    new_status = body.status if body.status is not None else old["status"]
    new_in = parse_epoch(body.punch_in) if body.punch_in is not None else old["punch_in"]
    new_out = parse_epoch(body.punch_out) if body.punch_out is not None else old["punch_out"]
    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT or LEAVE cannot have punch times")
        new_in = new_out = None
    elif new_in is None:
        raise HTTPException(422, "Presence status requires punch_in")
    if new_in is not None:
        local_in = new_in.astimezone(IST)
        shift_day = shift_date_for(local_in, emp["shift_start"], emp["shift_end"])
        if shift_day != day: raise HTTPException(422, "punch_in does not belong to this attendance date")
    if new_out is not None:
        if new_in is None: raise HTTPException(422, "punch_out requires punch_in")
        if new_out <= new_in or new_out > new_in + timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late = 0; work = None; overtime = 0; half = False
    if new_in is not None:
        local_in = new_in.astimezone(IST)
        shift_day = shift_date_for(local_in, emp["shift_start"], emp["shift_end"])
        late = compute_late_minutes(new_in, emp["shift_start"], shift_day)
        if new_out is not None:
            work = compute_work_hours(new_in, new_out)
            overtime = compute_overtime(new_out, emp["shift_end"], date, emp["shift_start"])
            half = work < 4.50
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": work,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    changes = {}
    for field in old:
        if old[field] != new[field]:
            if field in ("punch_in", "punch_out"):
                changes[field] = {"from": epoch_ms(old[field]), "to": epoch_ms(new[field])}
            else:
                changes[field] = {"from": old[field], "to": new[field]}
    if not changes: raise HTTPException(422, "The request does not change anything")
    if len(body.reason.strip()) < 5 or len(body.regularized_by.strip()) < 1:
        raise HTTPException(422, "reason must be 5-200 characters and regularized_by 1-50 characters")
    history = rec.get("history", [])
    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by.strip(), "reason": body.reason.strip(), "changes": changes}
    result = db.attendance_logs.update_one({"_id": rec["_id"], "history": history},
        {"$set": {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": work,
                  "late_minutes": late, "overtime_minutes": overtime, "half_day": half}, "$push": {"history": history_entry}})
    if result.modified_count != 1: raise HTTPException(409, "Record changed concurrently; retry the request")
    updated = db.attendance_logs.find_one({"_id": rec["_id"]}, {"_id": 0})
    return convert_datetimes(updated)


# Aggregation helpers. Analytics are calculated inside MongoDB pipelines.
def month_match(month):
    first, last = month_bounds(month)
    return first.isoformat(), last.isoformat(), first, last


def monthly_employee_pipeline(emp_code, first_s, last_s):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}},
        {"$addFields": {"_date": {"$dateFromString": {"dateString": "$date"}},
                         "_status": {"$ifNull": ["$status", "ABSENT"]},
                         "_late": {"$ifNull": ["$late_minutes", 0]}, "_ot": {"$ifNull": ["$overtime_minutes", 0]},
                         "_half": {"$ifNull": ["$half_day", False]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$_status", PRESENT_STATUSES]}, {"$lt": [{"$dayOfWeek": "$_date"}, 7]}, {"$gt": [{"$dayOfWeek": "$_date"}, 1]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$_status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late"}, "total_overtime_minutes": {"$sum": "$_ot"}}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    first_s, last_s, first, last = month_match(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None: raise HTTPException(404, "Employee not found")
    joined = validate_date(emp["joined_on"], "joined_on")
    wd = working_days(max(first, joined), last) if joined <= last else 0
    rows = list(db.attendance_logs.aggregate(monthly_employee_pipeline(emp_code, first_s, last_s)))
    stats = rows[0] if rows else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    present = float(stats.get("present_days", 0))
    pct = round_half_up(present / wd * 100, 4) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": stats.get("leave_days", 0), "late_count": stats.get("late_count", 0),
            "total_late_minutes": stats.get("total_late_minutes", 0), "total_overtime_minutes": stats.get("total_overtime_minutes", 0), "attendance_pct": pct}


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def department_summary_pipeline(first_s, last_s, department=None):
    match = {"joined_on": {"$lte": last_s}}
    if department is not None: match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]}, {"$lte": ["$date", last_s]}]}}},
            {"$project": {"status": 1, "date": 1, "work_hours": 1, "late_minutes": 1, "half_day": 1}}
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"_hasLog": {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                         "_weekday": {"$cond": [{"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 0]},
                         "_status": {"$ifNull": ["$logs.status", ""]}, "_late": {"$ifNull": ["$logs.late_minutes", 0]}}},
        {"$group": {"_id": "$department", "_employee_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$_status", PRESENT_STATUSES]}, {"$gt": ["$_weekday", 1]}, {"$lt": ["$_weekday", 7]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
            "work_sum": {"$sum": {"$cond": [{"$and": [{"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}, {"$in": ["$_status", PRESENT_STATUSES]}]}, "$logs.work_hours", 0]}},
            "work_count": {"$sum": {"$cond": [{"$and": [{"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}, {"$in": ["$_status", PRESENT_STATUSES]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": "$_late"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$_status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$_status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$_employee_codes"}, "present_days": 1,
                      "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]}, {"$round": [{"$divide": ["$work_sum", "$work_count"]}, 2]}, None]},
                      "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    first_s, last_s, _, _ = month_match(month)
    items = list(db.employees.aggregate(department_summary_pipeline(first_s, last_s, department), allowDiskUse=True))
    # Averages are rounded to two decimal places using decimal half-up at the response boundary.
    for item in items:
        if item.get("avg_work_hours") is not None: item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
        item["present_days"] = float(item.get("present_days", 0))
    return {"month": month, "items": items}


def leaderboard_pipeline(first_s, last_s, department=None, limit=10):
    pipeline = [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1},
                     "name": {"$first": "$employee.name"}, "department": {"$first": "$employee.department"}}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"rank": 1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}}
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first_s, last_s, _, _ = month_match(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(first_s, last_s, department, limit), allowDiskUse=True))
    return {"month": month, "items": items}


def trend_pipeline(department, from_date, to_date):
    from_s, to_s = from_date.isoformat(), to_date.isoformat()
    return [
        {"$match": {"department": department, "joined_on": {"$lte": to_s}}},
        {"$addFields": {"_joinDate": {"$dateFromString": {"dateString": "$joined_on"}}, "_fromDate": {"$dateFromString": {"dateString": from_s}}, "_toDate": {"$dateFromString": {"dateString": to_s}}}},
        {"$addFields": {"_startDate": {"$cond": [{"$gt": ["$_joinDate", "$_fromDate"]}, "$_joinDate", "$_fromDate"]}}},
        {"$addFields": {"_days": {"$map": {"input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": "$_startDate", "endDate": "$_toDate", "unit": "day"}}, 1]}]}, "as": "n", "in": {"$dateAdd": {"startDate": "$_startDate", "unit": "day", "amount": "$$n"}}}}}},
        {"$unwind": "$_days"},
        {"$addFields": {"_dayString": {"$dateToString": {"date": "$_days", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "day": "$_dayString"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$date", "$$day"]}]}}},
            {"$project": {"status": 1, "late_minutes": 1, "half_day": 1}}
        ], "as": "_logs"}},
        {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
               {"$group": {
            "_id": "$_dayString",
            "headcount": {"$addToSet": "$emp_code"},
            "present_count": {
                "$sum": {
                    "$cond": [
                        {
                            "$in": [
                                "$_logs.status",
                                ["PRESENT", "WFH", "ON_DUTY"]
                            ]
                        },
                        1,
                        0
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$_logs.late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "date": "$_id", "headcount": {"$size": "$headcount"}, "present_count": 1, "late_count": 1,
            "is_working_day": {"$and": [{"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, 1]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, 7]}]},
            "attendance_rate": {"$cond": [{"$and": [{"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, 1]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, 7]}, {"$gt": [{"$size": "$headcount"}, 0]}]}, {"$round": [{"$multiply": [{"$divide": ["$present_count", {"$size": "$headcount"}]}, 100]}, 4]}, None]}}},
        {"$sort": {"date": 1}},
       {"$setWindowFields": {
    "sortBy": {"date": 1},
    "output": {
        "moving_avg_7d": {
            "$avg": {
                "$ifNull": ["$attendance_rate", 0]
            },
            "window": {"documents": [-6, 0]}
        }
    }
}},
        {"$project": {"date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start = validate_date(from_date, "from")
    end = validate_date(to_date, "to")
    if start > end: raise HTTPException(422, "from cannot be after to")
    if (end - start).days > 91: raise HTTPException(422, "Trend range cannot exceed 92 calendar days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None: raise HTTPException(404, "Department not found")
    items = list(db.employees.aggregate(trend_pipeline(department, start, end), allowDiskUse=True))
    # The pipeline emits all dates for each department with at least one eligible employee.
    return {"department": department, "items": items}



def make_json_safe(value):
    if isinstance(value, dict):
        return {
            str(key): make_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [make_json_safe(item) for item in value]

    if hasattr(value, "as_datetime"):
        return str(value)

    if hasattr(value, "isoformat"):
        return value.isoformat()

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    return str(value)




@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    if endpoint == "attendance_list":
        q = {}

        if emp_code is not None:
            q["emp_code"] = emp_code

        if status is not None:
            if status not in ALL_STATUSES:
                raise HTTPException(422, "Invalid status")
            q["status"] = status

        df = validate_date(date_from, "date_from") if date_from else None
        dt = validate_date(date_to, "date_to") if date_to else None

        if df and dt and df > dt:
            raise HTTPException(422, "date_from cannot be after date_to")

        if df or dt:
            q["date"] = {}
            if df:
                q["date"]["$gte"] = df.isoformat()
            if dt:
                q["date"]["$lte"] = dt.isoformat()

        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size
        }

        return make_json_safe(
            db.command({"explain": cmd, "verbosity": "executionStats"})
        )

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")

        fs, ls, _, _ = month_match(month)

        return make_json_safe(
            db.command({
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": monthly_employee_pipeline(emp_code, fs, ls),
                    "cursor": {}
                },
                "verbosity": "executionStats"
            })
        )

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")

        fs, ls, _, _ = month_match(month)

        return make_json_safe(
            db.command({
                "explain": {
                    "aggregate": "employees",
                    "pipeline": department_summary_pipeline(fs, ls, department),
                    "cursor": {},
                    "allowDiskUse": True
                },
                "verbosity": "executionStats"
            })
        )

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        fs, ls, _, _ = month_match(month)

        return make_json_safe(
            db.command({
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": leaderboard_pipeline(fs, ls, department, limit),
                    "cursor": {},
                    "allowDiskUse": True
                },
                "verbosity": "executionStats"
            })
        )

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")

        fs = validate_date(from_date, "from")
        ts = validate_date(to_date, "to")

        if fs > ts:
            raise HTTPException(422, "from cannot be after to")

        return make_json_safe(
            db.command({
                "explain": {
                    "aggregate": "employees",
                    "pipeline": trend_pipeline(department, fs, ts),
                    "cursor": {},
                    "allowDiskUse": True
                },
                "verbosity": "executionStats"
            })
        )

    raise HTTPException(
        422,
        "Unknown endpoint. Use attendance_list, employee_monthly, department_summary, late_leaderboard, or department_trend"
    )
