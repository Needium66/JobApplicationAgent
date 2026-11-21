<#
.SYNOPSIS
  One-time Google Cloud setup for JobPilot Level 2 (no Terraform).

  Creates: APIs, docs bucket, Firestore, Artifact Registry, Pub/Sub event topic + dead-letter topic,
  one service account per component with least-privilege IAM, the Gmail secret placeholder,
  the IAP service agent and (optionally) a Model Armor template for the Defense Mesh.

.EXAMPLE
  .\scripts\level2\setup_level2.ps1 -ProjectId $projectid
  .\scripts\level2\setup_level2.ps1 -ProjectId $projectid -EnableModelArmor
#>
param(
    [string]$ProjectId = $env:PROJECT_ID,
    [string]$Region = $(if ($env:REGION) { $env:REGION } else { "us-central1" }),
    [string]$FirestoreLocation = "nam5",
    [switch]$EnableModelArmor
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")
if (-not $ProjectId) { throw "Pass -ProjectId or set `$env:PROJECT_ID" }

$Bucket = "$ProjectId-job-agent-docs"
Invoke-Gcloud config set project $ProjectId
$ProjectNumber = Get-ProjectNumber $ProjectId
$PubSubAgent = "service-$ProjectNumber@gcp-sa-pubsub.iam.gserviceaccount.com"

Write-Host ">> Enabling APIs" -ForegroundColor Cyan
$apis = @("run", "cloudbuild", "artifactregistry", "firestore", "pubsub", "cloudscheduler", "iap",
          "aiplatform", "secretmanager", "storage", "logging", "cloudtrace", "iam", "iamcredentials", "gmail")
if ($EnableModelArmor) { $apis += "modelarmor" }
Invoke-Gcloud services enable @($apis | ForEach-Object { "$_.googleapis.com" })

Write-Host ">> Storage bucket gs://$Bucket" -ForegroundColor Cyan
if (-not (Test-Gcloud storage buckets describe "gs://$Bucket")) {
    Invoke-Gcloud storage buckets create "gs://$Bucket" "--location=$Region" "--uniform-bucket-level-access" "--public-access-prevention"
}

Write-Host ">> Firestore (shared memory, message board, checkpoints, causal graph)" -ForegroundColor Cyan
if (-not (Test-Gcloud firestore databases describe)) {
    Invoke-Gcloud firestore databases create "--location=$FirestoreLocation" "--type=firestore-native"
}

Write-Host ">> Artifact Registry repository 'jobpilot'" -ForegroundColor Cyan
if (-not (Test-Gcloud artifacts repositories describe jobpilot "--location=$Region")) {
    Invoke-Gcloud artifacts repositories create jobpilot "--repository-format=docker" "--location=$Region" `
        "--description=JobPilot Level 2 images"
}

Write-Host ">> Pub/Sub: event bus + dead-letter queue" -ForegroundColor Cyan
foreach ($t in @("jobpilot-events", "jobpilot-dlq")) {
    if (-not (Test-Gcloud pubsub topics describe $t)) { Invoke-Gcloud pubsub topics create $t }
}
if (-not (Test-Gcloud pubsub subscriptions describe jobpilot-dlq-sub)) {
    Invoke-Gcloud pubsub subscriptions create jobpilot-dlq-sub "--topic=jobpilot-dlq" "--ack-deadline=60" `
        "--message-retention-duration=7d" "--expiration-period=never"
}

Write-Host ">> Service accounts (one identity per component)" -ForegroundColor Cyan
foreach ($short in $JpServiceAccounts.Values) {
    $email = Get-SaEmail $short $ProjectId
    if (-not (Test-Gcloud iam service-accounts describe $email)) {
        Invoke-Gcloud iam service-accounts create "jp-$short" "--display-name=JobPilot $short"
    }
}
Start-Sleep -Seconds 10  # let IAM see new accounts

function Grant-Project([string]$Short, [string[]]$Roles) {
    foreach ($r in $Roles) {
        Invoke-Gcloud projects add-iam-policy-binding $ProjectId "--member=serviceAccount:$(Get-SaEmail $Short $ProjectId)" `
            "--role=$r" "--condition=None" "--quiet" | Out-Null
    }
}

Write-Host ">> Least-privilege IAM" -ForegroundColor Cyan
$base = @("roles/datastore.user", "roles/logging.logWriter", "roles/cloudtrace.agent")
foreach ($k in @("edge", "ingestion", "evaluation", "cover_letter", "application", "healer")) {
    Grant-Project $JpServiceAccounts[$k] $base
}
foreach ($k in $JpAgents) { Grant-Project $JpServiceAccounts[$k] @("roles/aiplatform.user") }
if ($EnableModelArmor) {
    foreach ($k in @("edge", "ingestion", "cover_letter", "application")) { Grant-Project $JpServiceAccounts[$k] @("roles/modelarmor.user") }
}
# Publish to the event bus: edge, the four agents and the healer
foreach ($k in @("edge", "ingestion", "evaluation", "cover_letter", "application", "healer")) {
    Invoke-Gcloud pubsub topics add-iam-policy-binding jobpilot-events `
        "--member=serviceAccount:$(Get-SaEmail $JpServiceAccounts[$k] $ProjectId)" "--role=roles/pubsub.publisher" | Out-Null
}
# Healer drains the dead-letter subscription
Invoke-Gcloud pubsub subscriptions add-iam-policy-binding jobpilot-dlq-sub `
    "--member=serviceAccount:$(Get-SaEmail 'healer' $ProjectId)" "--role=roles/pubsub.subscriber" | Out-Null
# Pub/Sub service agent: forward to the DLQ, and mint push tokens for the push identity
Invoke-Gcloud pubsub topics add-iam-policy-binding jobpilot-dlq "--member=serviceAccount:$PubSubAgent" "--role=roles/pubsub.publisher" | Out-Null
Invoke-Gcloud iam service-accounts add-iam-policy-binding (Get-SaEmail "push" $ProjectId) `
    "--member=serviceAccount:$PubSubAgent" "--role=roles/iam.serviceAccountTokenCreator" | Out-Null
# Bucket access per agent
$bucketRoles = @{ edge = "roles/storage.objectViewer"; ingestion = "roles/storage.objectViewer";
                  cover_letter = "roles/storage.objectAdmin"; application = "roles/storage.objectAdmin" }
foreach ($k in $bucketRoles.Keys) {
    Invoke-Gcloud storage buckets add-iam-policy-binding "gs://$Bucket" `
        "--member=serviceAccount:$(Get-SaEmail $JpServiceAccounts[$k] $ProjectId)" "--role=$($bucketRoles[$k])" | Out-Null
}
# Gmail token secret (value added later, only if you enable email submission); only the application agent can read it
if (-not (Test-Gcloud secrets describe gmail-oauth-token)) {
    Invoke-Gcloud secrets create gmail-oauth-token "--replication-policy=automatic"
}
Invoke-Gcloud secrets add-iam-policy-binding gmail-oauth-token `
    "--member=serviceAccount:$(Get-SaEmail 'application' $ProjectId)" "--role=roles/secretmanager.secretAccessor" | Out-Null
# Cloud Build (gcloud builds submit) uses the default compute service account in new projects
foreach ($r in @("roles/cloudbuild.builds.builder", "roles/artifactregistry.writer")) {
    try {
        Invoke-Gcloud projects add-iam-policy-binding $ProjectId "--member=serviceAccount:$ProjectNumber-compute@developer.gserviceaccount.com" `
            "--role=$r" "--condition=None" "--quiet" | Out-Null
    } catch { Write-Warning "Could not grant $r to the compute service account: $_" }
}

Write-Host ">> IAP service agent" -ForegroundColor Cyan
try { Invoke-Gcloud beta services identity create "--service=iap.googleapis.com" "--project=$ProjectId" "--quiet" | Out-Null }
catch { Write-Warning "Could not create the IAP service agent automatically. It is created the first time you enable IAP in the console." }

if ($EnableModelArmor) {
    Write-Host ">> Model Armor template 'jobpilot-shield' (prompt injection, jailbreak, malicious URLs)" -ForegroundColor Cyan
    $env:CLOUDSDK_API_ENDPOINT_OVERRIDES_MODELARMOR = "https://modelarmor.$Region.rep.googleapis.com/"
    try {
        if (-not (Test-Gcloud model-armor templates describe jobpilot-shield "--location=$Region")) {
            Invoke-Gcloud model-armor templates create jobpilot-shield "--location=$Region" `
                "--pi-and-jailbreak-filter-settings-enforcement=enabled" `
                "--pi-and-jailbreak-filter-settings-confidence-level=medium-and-above" `
                "--malicious-uri-filter-settings-enforcement=enabled"
        }
    } finally { Remove-Item Env:\CLOUDSDK_API_ENDPOINT_OVERRIDES_MODELARMOR -ErrorAction SilentlyContinue }
}

$parentType = Get-GcloudValue projects describe $ProjectId "--format=value(parent.type)"
Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
if (-not $parentType) {
    Write-Host ""
    Write-Host "NOTE: this project has no Google Cloud organization (typical for personal Gmail projects)." -ForegroundColor Yellow
    Write-Host "IAP then needs a one-time OAuth setup before deploy (see README_LEVEL2.md, step 3):" -ForegroundColor Yellow
    Write-Host "  Console > Google Auth Platform > Branding (app name + support email) > Audience: External" -ForegroundColor Yellow
}
Write-Host "Next: .\scripts\level2\deploy_level2.ps1 -ProjectId $ProjectId -Users you@gmail.com"
