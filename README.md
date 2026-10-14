# HROne Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, regularization, and analytics.

## Requirements
- Python 3.10 or newer
- MongoDB 6.0 or newer
- Git

## Setup (Windows PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a .env file in the project root:
```env
MONGO_URI=mongodb://127.0.0.1:27017
MONGO_DB=hrone_attendance
```

Make sure MongoDB is running, then run:
```powershell
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

Open http://127.0.0.1:8000/docs to explore the API using Swagger UI.

## Features
- Employee creation and listing with filtering and pagination
- Attendance punch-in and punch-out
- Attendance history and regularization
- Employee monthly analytics
- Department summaries and attendance trends
- Late-arrival leaderboard
- MongoDB explain plans and indexes

## Project Files
- pp/main.py — API implementation
- equirements.txt — Python dependencies
- openapi.yaml — API contract
- DATA_MODEL.md — data model
- sample_data/ and sample_seed.py — sample data and loader
- REVIEW.md — starter defects and fixes
- DECISIONS.md — design decisions

## Notes
- MongoDB 6.0 or newer is required.
- Never commit .env, credentials, or other secrets.
- Sample-data performance does not guarantee production-scale performance.

## Submission
Submit the public GitHub repository URL through the assignment submission form. Keep the repository public for the required period.
