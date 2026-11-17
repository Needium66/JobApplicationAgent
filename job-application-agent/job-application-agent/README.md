# JobPilot — Level 1 Job Application Agent (Gemini + Google ADK + Google Cloud)

A single, monolithic, centrally managed agent that:

1. **Ingests** a resume (paste, file upload in the ADK UI, or `gs://` URI)
2. **Ingests** a job description (paste or URL)
3. **Evaluates** skills, experience, certifications, education, location/eligibility, and dates
4. **Scores** confidence 0–100 → **≥ 80 = APPROVE**, **< 80 = DENY** (enforced in code)
5. *(optional)* **Tailors** the resume truthfully for the role
6. **Generates** a role-specific cover letter (DOCX + Markdown in Cloud Storage)
7. **Applies** by email (Gmail API) or logs a portal hand-off packet. It always asks you to confirm first.

Built with **FCoT** using its two primary elements:

| FCoT element | Where | What it does |
|---|---|---|
| **Instruction Contract (IC)** | `job_app_agent/contract.py` | Tools, agent instructions, mission, deliverables, success criteria, hard constraints, safety policy, all typed in one place and rendered into the system instruction |
| **Recursive Loop, N = 3** | `job_app_agent/tools/fcot.py` + `guards.py` | Each stage (`evaluation`, `tailoring`, `cover_letter`) runs 3 iterations of **Recap → Reason → Verify**. The tool rejects out-of-order iterations. The guard blocks the stage's deliverable until iteration 3 is recorded as verified. |

Hard constraints are enforced **in code**, so the agent cannot skip them:
- `score_resume_match` is blocked until the evaluation loop completes
- the score/decision is computed completely (weights below); an unmet must-have caps the score at 79
- `submit_application` is blocked on DENY, blocked without a cover letter, and pauses for **human confirmation** (`FunctionTool(require_confirmation=True)`)
- URL fetching refuses private/metadata IPs (SSRF protection on Cloud Run)

**Scoring weights:** skills 35 · experience 25 · certifications 10 · education 10 · location/eligibility 10 · recency/dates 10. Any dimension the posting doesn't mention (`-1`) is dropped and the remaining weights are renormalised.

```
job-application-agent/
├── job_app_agent/            ← the ADK agent folder (this is what gets deployed)
│   ├── __init__.py
│   ├── agent.py              ← root_agent (single LlmAgent)
│   ├── contract.py           ← FCoT Instruction Contract
│   ├── guards.py             ← before_tool_callback: hard constraints in code
│   ├── config.py / state.py
│   ├── requirements.txt
│   ├── .env.example
│   └── tools/
│       ├── ingest.py         ← resume + JD ingestion (paste / GCS / URL)
│       ├── fcot.py           ← Recursive Loop state machine (N=3)
│       ├── scoring.py        ← date analysis + deterministic scoring
│       ├── documents.py      ← DOCX/MD → Cloud Storage + ADK artifacts
│       └── application.py    ← Gmail submit / portal hand-off → Firestore
├── scripts/
│   ├── setup_gcp.ps1         ← APIs, bucket, Firestore, service account, IAM (PowerShell)
│   ├── deploy.ps1            ← adk deploy cloud_run, private, with UI (PowerShell)
│   ├── setup_gcp.sh / deploy.sh  ← bash equivalents
│   └── create_gmail_token.py ← optional, for email submission
└── tests/                    ← 12 offline tests (no Gemini/GCP calls)
```

---

## Step-by-step: from the VS Code terminal (PowerShell) to Google Cloud

> All commands are **PowerShell** (Windows PowerShell 5.1 or PowerShell 7).

### Step 0: Prerequisites (one time)
- A Google Cloud project **with billing enabled**
- VS Code, **Python 3.11+** (from python.org, with "Add to PATH" ticked), Git
- **Google Cloud CLI for Windows**: https://cloud.google.com/sdk/docs/install. Then check with `gcloud --version` in a *new* terminal.

### Step 1: Open the project and create a virtual environment
In VS Code use **File → Open Folder → `job-application-agent`** and open a terminal with

```powershell
# Allow local scripts in this terminal session only (needed for Activate.ps1 and the .ps1 scripts)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r job_app_agent\requirements.txt pytest
adk --version                      # should print 2.14.1
```

### Step 2: Authenticate to Google Cloud
```powershell
$env:PROJECT_ID = "your-project-id"
$env:REGION     = "us-central1"

gcloud auth login
gcloud config set project $env:PROJECT_ID
gcloud auth application-default login                            # credentials your local code uses
gcloud auth application-default set-quota-project $env:PROJECT_ID
```
`$env:` variables last only for the current terminal. Re-run the first two lines if you open a new one.

