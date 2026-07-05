# ランタイムガイド

このドキュメントでは、ランタイム設定、互換性の境界、トラブルシューティング情報をまとめます。

## コマンドライン オプション

| オプション | 既定値 | 説明 |
| --- | --- | --- |
| `--app-upstream URL` | — | `APP_UPSTREAM` を上書きする。`config.toml` および環境変数より優先。設定ファイルを編集せずに転送先ポートを変更したい場合に便利。 |
| `--config PATH` | カレントディレクトリの `config.toml` | 設定ファイルのパス。 |
| `--verbose`, `-v` | `false` | 起動時に全設定値を出力する（シークレットはマスク）。`config.toml` の `VERBOSE = true` と同等。 |

## 設定パラメーター

セキュリティ注意:

- `config.toml` にはシークレット（例: `OAUTH2_PROXY_COOKIE_SECRET`、`IDP_<NAME>_CLIENT_SECRET`）が含まれます。`config.toml` を公開・コミットしないでください。

### グローバル設定

| パラメーター | 必須 | 既定値 | 説明 |
| --- | :---: | --- | --- |
| `IDP_LIST` | ✓ | — | 有効にする IdP 名をカンマ区切りで列挙（例: `entra,google`）。順序は選択画面の表示順。 |
| `DEFAULT_IDP` | | — | 未認証時に使用する既定 IdP。`IDP_LIST` に含まれる値を指定。未設定時の挙動は下記参照。 |
| `SITE_URL` | | `http://localhost` | リクエストに `Host` ヘッダーがない場合に使われるフォールバック URL（末尾スラッシュなし）。通常は変更不要。TLS 終端するフロント（トンネルドメインやリバースプロキシ）配下では `https://` の値を設定。 |
| `SITE_PORT` | | `8080` | このゲートウェイのリッスンポート（直接アクセスする場合は公開ポートを兼ねる）。 |
| `APP_UPSTREAM` | | `http://localhost:8081` ※ | 認証済みリクエストの転送先 URL。自分のアプリを使う場合はそのアプリの URL を設定してください。 |
| `DEBUG_HEADERS_ENDPOINT_ENABLED` | | `false` | `GET /.debug/headers` 診断エンドポイントを有効化するか。有効時はその URL でエミュレーターが受け取り計算したヘッダーを確認できる。既定では無効（`404` を返す）。 |
| `SKIP_AUTH_ROUTES` | | — | 認証をスキップしてアップストリームへ直接転送するルート。形式: リクエストパスにマッチする `[METHOD=]REGEX` パターンをカンマ区切りで列挙。例: `GET=^/health$,^/public/`。転送前に認証ヘッダーは除去される。 |
| `IDP_SELECT_ICONS` | | `simple` | `/.auth/login/select` 画面のアイコンスタイル。`simple` — Simple Icons CDN のロゴ。`generic` — 汎用 ID カードアイコン（完全オフライン対応）。`text` — アイコンなし、テキストのみ。 |
| `VERBOSE` | | `false` | 起動時に全設定値を出力するか（シークレットはマスク）。`--verbose` / `-v` CLI フラグと同等。 |

※ `SAMPLE_APP_PORT` を変更している場合、`APP_UPSTREAM` 未設定時の規定値は `http://localhost:<SAMPLE_APP_PORT>` になります。

既定 IdP の選択ルール:

- `DEFAULT_IDP` が設定されている場合、`/.auth/login` はその IdP へリダイレクトします。
- `DEFAULT_IDP` が未設定かつ `IDP_LIST` が1件の場合、その1件を既定 IdP として扱います。
- `DEFAULT_IDP` が未設定かつ `IDP_LIST` が複数件の場合、`/.auth/login` は IdP 選択画面を表示します。
- `IDP_LIST` の順序は、選択画面の表示順にのみ使用されます。

### IdP 個別設定

`IDP_LIST` に含めた各 IdP に対して `IDP_<NAME>_*` を設定します。`<NAME>` は `IDP_LIST` に記載した IdP 名を大文字にしたものです（例: `IDP_LIST` に `myoidc` と書いた場合、キー名は `IDP_MYOIDC_*` になります）。

