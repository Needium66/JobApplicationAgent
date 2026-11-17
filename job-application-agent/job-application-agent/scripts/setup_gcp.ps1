<#
.SYNOPSIS
  One-time Google Cloud setup for the Level 1 job-application agent.

.EXAMPLE
  .\scripts\setup_gcp.ps1 -ProjectId my-project -Region us-central1

  Works in Windows PowerShell 5.1 and PowerShell 7+.
  If scripts are blocked:  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
param(
    [string]$ProjectId = $env:PROJECT_ID,
    [string]$Region = $(if ($env:REGION) { $env:REGION } else { "us-central1" }),
    [string]$FirestoreLocation = $(if ($env:FIRESTORE_LOCATION) { $env:FIRESTORE_LOCATION } else { "nam5" }),
    [string]$Bucket = $env:DOCS_BUCKET
)

if (-not $ProjectId) { throw "Pass -ProjectId or set `$env:PROJECT_ID" }
if (-not $Bucket) { $Bucket = "$ProjectId-job-agent-docs" }
$SaName = "job-agent-sa"
$SaEmail = "$SaName@$ProjectId.iam.gserviceaccount.com"

# Runs gcloud and stops the script if it fails.
function Invoke-Gcloud {
    & gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

# Runs gcloud quietly and returns $true if it succeeded (used for "does it already exist?" checks).
function Test-Gcloud {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & gcloud @args *> $null } catch { }
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prev
    return $ok
}

# Grants a project-level IAM role (idempotent).
function Grant-ProjectRole([string]$Member, [string]$Role) {
    Invoke-Gcloud projects add-iam-policy-binding $ProjectId "--member=$Member" "--role=$Role" "--condition=None" "--quiet" | Out-Null
}

Invoke-Gcloud config set project $ProjectId
$ProjectNumber = (& gcloud projects describe $ProjectId "--format=value(projectNumber)").Trim()
if (-not $ProjectNumber) { throw "Could not read the project number. Is billing enabled and are you logged in?" }

Write-Host ">> Enabling APIs" -ForegroundColor Cyan
Invoke-Gcloud services enable `
    aiplatform.googleapis.com run.googleapis.com cloudbuild.googleapis.com `
    artifactregistry.googleapis.com firestore.googleapis.com storage.googleapis.com `
    secretmanager.googleapis.com logging.googleapis.com cloudtrace.googleapis.com `
    iam.googleapis.com gmail.googleapis.com

Write-Host ">> Cloud Storage bucket for resumes, tailored resumes, cover letters, ADK artifacts" -ForegroundColor Cyan
if (-not (Test-Gcloud storage buckets describe "gs://$Bucket")) {
    Invoke-Gcloud storage buckets create "gs://$Bucket" "--location=$Region" "--uniform-bucket-level-access"
} else { Write-Host "   bucket already exists" }

Write-Host ">> Firestore (Native) for application tracking" -ForegroundColor Cyan
if (-not (Test-Gcloud firestore databases describe)) {
    Invoke-Gcloud firestore databases create "--location=$FirestoreLocation" "--type=firestore-native"
} else { Write-Host "   Firestore database already exists" }

Write-Host ">> Runtime service account (least privilege)" -ForegroundColor Cyan
if (-not (Test-Gcloud iam service-accounts describe $SaEmail)) {
    Invoke-Gcloud iam service-accounts create $SaName "--display-name=Job application agent runtime"
    Start-Sleep -Seconds 10   # give IAM a moment to see the new account
}
foreach ($role in @("roles/aiplatform.user", "roles/datastore.user", "roles/secretmanager.secretAccessor",
                    "roles/logging.logWriter", "roles/cloudtrace.agent")) {
    Grant-ProjectRole "serviceAccount:$SaEmail" $role
}
Invoke-Gcloud storage buckets add-iam-policy-binding "gs://$Bucket" `
    "--member=serviceAccount:$SaEmail" "--role=roles/storage.objectAdmin" | Out-Null

Write-Host ">> Let Cloud Build (used by 'adk deploy cloud_run') build from source" -ForegroundColor Cyan
$ComputeSa = "serviceAccount:$ProjectNumber-compute@developer.gserviceaccount.com"
foreach ($role in @("roles/cloudbuild.builds.builder", "roles/run.builder")) {
    try { Grant-ProjectRole $ComputeSa $role } catch { Write-Warning "Could not grant $role to the compute SA: $_" }
}

Write-Host ">> Allow yourself to invoke the private Cloud Run service" -ForegroundColor Cyan
$Me = (& gcloud config get-value account).Trim()
Grant-ProjectRole "user:$Me" "roles/run.invoker"

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  Bucket:           gs://$Bucket"
Write-Host "  Service account:  $SaEmail"
Write-Host "Next: put DOCS_BUCKET=$Bucket in job_app_agent\.env, then run the agent locally (README step 6)."
