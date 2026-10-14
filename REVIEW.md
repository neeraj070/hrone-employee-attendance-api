# REVIEW.md

## Defects found in the starter implementation

| # | Where (function / area) | What is wrong | How to notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `health` | The starter reports success without verifying that MongoDB is reachable. | Stop MongoDB and call `GET /health`. | Ping MongoDB and return HTTP 503 when it is unavailable. |
| 2 | `list_employees` | Pagination starts at `page * page_size`, which skips the first page when page numbering starts at 1. | Request page 1 with a page size of 20 and inspect the first records. | Use `(page - 1) * page_size` for the skip offset. |
| 3 | `list_employees` | The total count does not apply the same department filter as the returned records. | Filter by department and compare `total` with the number of matching employees. | Count documents using the same filter as the list query. |
| 4 | `create_employee` | Checking for an existing employee before insertion is vulnerable to concurrent requests. | Submit two requests with the same employee code at nearly the same time. | Enforce uniqueness with a MongoDB unique index and handle `DuplicateKeyError` as HTTP 409. |
| 5 | `punch_in` | The starter does not safely handle an employee code that does not exist. | Punch in using an unknown employee code. | Check for the employee first and return HTTP 404 when not found. |
| 6 | `punch_in` | A separate duplicate check cannot prevent simultaneous requests from creating duplicate attendance records. | Send two punch-in requests for the same employee and attendance date concurrently. | Use a unique compound index on `emp_code` and `date`, and handle duplicate-key errors. |
| 7 | `list_attendance` | Fetching and sorting all matching records in application memory does not scale well. | Test the endpoint with a large number of attendance records. | Apply database sorting, skipping, and limiting before returning results. |
| 8 | Analytics endpoints | The starter contains incomplete analytics functionality that must be implemented to satisfy the API contract. | Exercise the monthly, department summary, leaderboard, and department trend endpoints. | Implement aggregation pipelines and use the shared pipeline builders for explain-plan checks. |

## Reviewed areas that are not necessarily defects

- A `FETCH` stage in an explain plan is not itself a defect. MongoDB may need to retrieve full documents after scanning an index.
- A rejected query plan is not evidence that the winning plan is inefficient. The winning plan and its execution statistics should be examined.
- A unique index is necessary even when application code checks for duplicates, because application-level checks alone are not concurrency-safe.
- Low execution times on the small sample dataset do not prove performance at production scale.
