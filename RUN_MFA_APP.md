# Run MFA App

This guide explains how to run and test the Multi Time Frame Analysis (MFA) app locally.

## Prerequisites

- Python virtual environment created at `.venv`
- Node.js and `npm` installed
- Backend and frontend code already generated under `workspace/backend` and `workspace/frontend`

## 1. Activate the Virtual Environment

From the project root:

```powershell
.\.venv\Scripts\Activate.ps1
```

## 2. Start the Backend

Open a terminal from the project root and run:

```powershell
cd .\workspace\backend
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

Backend URLs:

- Health check: `http://127.0.0.1:8000/health`
- Swagger docs: `http://127.0.0.1:8000/docs`
- MFA endpoint example: `http://127.0.0.1:8000/mfa?symbol=RELIANCE.NS`

## 3. Start the Frontend

Open a second terminal from the project root and run:

```powershell
cd .\workspace\frontend
npm install
npm run dev
```

Frontend URL:

- `http://localhost:5173`

The frontend uses `VITE_BACKEND_URL` if set. Otherwise it defaults to `http://127.0.0.1:8000`.

## 4. Manual MFA Test

Open `http://localhost:5173` and test with a few NSE symbols:

- `RELIANCE.NS`
- `TCS.NS`
- `INFY.NS`
- `HDFCBANK.NS`

Expected behavior:

- The page shows a symbol input or selection.
- Submitting a valid symbol loads MFA data from the backend.
- The UI renders daily, monthly, and yearly sections.
- The chart renders from backend `chart_data`.
- Loading, error, and no-data states appear when appropriate.

## 5. Backend API Test

From PowerShell:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/mfa?symbol=RELIANCE.NS"
```

Try a few edge cases:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/mfa?symbol=RELIANCE"
Invoke-RestMethod "http://127.0.0.1:8000/mfa?symbol=@@@.NS"
```

Expected results:

- Valid symbol returns MFA JSON.
- Missing `.NS` suffix returns a `400` error.
- Invalid symbol format returns a controlled error response.

## 6. Run Automated Backend Tests

From the project root:

```powershell
python -m pytest workspace/backend/tests
```

## 7. Common Issues

If the frontend is blank:

- Check the browser console for JavaScript errors.
- Confirm the backend is running on `http://127.0.0.1:8000`.
- Confirm the frontend terminal shows Vite is serving on `http://localhost:5173`.

If the frontend cannot call the backend:

- Make sure `workspace/backend/main.py` is running.
- Check that CORS is enabled for `http://localhost:5173`.

If a symbol fails:

- Use NSE symbols with the `.NS` suffix.
- Try one of the example symbols first.

## 8. Stop the App

- In the backend terminal, press `Ctrl+C`
- In the frontend terminal, press `Ctrl+C`
