$ErrorActionPreference = 'Stop'

$containerId = $null
$savedDatabaseUrlSync = $env:DATABASE_URL_SYNC
$savedTestUrl = $env:AIK_TEST_POSTGRES_URL
$savedAdminUrl = $env:AIK_TEST_POSTGRES_ADMIN_URL

try {
    $suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $name = "aik-0008-verify-$suffix"
    $containerId = docker run -d --rm --name $name `
        -e POSTGRES_HOST_AUTH_METHOD=trust `
        -e POSTGRES_DB=aik_verify `
        -p 127.0.0.1::5432 `
        --tmpfs /var/lib/postgresql/data:rw,size=512m `
        postgres:16-alpine
    if ($LASTEXITCODE -ne 0 -or -not $containerId) {
        throw 'Could not start the disposable PostgreSQL 16 container.'
    }

    $mapping = docker port $containerId 5432/tcp
    if ($LASTEXITCODE -ne 0 -or $mapping -notmatch '^127\.0\.0\.1:(\d+)$') {
        throw 'The disposable database did not bind to a loopback-only port.'
    }
    $port = $Matches[1]

    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        docker exec $containerId pg_isready -U postgres -d aik_verify *> $null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        throw 'Disposable PostgreSQL did not become ready within 60 seconds.'
    }

    $base = "postgresql://postgres@127.0.0.1:$port"
    $env:AIK_TEST_POSTGRES_ADMIN_URL = "$base/postgres"
    $env:AIK_TEST_POSTGRES_URL = "$base/aik_verify"
    python -m pytest -q -s tests/postgres
    if ($LASTEXITCODE -ne 0) {
        throw 'PostgreSQL migration/trigger verification failed.'
    }
}
finally {
    if ($null -ne $savedDatabaseUrlSync) { $env:DATABASE_URL_SYNC = $savedDatabaseUrlSync }
    else { Remove-Item Env:DATABASE_URL_SYNC -ErrorAction SilentlyContinue }
    if ($null -ne $savedTestUrl) { $env:AIK_TEST_POSTGRES_URL = $savedTestUrl }
    else { Remove-Item Env:AIK_TEST_POSTGRES_URL -ErrorAction SilentlyContinue }
    if ($null -ne $savedAdminUrl) { $env:AIK_TEST_POSTGRES_ADMIN_URL = $savedAdminUrl }
    else { Remove-Item Env:AIK_TEST_POSTGRES_ADMIN_URL -ErrorAction SilentlyContinue }
    if ($containerId) {
        docker stop $containerId *> $null
    }
}
