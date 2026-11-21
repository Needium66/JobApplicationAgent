<#
.SYNOPSIS
  Builds and deploys JobPilot Level 2 (gcloud only):
    jp-edge (IAP)  ->  Pub/Sub jobpilot-events  ->  jp-ingestion / jp-evaluation / jp-cover-letter / jp-application
    jp-healer      <-  Cloud Scheduler (every 5 min) + jobpilot-dlq-sub
  Safe to re-run: existing resources are updated.

.EXAMPLE
  .\scripts\level2\deploy_level2.ps1 -ProjectId $projectid  -Users you@gmail.com
  .\scripts\level2\deploy_level2.ps1 -ProjectId $projectid -SkipBuild -Users you@gmail.com,friend@gmail.com
#>
param(
    [string]$ProjectId = $env:PROJECT_ID,
    [string]$Region = $(if ($env:REGION) { $env:REGION } else { "us-central1" }),
    [string[]]$Users = @(),                     # granted IAP access (roles/iap.httpsResourceAccessor)
    [string[]]$AdminEmails = @(),               # JobPilot admins (see all cases); defaults to -Users[0]
    [string]$GeminiModel = "gemini-3.8-flash",
    [string]$FallbackModel = "gemini-3.5-flash",
    [string]$Tag = (Get-Date -Format "yyyyMMdd-HHmmss"),
    [switch]$SkipBuild,
    [switch]$EnableModelArmor,
    [switch]$EnableEmail,
    [string]$ApplicantEmail = ""
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")
if (-not $ProjectId) { throw "Pass -ProjectId or set `$env:PROJECT_ID" }
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$ProjectNumber = Get-ProjectNumber $ProjectId
$Image = "$Region-docker.pkg.dev/$ProjectId/jobpilot/jobpilot:$Tag"
$Bucket = "$ProjectId-job-agent-docs"
if ($AdminEmails.Count -eq 0 -and $Users.Count -gt 0) { $AdminEmails = @($Users[0]) }
$temp = @()

try {
    # ---------- 1. Build ----------
    if ($SkipBuild) {
        # Reuse the image already running on a specialist service, else the newest image in Artifact Registry.
        $Image = Get-GcloudValue run services describe jp-ingestion "--region=$Region" "--project=$ProjectId" `
            "--format=value(spec.template.spec.containers[0].image)"
        if (-not $Image) {
            $repo = "$Region-docker.pkg.dev/$ProjectId/jobpilot/jobpilot"
            $digest = Get-GcloudValue artifacts docker images list $repo "--project=$ProjectId" "--sort-by=~UPDATE_TIME" `
                "--limit=1" "--format=value(version)"
            if ($digest) { $Image = "$repo@$digest" }
        }
        if (-not $Image) {
            throw ("-SkipBuild found no existing image in project $ProjectId (gcloud said: $LastGcloudError). " +
                   "Run the deploy once without -SkipBuild to build it.")
        }
        Write-Host ">> Reusing image $Image" -ForegroundColor Cyan
    } else {
        Write-Host ">> Building $Image with Cloud Build" -ForegroundColor Cyan
        Push-Location $Root
        try { Invoke-Gcloud builds submit "--config=cloudbuild.level2.yaml" "--substitutions=_IMAGE=$Image" "--project=$ProjectId" "." }
        finally { Pop-Location }
    }

    # ---------- 2. Shared environment ----------
    $common = [ordered]@{
        BACKEND = "gcp"; GOOGLE_GENAI_USE_ENTERPRISE = "1"; GOOGLE_CLOUD_PROJECT = $ProjectId
        GOOGLE_CLOUD_LOCATION = "global"; REGION = $Region
        GEMINI_MODEL = $GeminiModel; GEMINI_FALLBACK_MODEL = $FallbackModel
        DOCS_BUCKET = $Bucket; EVENTS_TOPIC = "jobpilot-events"; DLQ_SUBSCRIPTION = "jobpilot-dlq-sub"
        ADMIN_EMAILS = ($AdminEmails -join ","); LEASE_SECONDS = "300"; MAX_AGENT_ATTEMPTS = "5"
        APPLY_EMAIL_ENABLED = $(if ($EnableEmail) { "true" } else { "false" }); APPLICANT_EMAIL = $ApplicantEmail
        GMAIL_TOKEN_SECRET = "gmail-oauth-token"
        MODEL_ARMOR_TEMPLATE = $(if ($EnableModelArmor) { "projects/$ProjectId/locations/$Region/templates/jobpilot-shield" } else { "" })
    }
    $pushSa = Get-SaEmail "push" $ProjectId
    $schedSa = Get-SaEmail "scheduler" $ProjectId

    function Deploy-Role([string]$Role, [hashtable]$Extra, [string[]]$Flags) {
        $svc = Get-ServiceName $Role
        $vars = [ordered]@{}; foreach ($k in $common.Keys) { $vars[$k] = $common[$k] }
        $vars["SERVICE_ROLE"] = $Role
        foreach ($k in $Extra.Keys) { $vars[$k] = $Extra[$k] }
        $envFile = New-EnvFile $vars; $script:temp += $envFile
        $sa = Get-SaEmail $JpServiceAccounts[$Role] $ProjectId
        Write-Host ">> Deploying $svc" -ForegroundColor Cyan
        Invoke-Gcloud run deploy $svc "--image=$Image" "--region=$Region" "--project=$ProjectId" "--service-account=$sa" `
            "--env-vars-file=$envFile" "--no-allow-unauthenticated" "--memory=1Gi" "--cpu-boost" "--quiet" @Flags
        return (Get-GcloudValue run services describe $svc "--region=$Region" "--format=value(status.url)")
    }

    # ---------- 3. Private services: four specialists + healer ----------
    $urls = @{}
    foreach ($role in $JpAgents) {
        $urls[$role] = Deploy-Role $role @{ PUSH_INVOKER_SA = $pushSa } @("--timeout=900", "--concurrency=2", "--max-instances=5")
        Invoke-Gcloud run services add-iam-policy-binding (Get-ServiceName $role) "--region=$Region" `
            "--member=serviceAccount:$pushSa" "--role=roles/run.invoker" | Out-Null
    }
    $urls["healer"] = Deploy-Role "healer" @{ SCHEDULER_SA = $schedSa } @("--timeout=300", "--concurrency=1", "--max-instances=1")
    Invoke-Gcloud run services add-iam-policy-binding jp-healer "--region=$Region" `
        "--member=serviceAccount:$schedSa" "--role=roles/run.invoker" | Out-Null

    # ---------- 4. Edge behind IAP (replaces an API gateway) ----------
    $aud = "/projects/$ProjectNumber/locations/$Region/services/jp-edge"
    # Deployed private first (no one can reach it yet), then IAP is switched on. --iap is GA in recent gcloud
    # versions but beta-only in older ones, so use whichever this machine supports.
    $edgeUrl = Deploy-Role "edge" @{ AUTH_MODE = "iap"; IAP_AUDIENCE = $aud; RATE_LIMIT_PER_MIN = "30" } `
        @("--timeout=60", "--concurrency=40", "--max-instances=5")
    Write-Host ">> Enabling IAP on jp-edge" -ForegroundColor Cyan
    if (Test-GcloudFlag "run services update" "iap") {
        Invoke-Gcloud run services update jp-edge "--region=$Region" "--project=$ProjectId" "--iap" "--quiet"
    } else {
        Write-Host "   (this gcloud only has --iap in beta; using 'gcloud beta'. 'gcloud components update' removes the need)"
        Invoke-Gcloud beta run services update jp-edge "--region=$Region" "--project=$ProjectId" "--iap" "--quiet"
    }
    Invoke-Gcloud run services add-iam-policy-binding jp-edge "--region=$Region" `
        "--member=serviceAccount:service-$ProjectNumber@gcp-sa-iap.iam.gserviceaccount.com" "--role=roles/run.invoker" | Out-Null
    foreach ($u in $Users) {
        Write-Host "   granting IAP access to $u"
        Invoke-Gcloud iap web add-iam-policy-binding "--resource-type=cloud-run" "--service=jp-edge" "--region=$Region" `
            "--member=user:$u" "--role=roles/iap.httpsResourceAccessor" | Out-Null
    }

    # ---------- 5. Choreography: one filtered push subscription per specialist ----------
    $filters = @{
        ingestion    = 'attributes.type = "case.created"'
        evaluation   = 'attributes.type = "ingestion.completed"'
        cover_letter = 'attributes.type = "evaluation.completed" AND attributes.decision = "APPROVE"'
        application  = 'attributes.type = "application.approved"'
    }
    $pubsubAgent = "service-$ProjectNumber@gcp-sa-pubsub.iam.gserviceaccount.com"
    foreach ($role in $JpAgents) {
        $sub = "$(Get-ServiceName $role)-sub"
        $subArgs = @("--push-endpoint=$($urls[$role])/events", "--push-auth-service-account=$pushSa",
                    "--push-auth-token-audience=$($urls[$role])", "--ack-deadline=600",
                    "--min-retry-delay=10s", "--max-retry-delay=600s",
                    "--dead-letter-topic=jobpilot-dlq", "--max-delivery-attempts=5")
        if (Test-Gcloud pubsub subscriptions describe $sub) {
            Write-Host ">> Updating subscription $sub" -ForegroundColor Cyan
            Invoke-Gcloud pubsub subscriptions update $sub @subArgs
        } else {
            Write-Host ">> Creating subscription $sub  [$($filters[$role])]" -ForegroundColor Cyan
            $ff = New-FlagsFile @{ "message-filter" = $filters[$role] }; $temp += $ff
            Invoke-Gcloud pubsub subscriptions create $sub "--topic=jobpilot-events" "--flags-file=$ff" "--expiration-period=never" @subArgs
        }
        Invoke-Gcloud pubsub subscriptions add-iam-policy-binding $sub "--member=serviceAccount:$pubsubAgent" `
            "--role=roles/pubsub.subscriber" | Out-Null
    }

    # ---------- 6. Auto-healing sweep every 5 minutes ----------
    $jobArgs = @("--location=$Region", "--schedule=*/5 * * * *", "--uri=$($urls['healer'])/heal", "--http-method=POST",
                 "--oidc-service-account-email=$schedSa", "--oidc-token-audience=$($urls['healer'])", "--attempt-deadline=300s")
    if (Test-Gcloud scheduler jobs describe jp-healer "--location=$Region") {
        Invoke-Gcloud scheduler jobs update http jp-healer @jobArgs
    } else {
        Invoke-Gcloud scheduler jobs create http jp-healer @jobArgs
    }

    Write-Host ""
    Write-Host "JobPilot Level 2 deployed." -ForegroundColor Green
    Write-Host "  Open: $edgeUrl   (sign in with an account you passed to -Users)"
    Write-Host "  Image: $Image"
} finally {
    $temp | ForEach-Object { Remove-Item $_ -ErrorAction SilentlyContinue }
}
