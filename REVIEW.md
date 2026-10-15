# Code Review

The implementation was reviewed against the attendance API requirements, including data validation, concurrency, calculated fields, analytics, and query performance.

| # | Where | Defect or risk | How to notice it | Fix or handling |
|---|---|---|---|---|
| 1 | Employee creation | Duplicate employee codes must not be allowed. | Submit the same employee code twice. | Enforce a unique MongoDB index and return a conflict response. |
| 2 | Punch-in | Concurrent requests must not create duplicate daily records. | Send two punch-in requests concurrently for the same employee and date. | Use a unique compound index and handle duplicate-key conflicts. |
| 3 | Punch-out | Concurrent requests must not overwrite a completed punch-out. | Send two punch-out requests concurrently. | Use a conditional atomic update and return a conflict when no record is updated. |
| 4 | Regularization | Concurrent updates must not silently lose history entries. | Submit competing regularization requests. | Match the previously read history during the update and return a conflict if the record changed. |
| 5 | Analytics | Employees without attendance logs must still count toward headcount. | Include an eligible employee with no logs in a department summary. | Start from eligible employees and look up attendance records. |
| 6 | Query performance | Analytics and listing queries need suitable indexes. | Inspect execution statistics using the explain endpoints. | Add indexes supporting the relevant filters and sorts. |

## Not confirmed

The table records implementation risks and the handling verified during testing. Compare each item with the original starter code before treating it as a confirmed starter defect. Add any other defects you actually found, with the relevant function and observed symptom.
