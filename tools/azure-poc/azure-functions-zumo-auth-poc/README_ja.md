# Azure Functions版 X-ZUMO-AUTH実機検証PoC

`../azure-zumo-auth-poc`でApp Service上の`X-ZUMO-AUTH`/クライアント主導ログインフローを
実機検証済み(Q1〜Q6確定)。Microsoft自身の技術ブログ([Japan PaaS Support Team
Blog](https://azure.github.io/jpazpaas/2023/10/23/access-to-easyauth-enabled-appservice-or-functions.html))
には、同じ手順がAzure Functionsにも共通して適用されると書かれているが、
`allowedApplications`や`403`の挙動まで一つ一つ実機で確認したわけではない。このPoCは
その裏付けを取るためのもの。

`function_app.py`はAzure Functions Python v2モデルの最小HTTPトリガー
(`GET /api/session`)で、`src/_sample_app_shared.py`の`principal_summary()`を
そのまま呼び出し、App Service版`sample_app.py`の`/api/session`と直接比較できる同じ
形式のJSONを返す。リッチなHTML UIやWebSocket/SSE/HTTP2デモは対象外(今回の検証目的
=ヘッダー注入の一致確認には不要なため)。

`http_auth_level=ANONYMOUS`にして、Functions自身のキー認証機構を完全に無効化している
(Easy Authだけの挙動を切り出して見るため)。

## 状態: 検証中(F1・F2確定)

- **F1(確定、2026-08-22)**: Function Appの`POST /.auth/login/aad`に、このアプリ自身の
  clientId/secretによるクライアントクレデンシャルaccess_tokenを送ったところ`200 OK`で
  `authenticationToken`を含むJSONが返った。App Service(Q1)と同じ挙動
- **F2(確定、2026-08-22)**: `GET /api/session`に`X-ZUMO-AUTH`単独(Cookie無し)で
  アクセスしたところ、期待通りApp Service版`sample_app.py`の`/api/session`と同じ形の
  レスポンスが返った
- **F3(確定、2026-08-22)**: 不正な`X-ZUMO-AUTH: garbage`を送ると`401`になった。
  App Service(Q5)と同じ挙動

## 検証すべき項目

| # | 質問 |
| --- | --- |
| F1 | App Service向けに検証したQ1(クライアント主導ログイン: `POST /.auth/login/aad`に`{"access_token": "..."}`)は、Function Appでも同じ`{"authenticationToken": "...", "user": {"userId": "..."}}`形式で返るか |
| F2 | Function Appの`/api/session`に`X-ZUMO-AUTH`単独でアクセスすると、App Serviceの`sample_app.py`版`/api/session`と同じ形式・同じ内容(`easyauth_headers`のマスク挙動含む)で返るか |
| F3 | 無効な`X-ZUMO-AUTH`を送った場合、App Serviceと同じ`401`になるか |
| F4 | Functionsの`authLevel`(今回は`ANONYMOUS`にしているので無関係)とは別に、Easy Auth自体の`allowedApplications`ポリシーはApp Serviceと同じ挙動(同じclientIdのトークンのみ許可)か |

## 前提条件

- `../azure-zumo-auth-poc`で使ったAADアプリ登録(clientId・tenant ID。以下では
  `<client-id>`/`<tenant-id>`と表記)と、そのクライアントクレデンシャルフローで
  access_tokenを取得する手順が既にある前提(同じ資格情報を再利用する)

## デプロイ

Function App自体はまだ無いので新規作成する。既存のリソースグループを使う。

```powershell
$rg = "<resource-group-name>"  # azure-zumo-auth-pocで使ったものと同じ
$funcAppName = "<new-func-app-name>"
$storageAccount = "<new-storage-account-name>"  # グローバルに一意、小文字・数字のみ
$location = "<location>"  # azure-zumo-auth-pocで使ったものと同じ

az storage account create --resource-group $rg --name $storageAccount --location $location --sku Standard_LRS

az functionapp create --resource-group $rg --name $funcAppName --storage-account $storageAccount `
  --consumption-plan-location $location --runtime python --runtime-version 3.12 `
  --functions-version 4 --os-type Linux
```

コードをデプロイする。`_sample_app_shared.py`はこのフォルダにコピーを置かず、`src/`から
都度コピーする(`src/`が正本。App Service版`sample_app.py`のデプロイ手順と同じ考え方)。

使い捨てのビルド物はリポジトリ直下ではなく、このPoCフォルダの中に作る。`.gitignore`側で
`tools/azure-poc/*/deploy-*/`と`tools/azure-poc/*/*.zip`を既に除外済み。

```powershell
$pocDir = "tools\azure-poc\azure-functions-zumo-auth-poc"

# ビルドを走らせる設定は最初のデプロイより前に設定しておく
# (App Service版デプロイでハマった点と同じ理由: 後から設定しても遡ってビルドされない)
az functionapp config appsettings set --resource-group $rg --name $funcAppName `
  --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true

New-Item -ItemType Directory -Force -Path "$pocDir\deploy-func" | Out-Null
Copy-Item "$pocDir\function_app.py" "$pocDir\deploy-func\"
Copy-Item "$pocDir\host.json" "$pocDir\deploy-func\"
Copy-Item "$pocDir\requirements.txt" "$pocDir\deploy-func\"
Copy-Item src\_sample_app_shared.py "$pocDir\deploy-func\"

Compress-Archive -Path "$pocDir\deploy-func\*" -DestinationPath "$pocDir\func-app.zip" -Force
az functionapp deploy --resource-group $rg --name $funcAppName --src-path "$pocDir\func-app.zip" --type zip
```

デプロイ後、`deploy-func/`と`func-app.zip`はローカルの使い捨てビルド物なので削除してよい
(`.gitignore`で除外済みなので、消さなくても`git status`は汚れない)。

## 認証設定

Azure Portal → この関数アプリ → **認証** → **IDプロバイダーの追加**

- IDプロバイダー: **Microsoft**
- アプリの登録: **既存のアプリ登録の詳細を指定**(新規作成しない)
  - アプリケーション(クライアント)ID: `<client-id>`
  - クライアントシークレット: (App Service版で使ったものと同じ値)
  - 発行者URL: `https://login.microsoftonline.com/<tenant-id>/v2.0`
- 追加のチェック: 既定のままでよい(**今回は同じclientIdのトークンを使うので
  `allowedApplications`が自動的にこのアプリ自身に絞られても問題ない**。App Service初回
  検証時のように別クライアント(az cli等)のトークンを使うわけではないため)
- 認証設定: 既定のまま追加

## テスト

### access_tokenの取得(F1〜F3共通の前提)

このアプリ登録自身をクライアントとするクライアントクレデンシャルフローでトークンを
取得する(`azp`がこのアプリ自身になるため、`allowedApplications`の既定制限にそのまま
合致する)。クライアントクレデンシャルトークンは通常1時間程度で失効するので、期限切れ
になったら再実行すること。

認証設定でクライアントシークレットをPortalに貼った時点で、同じ値が
`MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`としてこのFunction App自身にも保存されている
ので、App Service側を参照しなくてもこのFunction Appから直接取得できる。

```powershell
$rg = "<resource-group-name>"
$funcAppName = "<func-app-name>"
$clientId = "<client-id>"    # ../azure-zumo-auth-poc と同じアプリ登録
$tenantId = "<tenant-id>"

$secret = az functionapp config appsettings list --resource-group $rg --name $funcAppName `
  --query "[?name=='MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'].value" -o tsv

$tokenResponse = Invoke-RestMethod -Method Post `
  -Uri "https://login.microsoftonline.com/$tenantId/oauth2/v2.0/token" `
  -Body @{
    client_id     = $clientId
    client_secret = $secret
    scope         = "api://$clientId/.default"
    grant_type    = "client_credentials"
  }
$accessToken = $tokenResponse.access_token
```

### F1 — クライアント主導ログイン

```powershell
$funcAppName = "<func-app-name>"

curl -i --show-error -X POST https://$funcAppName.azurewebsites.net/.auth/login/aad `
  -H "Content-Type: application/json" `
  -d "{`"access_token`": `"$accessToken`"}"
```

`authenticationToken`を含むJSONが返るか確認する。

### F2 — /api/sessionへのX-ZUMO-AUTHアクセス

```powershell
$funcToken = "<F1で取得したauthenticationToken>"

curl -s https://$funcAppName.azurewebsites.net/api/session -H "X-ZUMO-AUTH: $funcToken"
```

App Service版`sample_app.py`の`/api/session`(このリポジトリの実機検証時の結果、または
ローカルエミュレータでの結果)と比較する。

### F3 — 不正なトークン

```powershell
curl -s -i https://$funcAppName.azurewebsites.net/api/session -H "X-ZUMO-AUTH: garbage"
```

`401`になるか確認する。

## 結果の記録

実施後は上記「状態」欄を実際の回答で更新し、`ToDo.md`に反映すること。

## ローカルで動かす

`function_app.py`は実機Azure Functionsへのデプロイなしに、Azure Functions Core Toolsで
ローカル実行できる。ただし**ローカルのCore ToolsにはEasy Auth(認証)機能が一切無い**
(実機Azureにデプロイした時だけ有効なプラットフォーム機能のため)。`/.auth/login/aad`は
存在せず、`X-ZUMO-AUTH`/`Authorization`ヘッダーの検証も行われない。したがって、
**このリポジトリ自身のEmulatorを「Functions役」のゲートウェイとして前段に置き、
本物のEasy Auth形式のヘッダーを注入させて初めて意味のあるテストになる**
(`../azure-crossorigin-zumo-poc`のC6・C7で実際に検証した構成と同じ考え方)。
次の2つを両方起動する。

### 1. function_app.pyをローカル起動する

```powershell
$pocDir = "tools\azure-poc\azure-functions-zumo-auth-poc"

# デプロイ手順と同じくステージング(.gitignore対象)に集める
New-Item -ItemType Directory -Force -Path "$pocDir\deploy-func" | Out-Null
Copy-Item "$pocDir\function_app.py","$pocDir\host.json","$pocDir\requirements.txt" "$pocDir\deploy-func\"
Copy-Item src\_sample_app_shared.py "$pocDir\deploy-func\"

# local.settings.json(ローカル実行専用の設定。Easy Authは含まれない)
@'
{
  "IsEncrypted": false,
  "Values": { "AzureWebJobsStorage": "", "FUNCTIONS_WORKER_RUNTIME": "python" }
}
'@ | Out-File -Encoding utf8 "$pocDir\deploy-func\local.settings.json"

cd "$pocDir\deploy-func"
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\Activate.ps1
func start
```

`func start`を止める際は、`Ctrl+C`だけでなく、ポート(既定`7071`)がまだ使用中でないか
(`netstat -ano`等で)確認すること。子プロセスが残って次回起動時に
`Port 7071 is unavailable`になることがある。

### 2. Emulatorを組み合わせる

Emulator側の`config.toml`で`APP_UPSTREAM`をこのローカルFunctionsに向ける:

```toml
APP_UPSTREAM = "http://localhost:7071"
```

設定後にEmulatorを起動(または再起動)すれば、実際にAADに
サインインして得た本物のEasy Auth形式のヘッダーが、このローカルの`function_app.py`に
そのまま届く。あとは既存の手順(F1〜F3)と同様に、Emulator側のURLに対して
`X-ZUMO-AUTH`/`Authorization: Bearer`でアクセスして確認する。

## 後片付け

検証後はFunction App・ストレージアカウント(このPoC専用のリソースグループなら
リソースグループ全体)を削除して課金を止めること。