### Step 3: Provision the Google Cloud infrastructure
```powershell
.\scripts\setup_gcp.ps1 -ProjectId $env:PROJECT_ID -Region $env:REGION
```
This enables Vertex AI (Gemini), Cloud Run, Cloud Build, Artifact Registry, Firestore, Cloud Storage, Secret Manager, Logging, Trace and Gmail APIs. It then creates:
- bucket `gs://<project-id>-job-agent-docs` (documents + ADK artifacts)
- Firestore Native database (application records)
- service account `job-agent-sa` with least-privilege roles
- Cloud Build permissions that `adk deploy` needs

It's safe to re-run; anything that already exists is skipped.

### Step 4: Configure the agent
```powershell
Copy-Item job_app_agent\.env.example job_app_agent\.env
```
Set `GOOGLE_CLOUD_PROJECT` and `DOCS_BUCKET=<project-id>-job-agent-docs`.
To work fully offline (local files instead of GCS/Firestore), set `JOB_AGENT_LOCAL_ONLY=true` and leave `DOCS_BUCKET` empty. Gemini still needs Google Cloud.

> **Model note:** `gemini-2.5-*` models were retired on Vertex AI on 20 Oct 2026, so the default is `gemini-3.8-flash` on the `global` endpoint. If you get a 404 "model not found", check the current IDs on Google's *Model versions* page and change `GEMINI_MODEL`.

### Step 5: Run the tests
```powershell
pytest -q
```

### Step 6: Run the agent locally
Run this from the **project root** (the parent of `job_app_agent\`):
```powershell
adk web            # open http://localhost:8000 and pick "job_app_agent"
# or, terminal only:
adk run job_app_agent
```

### Step 7: Try the workflow
1. *"Here's my resume:"* then paste it, or attach the PDF with the paperclip (Gemini reads it and calls `ingest_resume_text`)
2. *"Evaluate it for this job: https://…"*, or paste the job description
3. Watch the **Events** tab: you'll see `fcot_record_iteration` ×3 (Recap/Reason/Verify), then `score_resume_match`
4. On APPROVE: *"Tailor my resume and write the cover letter"*. Each stage runs its own 3-iteration loop.
5. *"Apply via the portal"*. ADK shows a **confirmation prompt** before `submit_application` runs.

Documents go to `gs://<bucket>/<user>/<session>/<company>-<title>/…` (or `.\local_output\` offline). Application records go to Firestore → `applications`.

### Step 8: Deploy to Cloud Run
```powershell
.\scripts\deploy.ps1 -ProjectId $env:PROJECT_ID -Region $env:REGION
```
This runs `adk deploy cloud_run`, which packages the agent, builds it with Cloud Build, pushes it to Artifact Registry, and deploys a **private** Cloud Run service `job-app-agent`. The service runs as `job-agent-sa`, uses GCS for ADK artifacts, sends traces to Cloud Trace, and includes the ADK web UI.

### Step 9: Use the deployed agent
Because the service is private, open it through an authenticated local proxy:
```powershell
gcloud run services proxy job-app-agent --region $env:REGION --port 8080
# open http://localhost:8080   (Ctrl+C to stop the proxy)
```
Or call the API directly:
```powershell
$url     = gcloud run services describe job-app-agent --region $env:REGION --format="value(status.url)"
$headers = @{ Authorization = "Bearer $(gcloud auth print-identity-token)" }

Invoke-RestMethod "$url/list-apps" -Headers $headers
Invoke-RestMethod "$url/apps/job_app_agent/users/ola/sessions/s1" -Method Post -Headers $headers

$body = @{
  app_name    = "job_app_agent"; user_id = "ola"; session_id = "s1"
  new_message = @{ role = "user"; parts = @(@{ text = "Hi, I want to evaluate my resume for a role." }) }
} | ConvertTo-Json -Depth 6
$events = Invoke-RestMethod "$url/run" -Method Post -Headers $headers -ContentType "application/json" -Body $body
$events | ForEach-Object { $_.content.parts.text } | Where-Object { $_ }
```

### Step 10 (optional): Enable email submission via Gmail
```powershell
pip install google-auth-oauthlib
# Console → Google Auth Platform → Clients → Create "Desktop app" → download as client_secret.json into the project root
python scripts\create_gmail_token.py                 # browser consent, gmail.send scope only
gcloud secrets create gmail-oauth-token --data-file=token.json
Remove-Item token.json
.\scripts\deploy.ps1 -ProjectId $env:PROJECT_ID -EnableEmail -ApplicantEmail "you@example.com"
```
While the OAuth app is in *Testing* mode, add your own Google account as a test user.

### Step 11: Observe, update, clean up
```powershell
gcloud run services logs read job-app-agent --region $env:REGION --limit 100   # logs
# Traces: Console → Trace Explorer · Records: Console → Firestore → applications
.\scripts\deploy.ps1 -ProjectId $env:PROJECT_ID                               # redeploy after code changes
gcloud run services delete job-app-agent --region $env:REGION                 # teardown
```



---
