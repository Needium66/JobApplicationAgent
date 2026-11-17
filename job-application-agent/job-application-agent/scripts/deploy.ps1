<#
.SYNOPSIS
  Deploys the agent to Cloud Run (private, with the ADK web UI).

.EXAMPLE
  .\scripts\deploy.ps1 -ProjectId my-project
  .\scripts\deploy.ps1 -ProjectId my-project -EnableEmail -ApplicantEmail you@example.com

  Run from the project root with the .venv activated (so `adk` is on PATH).
#>
param(
    [string]$ProjectId = $env:PROJECT_ID,
    [string]$Region = $(if ($env:REGION) { $env:REGION } else { "us-central1" }),
    [string]$Bucket = $env:DOCS_BUCKET,
    [string]$Service = "job-app-agent",
    [string]$Model = $(if ($env:GEMINI_MODEL) { $env:GEMINI_MODEL } else { "gemini-3.8-flash" }),
    [switch]$EnableEmail,
    [string]$ApplicantEmail = $env:APPLICANT_EMAIL
)

if (-not $ProjectId) { throw "Pass -ProjectId or set `$env:PROJECT_ID" }
if (-not $Bucket) { $Bucket = "$ProjectId-job-agent-docs" }
if (-not (Get-Command adk -ErrorAction SilentlyContinue)) {
    throw "'adk' not found. Activate the venv first:  .\.venv\Scripts\Activate.ps1"
}
$SaEmail = "job-agent-sa@$ProjectId.iam.gserviceaccount.com"
$EmailOn = if ($EnableEmail) { "true" } else { "false" }

# Run from the project root so ./job_app_agent resolves.
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $adkArgs = @(
        "deploy", "cloud_run",
        "--project=$ProjectId",
        "--region=$Region",
        "--service_name=$Service",
        "--app_name=job_app_agent",
        "--with_ui",
        "--trace_to_cloud",
        "--artifact_service_uri=gs://$Bucket",
        "--env", "GOOGLE_GENAI_USE_ENTERPRISE=1",
        "--env", "GOOGLE_CLOUD_LOCATION=global",
        "--env", "GEMINI_MODEL=$Model",
        "--env", "DOCS_BUCKET=$Bucket",
        "--env", "FIRESTORE_COLLECTION=applications",
        "--env", "APPLY_EMAIL_ENABLED=$EmailOn",
        "--env", "GMAIL_TOKEN_SECRET=gmail-oauth-token",
        "--env", "APPLICANT_EMAIL=$ApplicantEmail",
        # gcloud flags go through --provider-args (avoids PowerShell's handling of a bare "--")
        "--provider-args=--service-account=$SaEmail",
        "--provider-args=--no-allow-unauthenticated",
        "--provider-args=--memory=1Gi",
        "--provider-args=--timeout=600",
        "./job_app_agent"
    )
    Write-Host ">> adk $($adkArgs -join ' ')" -ForegroundColor Cyan
    & adk @adkArgs
    if ($LASTEXITCODE -ne 0) { throw "Deployment failed (exit $LASTEXITCODE)" }

    $url = (& gcloud run services describe $Service "--region=$Region" "--project=$ProjectId" "--format=value(status.url)")
    Write-Host ""
    Write-Host "Deployed: $url" -ForegroundColor Green
    Write-Host "Open the UI:  gcloud run services proxy $Service --region $Region --port 8080   ->  http://localhost:8080"
}
finally {
    Pop-Location
}