| パラメーター | 必須 | 既定値 | 説明 |
| --- | :---: | --- | --- |
| `IDP_<NAME>_DISPLAY_NAME` | | IdP 名 | IdP 選択画面に表示する表示名。 |
| `IDP_<NAME>_ICON` | | — | IdP 選択画面に表示するアイコン。[Simple Icons](https://simpleicons.org) のスラッグ（例: `microsoft`）または画像 URL を指定。`IDP_SELECT_ICONS` が `generic` または `text` の場合は無効。 |
| `IDP_<NAME>_KIND` | | IdP 名から推定 | IdP のバックエンド種別。既知の IdP 名は自動検出（`entra` → `microsoft` など）、それ以外は `oidc` が既定。指定可能な値: `microsoft`（Entra ID / Microsoft account）、`google`、`apple`、`facebook`、`github`、`oidc`（エイリアス: `openid-connect`）。 |
| `IDP_<NAME>_CLIENT_ID` | ✓ | — | IdP に登録した OAuth2 / OIDC client ID。 |
| `IDP_<NAME>_CLIENT_SECRET` | ✓ | — | IdP に登録した OAuth2 / OIDC client secret。 |
| `IDP_<NAME>_OIDC_ISSUER_URL` | ✓ ※1 | ※2 | OIDC issuer URL。`microsoft`・`google`・`apple`・`oidc` の KIND で必須。 |
| `IDP_<NAME>_AUTH_PROVIDER` | | KIND から推定 | `/.auth/me` の `identity_provider` フィールドおよび `X-MS-CLIENT-PRINCIPAL-IDP` ヘッダーの値（例: `microsoft` → `aad`）。 |
| `IDP_<NAME>_AUTH_USER_ID_CLAIM` | | KIND から推定 | ユーザー ID として使用する JWT claim 名（例: `microsoft` → `preferred_username`、`google` → `email`）。 |
| `IDP_<NAME>_SCOPES` | | `openid profile email` | リクエストする OAuth2 スコープ（スペース区切り）。委任アクセスシナリオでは追加スコープをここに記載。 |
| `IDP_<NAME>_PROMPT` | | — | 認証リクエストごとに送る OIDC `prompt` パラメーター（`login` / `select_account` / `consent`）。OIDC 以外には無効。 |
| `IDP_<NAME>_CODE_CHALLENGE_METHOD` | | `microsoft`/`google`/`apple`: `S256`、その他: — | PKCE のコードチャレンジ方式（`S256` または `plain`）。`microsoft`・`google`・`apple` はこの設定に関わらず常に `S256` を使用。`oidc` KIND で IdP が PKCE に対応している場合は `S256` を設定。OIDC 以外には無効。 |
| `IDP_<NAME>_LOGOUT_ENDPOINT` | | KIND から導出 | IdP ログアウト URL。`microsoft` KIND は OIDC issuer URL から自動導出。 |
| `IDP_<NAME>_SKIP_CLAIMS_FROM_PROFILE_URL` | | `microsoft`: `true`、その他: `false` | OIDC userinfo からの claim 取得をスキップするか。`true` にすると userinfo レスポンスが ID token の claim を上書きしない。 |
| `IDP_<NAME>_EXTRA_ARGS` | | — | この IDP の oauth2-proxy に追加で渡す起動オプション（スペース区切り）。例: `"--allowed-group=my-group --oidc-extra-audience=myapp"`。指定可能なオプションは [oauth2-proxy 設定リファレンス](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview) を参照。 |

※1 `microsoft`・`google`・`apple`・`oidc` KIND の場合のみ必須。

※2 `IDP_<NAME>_OIDC_ISSUER_URL` の KIND 別既定値:

| KIND | 既定値 |
| --- | --- |
| `microsoft` | — （必須） |
| `google` | `https://accounts.google.com` |
| `apple` | `https://appleid.apple.com` |
| `oidc` | — （必須） |

### GitHub プロバイダーに関する注意

oauth2-proxy の GitHub プロバイダーはセッション作成時に GitHub の `/user/emails` および `/user/orgs` API を呼び出すため、`user:email` と `read:org` スコープが必要です。エミュレーターはこれらをデフォルトスコープとして自動設定します。

**OAuth App：** GitHub Settings → Developer settings → OAuth Apps でアプリを作成します。`IDP_GITHUB_CLIENT_ID` と `IDP_GITHUB_CLIENT_SECRET` を設定するだけで追加の設定は不要です。

**GitHub App：** OAuth App の代わりに GitHub App を使用する場合、ユーザー認可（OAuth）フローは同じ `CLIENT_ID` / `CLIENT_SECRET` フィールドを使いますが、GitHub App の **Permissions & events** ページで以下の権限を付与する必要があります：

| セクション | 権限 | 必要なレベル |
| --- | --- | --- |
| Account permissions | Email addresses | Read-only または Read and write |

この権限がない場合、ブラウザには `500 Internal Server Error` が表示されてログインに失敗します。`OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR = true` を設定すると、詳細なエラー原因として `unexpected status "403": {"message":"Resource not accessible by integration"}` が確認できます。

### Facebook プロバイダーに関する注意

#### email パーミッション

oauth2-proxy の Facebook プロバイダーはセッション作成時に Graph API（`/me?fields=name,email`）を呼び出すため、`email` フィールドが必要です。エミュレーターは `public_profile email` をデフォルトスコープとして自動設定しますが、`email` パーミッションをアプリに明示的に追加する必要があります。**App Dashboard → Permissions and Features** で `email` を見つけて **Add** をクリックしてください。この設定を行わないと、ログインの途中で `Invalid Scopes: email` というメッセージを含む Facebook のエラー画面が表示されてフローが中断されます。`OAUTH2_PROXY_REQUEST_LOGGING = true` を設定すると、ログに記録されるコールバック URL に `error_code=100` が含まれることで原因を確認できます。

#### HTTPS が必須

Facebook Login はリダイレクト URI に HTTPS を要求します。`TLS_CERT_FILE` と `TLS_KEY_FILE` を設定し、`SITE_URL` を `https://` の URL に変更してからテストしてください。ローカル開発では、`SITE_URL` を `https://site.localhost` に設定し、Facebook アプリの有効な OAuth リダイレクト URI に `https://site.localhost:<port>/oauth2/callback` を登録すると便利です。証明書は mkcert で発行できます（後述の「[HTTPS (TLS) を有効にする](#https-tls-を有効にする)」を参照）。

### oauth2-proxy 設定

| パラメーター | 必須 | 既定値 | 説明 |
| --- | :---: | --- | --- |
| `OAUTH2_PROXY_COOKIE_SECRET` | | 自動生成 | oauth2-proxy のセッション cookie 署名シークレット。未設定時は起動時に自動生成して `config.toml` に追記保存される。再起動後も同じ値が使われる。 |
| `OAUTH2_PROXY_COOKIE_SECURE` | | `false` | セッション cookie に `Secure` フラグを付与するか。`TLS_CERT_FILE`/`TLS_KEY_FILE` で HTTPS を有効にした場合は未設定でも自動的に `true` になります。 |
| `OAUTH2_PROXY_PORT_BASE` | | `4180` | 内部 oauth2-proxy インスタンスのベースポート。各 IdP はこの値から連番でポートを使用（例: `4180`、`4181`、…）。 |
| `OAUTH2_PROXY_WHITELIST_DOMAIN` | | `SITE_URL`/`SITE_PORT` から導出 | リダイレクト先として許可するドメイン。 |
| `OAUTH2_PROXY_TRUSTED_PROXY_IP` | | `APP_UPSTREAM` が localhost の場合 `127.0.0.1,::1` | `X-Forwarded-*` ヘッダーを信頼するリバースプロキシの IP アドレスまたは CIDR（カンマ区切り）。`APP_UPSTREAM` が `localhost`・`127.0.0.1`・`[::1]` を指している場合は自動的に `127.0.0.1,::1` を設定。Docker などローカル以外の環境では明示的に指定（例: `172.17.0.0/16`）。 |
| `OAUTH2_PROXY_STANDARD_LOGGING` | | `false` | oauth2-proxy の起動・終了メッセージをターミナルに表示するか。 |
| `OAUTH2_PROXY_AUTH_LOGGING` | | `false` | oauth2-proxy の認証イベントログをターミナルに表示するか。 |
| `OAUTH2_PROXY_REQUEST_LOGGING` | | `false` | oauth2-proxy のリクエストごとの HTTP ログをターミナルに表示するか。 |
| `OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR` | | `false` | OIDC エラー時（client ID・issuer URL の設定ミスなど）に詳細情報を表示するか。開発時に便利。本番環境では非推奨。 |
| `OAUTH2_PROXY_PLATFORM` | | 自動検出 | バイナリダウンロード対象のプラットフォーム。自動検出できない環境のみ設定が必要。指定可能な値: `windows-amd64`、`windows-arm64`、`linux-amd64`、`linux-arm64`、`linux-arm`、`darwin-amd64`、`darwin-arm64`。 |
| `OAUTH2_PROXY_VERSION` | | latest | ダウンロード・維持するバージョンタグ（例: `v7.6.0`）。未設定時は最新安定版（prerelease 除く）。バイナリが存在しバージョンが一致する場合は何もしない。 |
| `OAUTH2_PROXY_AUTO_UPDATE` | | `false` | `true` にすると起動時に自動更新。`false` でも新バージョンがあれば通知。ネットワーク不可の場合はスキップして起動続行。 |

起動時に `bin/oauth2-proxy/oauth2-proxy[.exe]` が存在しない場合、GitHub Releases から自動的にダウンロードします。バイナリが存在する場合は常にバージョンチェックを行い、最新でなければ通知します。

バージョン管理の動作まとめ:

| 状態 | 動作 |
| --- | --- |
| バイナリなし | ダウンロード（`OAUTH2_PROXY_VERSION` 指定時はそのバージョン、未設定時は latest） |
| バイナリあり・バージョン固定・不一致 | 指定バージョンへ更新 |
| バイナリあり・`AUTO_UPDATE = true` | latest（または固定バージョン）と比較し、差異があれば更新 |
| バイナリあり・`AUTO_UPDATE = false`（既定） | チェックのみ実行し、新しいバージョンがあれば通知（更新しない） |
| バージョンチェック時にネットワーク不可 | チェックをスキップして起動続行 |

### ネットワーク / SSL 設定

| パラメーター | 必須 | 既定値 | 説明 |
| --- | :---: | --- | --- |
| `TLS_CERT_FILE` | | — | TLS サーバー証明書（PEM 形式）のパス。`TLS_KEY_FILE` とともに設定すると、エミュレーターが HTTPS でリクエストを受け付けます。 |
| `TLS_KEY_FILE` | | — | TLS 秘密鍵（PEM 形式）のパス。`TLS_CERT_FILE` とともに設定すると、エミュレーターが HTTPS でリクエストを受け付けます。 |
| `SSL_CA_BUNDLE` | | — | カスタム CA 証明書バンドル（PEM 形式）のパス。エミュレーター自身が GitHub へ HTTPS 接続する際（oauth2-proxy のダウンロード）に使用します。通常は不要 — [truststore](https://github.com/sethmlarson/truststore) によって OS の証明書ストア（Windows・macOS・Linux）が自動的に参照されます。社内ネットワークに SSL インスペクション（MITM プロキシ）があり、そのプロキシの CA を OS のストアに追加できない場合（例: Linux でルート権限がない環境）にのみ設定してください。 |

#### HTTPS (TLS) を有効にする

`TLS_CERT_FILE` と `TLS_KEY_FILE` を設定すると、ゲートウェイが HTTPS でリッスンします。ホストには `site.localhost` の使用を推奨します（Facebook Login では必須）。

モダンブラウザは RFC 6761 に従い `*.localhost` を自動的に `127.0.0.1` に解決するため、ブラウザでアクセスする場合は hosts ファイルへの追加は不要です。ブラウザ以外の HTTP クライアントでアクセスする場合は必要になることがあります:

```text
# Windows: C:\Windows\System32\drivers\etc\hosts  /  macOS・Linux: /etc/hosts
127.0.0.1  site.localhost
```

`config.toml` を更新する:

```toml
SITE_URL      = "https://site.localhost"
SITE_PORT     = "8443"
TLS_CERT_FILE = "./server.crt"
TLS_KEY_FILE  = "./server.key"
```

> IdP のアプリ登録（リダイレクト URI）も `https://site.localhost:8443/oauth2/callback` に更新してください。

`OAUTH2_PROXY_COOKIE_SECURE` は TLS 有効時に未設定であれば自動的に `true` になります。

##### 推奨: mkcert による証明書生成

[mkcert](https://github.com/FiloSottile/mkcert) を使うと、OS の証明書ストアに信頼済み CA を登録した開発用証明書を生成できます。ブラウザ警告が出ません。

入手先: [https://github.com/FiloSottile/mkcert](https://github.com/FiloSottile/mkcert)

```sh
mkcert -install  # CA をシステム証明書ストアに登録（初回のみ）
mkcert -cert-file server.crt -key-file server.key site.localhost
```

生成した `server.crt` / `server.key` を `config.toml` で指定したパスに配置してください。

##### 代替: openssl による自己署名証明書

```sh
openssl req -x509 -newkey rsa:4096 -keyout server.key -out server.crt \
  -sha256 -days 365 -nodes -subj "/CN=site.localhost"
```

自己署名証明書はブラウザに警告が表示されます。

### 動作確認用アプリ設定

動作確認用アプリ（`src/sample_app.py`）の設定です。`SAMPLE_APP_ENABLED = true` を指定した場合のみ起動します。

| パラメーター | 必須 | 既定値 | 説明 |
| --- | :---: | --- | --- |
| `SAMPLE_APP_ENABLED` | | `false` | sample_app.py を動作確認用アプリとして起動するか。 |
| `SAMPLE_APP_PORT` | | `8081` | sample_app.py の内部ポート。`APP_UPSTREAM` にこの値を設定するとリクエストを sample_app へ転送できる。 |
| `SAMPLE_APP_STORAGE_BLOB_URL` | | — | 委任ストレージアクセスの検証に使用する Azure Blob Storage URL。形式: `https://<account>.blob.core.windows.net/<container>/<blob>`。 |
| `SAMPLE_APP_OBO_STORAGE_SCOPE` | | `https://storage.azure.com/.default` | ストレージアクセス token リクエスト時に使用する OBO スコープ。 |
| `SAMPLE_APP_STORAGE_TIMEOUT_SECONDS` | | `10` | ストレージリクエストのタイムアウト秒数。 |
| `SAMPLE_APP_STORAGE_PREVIEW_BYTES` | | `4096` | ストレージレスポンスのプレビューバイト数。 |
| `SAMPLE_APP_TITLE` | | `Easy Auth verification app` | sample_app の UI に表示するタイトル。 |
| `SAMPLE_APP_DESCRIPTION` | | — | sample_app の UI に表示する説明。 |

## トラブルシューティング

### `invalid_client` でログインが失敗する

`IDP_<NAME>_CLIENT_ID` と `IDP_<NAME>_CLIENT_SECRET` が IdP のアプリ登録と一致しているか確認してください。

### IdP リダイレクト後にログインが失敗する（`AADSTS50011`）

リダイレクト URI の不一致です。callback URL はブラウザのアドレスバーに表示されている origin に追従します。IdP のアプリ登録（Authentication）のリダイレクト URI を次と一致させてください:

```text
<ブラウザがアクセスしている origin>/oauth2/callback
```

例: `http://localhost:8080/oauth2/callback`、転送ドメイン経由なら `https://xxx-8080.usw2.devtunnels.ms/oauth2/callback`。使用する origin ごとに 1 件ずつ登録してください。

### アプリに到達できない（502 エラー）

`APP_UPSTREAM` が正しく設定されているか、アプリケーションがその URL で起動しているか確認してください。

### oauth2-proxy が HTTP 500 を返す

いくつかの原因が考えられます。

#### 1. クライアントシークレットが間違っている

`IDP_<NAME>_CLIENT_SECRET` に設定する値は、シークレットの**値**であり、シークレットの ID（オブジェクト ID）ではありません。

診断するには、以下のいずれかを有効にしてください:

- **`OAUTH2_PROXY_STANDARD_LOGGING = true`**: Output に詳細なエラーメッセージが出力されます。例:

  ```text
  [oauthproxy.go:928] Error redeeming code during OAuth2 callback: token exchange failed: oauth2: "invalid_client" "AADSTS7000215: Invalid client secret provided. Ensure the secret being sent in the request is the client secret value, not the client secret ID, for a secret added to app '<app-id>'."
  ```

- **`OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR = true`**: ブラウザに 500 エラー画面とエラー詳細が表示されます。例:

  ```text
  500
  Internal Server Error

  token exchange failed: oauth2: "invalid_client" "AADSTS7000215: Invalid client secret provided. Ensure the secret being sent in the request is the client secret value, not the client secret ID, for a secret added to app '<app-id>'."
  ```

### ヘッダーの内容を確認したい

`config.toml` で `DEBUG_HEADERS_ENDPOINT_ENABLED = true` を設定すると、`GET /.debug/headers` エンドポイントでエミュレーターが計算したヘッダーを確認できます。

### oauth2-proxy のログを確認したい

`OAUTH2_PROXY_STANDARD_LOGGING`・`OAUTH2_PROXY_AUTH_LOGGING`・`OAUTH2_PROXY_REQUEST_LOGGING` のいずれか（または複数）を `true` に設定すると、対応するログカテゴリがターミナルに表示されます。

OIDC 設定ミスのエラー詳細を確認したい場合は `OAUTH2_PROXY_SHOW_DEBUG_ON_ERROR = true` を設定してください。

なお、oauth2-proxy が予期せず終了した場合の起動エラーはこれらの設定に関わらず常に表示されます。
