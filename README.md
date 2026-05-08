# Nor-Cal Hockey Scheduler MVP

This is a clean Python + Streamlit rebuild of the Alpha scheduling idea.

It removes:
- Google Apps Script
- JavaScript menu/protection code
- Google Sheets as a requirement

It keeps the core idea:
- multiple leagues
- teams by league
- time slots by league
- blackout dates
- generated schedule
- audit warnings
- Excel upload/download

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud

1. Push this folder to GitHub.
2. Create a new Streamlit Community Cloud app.
3. Point it to `app.py`.
4. Upload/edit/download Excel files from the web app.

## Current MVP features

- Editable setup tables using `st.data_editor`
- Generates schedules for all active leagues
- Uses round-robin matchups
- Handles odd team counts with byes
- Pushes blackout dates by one week
- Creates audit warnings for schedule issues
- Exports a finished Excel workbook

## Next features to add

- Ref conflict audit
- Playoff eligibility report
- Approve/Deny review queue
- Playoff bracket generator
- Optional Dash API import later
