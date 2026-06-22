<#
.SYNOPSIS
    Generates an Apple IDP client secret JWT.

.DESCRIPTION
    Generates a client secret JWT for Apple Sign In and saves it to the specified file.
    Requires the .NET SDK.

.PARAMETER TeamId
    Apple Developer Program Team ID.

.PARAMETER ClientId
    Apple Services ID (client ID).

.PARAMETER P8File
    Path to the .p8 private key file. The filename must follow the pattern AuthKey_<KeyId>.p8.

.PARAMETER JwtFile
    Output file path for the generated JWT (*.jwt).

.EXAMPLE
    .\sign-client-secret-jwt.ps1 -TeamId "XXXXXXXXXX" -ClientId "com.example.app" `
        -P8File ".cert\AuthKey_7XB436U268.p8" -JwtFile "client_secret.jwt"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$TeamId,

    [Parameter(Mandatory=$true, Position=1)]
    [string]$ClientId,

    [Parameter(Mandatory=$true, Position=2)]
    [string]$P8File,

    [Parameter(Mandatory=$true, Position=3)]
    [string]$JwtFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Check .NET SDK
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    Write-Error ".NET SDK not found. Please install it from https://dot.net"
    exit 1
}

# Resolve .p8 file path
$P8File = Resolve-Path $P8File | Select-Object -ExpandProperty Path

# Extract KeyId from filename (AuthKey_<KeyId>.p8)
$p8BaseName = [System.IO.Path]::GetFileNameWithoutExtension($P8File)
if ($p8BaseName -notmatch '^AuthKey_(.+)$') {
    Write-Error ".p8 filename must follow the pattern 'AuthKey_<KeyId>.p8'. Got: '$([System.IO.Path]::GetFileName($P8File))'"
    exit 1
}
$KeyId = $Matches[1]
Write-Verbose "KeyId: $KeyId"

# Read .p8 file and strip PEM header/footer lines, then remove all whitespace
$p8Base64 = ((Get-Content -Path $P8File) -join '') -replace '-----[^-]+-----', '' -replace '\s', ''

# Resolve output file to an absolute path (file may not exist yet)
if (-not [System.IO.Path]::IsPathRooted($JwtFile)) {
    $JwtFile = Join-Path $PWD $JwtFile
}
$jwtDir = [System.IO.Path]::GetDirectoryName($JwtFile)
if ($jwtDir -and -not (Test-Path $jwtDir)) {
    New-Item -ItemType Directory -Path $jwtDir | Out-Null
}

# Create temp project directory
$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) "sign-jwt-$([System.Guid]::NewGuid())"
New-Item -ItemType Directory -Path $tempDir | Out-Null
Write-Verbose "Temp directory: $tempDir"

try {
    # Detect target framework from installed .NET SDK version
    $dotnetVersion = (& dotnet --version)
    if ($dotnetVersion -match '^(\d+)\.') {
        $targetFramework = "net$($Matches[1]).0"
    } else {
        $targetFramework = "net8.0"
    }
    Write-Verbose "Target framework: $targetFramework"

    # Create .csproj
    $csprojPath = Join-Path $tempDir "sign-jwt.csproj"
    @"
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>$targetFramework</TargetFramework>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>enable</Nullable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.IdentityModel.Tokens" Version="8.*" />
    <PackageReference Include="System.IdentityModel.Tokens.Jwt" Version="8.*" />
  </ItemGroup>
</Project>
"@ | Set-Content -Path $csprojPath -Encoding utf8NoBOM

    # C# function body embedded directly (single-quoted to prevent PowerShell interpolation)
    $csFunctionOnly = @'
public static string GetAppleClientSecret(string teamId, string clientId, string keyId, string p8key)
{
    string audience = "https://appleid.apple.com";

    string issuer = teamId;
    string subject = clientId;
    string kid = keyId;

    IList<Claim> claims = new List<Claim> {
        new Claim ("sub", subject)
    };

    CngKey cngKey = CngKey.Import(Convert.FromBase64String(p8key), CngKeyBlobFormat.Pkcs8PrivateBlob);

    SigningCredentials signingCred = new SigningCredentials(
        new ECDsaSecurityKey(new ECDsaCng(cngKey)),
        SecurityAlgorithms.EcdsaSha256
    );

    JwtSecurityToken token = new JwtSecurityToken(
        issuer,
        audience,
        claims,
        DateTime.Now,
        DateTime.Now.AddDays(180),
        signingCred
    );
    token.Header.Add("kid", kid);
    token.Header.Remove("typ");

    JwtSecurityTokenHandler tokenHandler = new JwtSecurityTokenHandler();

    return tokenHandler.WriteToken(token);
}
'@

    @"
using System;
using System.Collections.Generic;
using System.Security.Claims;
using System.Security.Cryptography;
using Microsoft.IdentityModel.Tokens;
using System.IdentityModel.Tokens.Jwt;

public static class AppleAuth
{
$csFunctionOnly
}
"@ | Set-Content -Path (Join-Path $tempDir "AppleAuth.cs") -Encoding utf8NoBOM

    # Create entry point Program.cs (sensitive data passed via environment variables)
    @"
using System;

var jwt = AppleAuth.GetAppleClientSecret(
    Environment.GetEnvironmentVariable("APPLE_TEAM_ID")!,
    Environment.GetEnvironmentVariable("APPLE_CLIENT_ID")!,
    Environment.GetEnvironmentVariable("APPLE_KEY_ID")!,
    Environment.GetEnvironmentVariable("APPLE_P8_KEY")!
);
Console.Write(jwt);
"@ | Set-Content -Path (Join-Path $tempDir "Program.cs") -Encoding utf8NoBOM

    # Build (capture stderr too; display on failure)
    Write-Host "Building..."
    $buildOutput = & dotnet build $csprojPath --verbosity minimal 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "--- Build errors ---" -ForegroundColor Red
        $buildOutput | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
        Write-Error "Build failed. See errors above."
        exit 1
    }

    # Pass sensitive data via environment variables
    $env:APPLE_TEAM_ID   = $TeamId
    $env:APPLE_CLIENT_ID = $ClientId
    $env:APPLE_KEY_ID    = $KeyId
    $env:APPLE_P8_KEY    = $p8Base64

    try {
        # Run and capture stdout (JWT) separately from stderr
        Write-Host "Generating JWT..."
        $stdoutFile = Join-Path $tempDir "stdout.txt"
        $stderrFile = Join-Path $tempDir "stderr.txt"
        $proc = Start-Process dotnet `
            -ArgumentList "run --project `"$csprojPath`" --no-build" `
            -RedirectStandardOutput $stdoutFile `
            -RedirectStandardError  $stderrFile `
            -Wait -PassThru -NoNewWindow

        if ($proc.ExitCode -ne 0) {
            $errText = Get-Content $stderrFile -Raw -ErrorAction SilentlyContinue
            Write-Error "JWT generation failed. $errText"
            exit 1
        }
        $jwt = (Get-Content $stdoutFile -Raw).TrimEnd()
    } finally {
        # Clear sensitive environment variables
        Remove-Item Env:\APPLE_TEAM_ID   -ErrorAction SilentlyContinue
        Remove-Item Env:\APPLE_CLIENT_ID -ErrorAction SilentlyContinue
        Remove-Item Env:\APPLE_KEY_ID    -ErrorAction SilentlyContinue
        Remove-Item Env:\APPLE_P8_KEY    -ErrorAction SilentlyContinue
    }

    # Write JWT to output file (no trailing newline)
    [System.IO.File]::WriteAllText($JwtFile, $jwt)
    Write-Host "Client secret JWT saved to: $JwtFile"

} finally {
    # Remove temp directory
    Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
}
