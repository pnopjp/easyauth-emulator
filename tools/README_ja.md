# ツール

このディレクトリには、EasyAuth Emulator のセットアップや運用を補助するスクリプトが含まれます。

---

## sign-client-secret-jwt.ps1

Apple Sign In with Apple の client secret JWT を生成する PowerShell スクリプトです。

### 背景

Apple IDP では、client secret として**静的な文字列ではなく JWT**（ES256 署名付き）を使用します。
JWT は有効期限があるため、定期的に再生成が必要です。

このスクリプトは [Azure App Service の Apple IDP 設定ドキュメント](https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-provider-apple#sign-the-client-secret-jwt) に記載された手順を基に、PowerShell + .NET SDK で実装しています。一時プロジェクトをビルド・実行して JWT を生成し、指定ファイルに書き出します。

### 前提条件

- .NET SDK 8 以降がインストールされていること（`dotnet` コマンドが使える状態）
- Apple Developer Program に加入していること
- **Sign In with Apple** が有効な Services ID と秘密鍵（`.p8` ファイル）を取得済みであること

### 必要な情報の取得

Apple Developer ポータルで以下の情報を事前に確認してください。

| 情報 | 取得場所 |
| --- | --- |
| **Team ID** | Account → Membership Details |
| **Client ID** | Certificates, Identifiers & Profiles → Identifiers → Services IDs |
| **Key ID / .p8 ファイル** | Certificates, Identifiers & Profiles → Keys（Sign In with Apple を有効にして作成） |

> `.p8` ファイルは Apple が `AuthKey_<KeyId>.p8` という名前でダウンロードさせます。このスクリプトはそのファイル名から Key ID を自動的に取得するため、**ファイル名を変更しないでください**。

### パラメーター

| パラメーター | 必須 | 説明 |
| --- | :---: | --- |
| `-TeamId` | ✓ | Apple Developer Program の Team ID（例: `ABCD123456`） |
| `-ClientId` | ✓ | Services ID として登録した client ID（例: `com.example.app`） |
| `-P8File` | ✓ | `.p8` 秘密鍵ファイルのパス。ファイル名は `AuthKey_<KeyId>.p8` 形式であること |
| `-JwtFile` | ✓ | 生成した JWT の出力先ファイルパス（例: `client_secret.jwt`） |

### 使用例

```powershell
.\tools\sign-client-secret-jwt.ps1 `
    -TeamId "ABCD123456" `
    -ClientId "com.example.myapp" `
    -P8File "AuthKey_ZYXW987654.p8" `
    -JwtFile "client_secret.jwt"
```

実行すると `client_secret.jwt` に JWT が書き出されます。

### config.toml への設定

生成した JWT ファイルの中身を `IDP_<NAME>_CLIENT_SECRET` に設定します。

```toml
IDP_APPLE_CLIENT_SECRET = "<client_secret.jwt の内容>"
```

### 有効期限

生成される JWT の有効期間は **180 日**です（Apple が定める上限）。有効期限が切れる前に再生成し、`config.toml` の `IDP_<NAME>_CLIENT_SECRET` を新しい JWT の内容に更新してください。
