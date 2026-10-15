# Design Decisions

## 1. Indexes

I created a unique index on `employees.emp_code` so duplicate employee codes cannot be inserted. I added an index on department and employee code for department filtering, plus an index on joined date for employee eligibility. For attendance, I used a unique compound index on employee code and date to prevent duplicate daily records. Additional indexes support date-based listing, monthly employee queries, status filtering, and the late leaderboard. I would avoid adding indexes without evidence that a query needs them because every index increases write and storage costs.

## 2. Punch-in race

Two requests can arrive for the same employee and date at the same time. The unique compound index allows only one attendance record for that employee and date. The request that successfully inserts the record receives success; the other request receives a conflict response. This makes the database enforce the uniqueness guarantee rather than relying only on an application-level check.

## 3. Leaderboard ties

The leaderboard ranks employees by late minutes and includes employees tied at the cutoff rank. Employee code provides deterministic ordering among tied employees.

## 4. Headcount

The department summary starts with eligible employees and looks up their attendance records. Employees without attendance logs remain in the result and are included in headcount.

## 5. Scaling

For much larger datasets, I would measure query performance, review execution plans, and consider partitioning or archiving older attendance records if justified.
