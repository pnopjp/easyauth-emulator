# Tools

This directory contains scripts to assist with setting up and operating EasyAuth Emulator.

---

## sign-client-secret-jwt.ps1

A PowerShell script that generates a client secret JWT for Sign in with Apple.

### Background

Apple IDP requires a **JWT signed with ES256** as the client secret, rather than a static string.
Since JWTs have an expiration, they must be regenerated periodically.

This script is based on the procedure described in the [Azure App Service Apple IDP documentation](https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-provider-apple#sign-the-client-secret-jwt), implemented using PowerShell and the .NET SDK. It builds and runs a temporary project to generate the JWT and writes it to a specified file.

### Prerequisites

- .NET SDK 8 or later installed (the `dotnet` command must be available)
- Membership in the Apple Developer Program
- A Services ID with **Sign in with Apple** enabled, and a private key (`.p8` file)

### Required Information

Collect the following from the Apple Developer portal before running the script.

| Information | Where to find it |
| --- | --- |
| **Team ID** | Account → Membership Details |
| **Client ID** | Certificates, Identifiers & Profiles → Identifiers → Services IDs |
| **Key ID / .p8 file** | Certificates, Identifiers & Profiles → Keys (create with Sign in with Apple enabled) |

> Apple downloads the `.p8` file with the name `AuthKey_<KeyId>.p8`. This script automatically extracts the Key ID from that filename, so **do not rename the file**.

### Parameters

| Parameter | Required | Description |
| --- | :---: | --- |
| `-TeamId` | ✓ | Team ID from the Apple Developer Program (e.g., `ABCD123456`) |
| `-ClientId` | ✓ | Client ID registered as a Services ID (e.g., `com.example.app`) |
| `-P8File` | ✓ | Path to the `.p8` private key file. The filename must be in the format `AuthKey_<KeyId>.p8` |
| `-JwtFile` | ✓ | Output file path for the generated JWT (e.g., `client_secret.jwt`) |

### Usage

```powershell
.\tools\sign-client-secret-jwt.ps1 `
    -TeamId "ABCD123456" `
    -ClientId "com.example.myapp" `
    -P8File "AuthKey_ZYXW987654.p8" `
    -JwtFile "client_secret.jwt"
```

Running this command writes the JWT to `client_secret.jwt`.

### config.toml Configuration

Set the contents of the generated JWT file as the value of `IDP_<NAME>_CLIENT_SECRET`.

```toml
IDP_APPLE_CLIENT_SECRET = "<contents of client_secret.jwt>"
```

### Expiration

The generated JWT is valid for **180 days** (the maximum allowed by Apple). Regenerate it before it expires and update `IDP_<NAME>_CLIENT_SECRET` in `config.toml` with the new JWT.
