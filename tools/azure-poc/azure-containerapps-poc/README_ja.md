# Azure Container Apps Easy Auth 実機検証PoC

`ToDo.md`に「Container Apps, Functions, Static Web Appsとの動作比較をする」という未着手項目があり、
App ServiceとFunctionsについては既に検証済みだった。Container Appsだけは、zipデプロイではなく
実際のコンテナイメージが必要な点、組み込み認証にApp Serviceのような「簡易設定」がポータルに無い点で
他の2つと構成が異なる。このPoCでは、エミュレータ自身に同梱されている`src/sample_app.py`をそのまま
実際のContainer Appにデプロイし、組み込み認証(Easy Auth)を有効化して実機の挙動を直接比較する。

このフォルダの`Dockerfile`・`__init__.py`・`_sample_app_shared.py`・`sample_app.py`は`src/*`のコピー
(Container Appsはリポジトリを直接実行するのではなくコンテナイメージからビルドするため)。sample_app
側に変更があった場合は`src/`から再コピーすること。

## 状況: 検証完了(発見1はエミュレータとの実際の差異、発見2は一致を確認)

- **発見1(2026-08-25確認済み)**: `unauthenticatedClientAction=RedirectToLoginPage`を設定していても、
  実際に`302`を返すのはリクエストの`User-Agent`ヘッダーに`Mozilla`という文字列が含まれている場合だけ
  だった。それ以外のUser-Agent(curlの既定値、User-Agentヘッダー自体が無い場合、任意の非ブラウザ文字列)
  は`Accept`や`Sec-Fetch-Mode`に関係なく素の`401 Unauthorized`+`WWW-Authenticate: Bearer`になる。
  既存のApp Service PoC(`../azure-zumo-auth-poc`のアプリ、`easyauth-emulator-test-as.azurewebsites.net`)
  でも同じ挙動を確認済みなので、Container Apps固有ではなくEasy Auth全体の一般的な挙動と分かった。
  **このエミュレータはここが実機と異なる**: `_deny_unauthenticated()`(`src/app.py:1312`)は非gRPCリクエスト
  なら`User-Agent`を見ずに常にリダイレクトしている。
- **トークンストア(2026-08-25確認済み)**: マネージドIDベースのblobトークンストアは動作した。
  新規ストレージアカウントは作らず、`../azure-functions-zumo-auth-poc`で使っていた既存の
  `easyauthemulatortestwcus`に新規コンテナ`token-store`を作成し、Container Appの
  システム割り当てIDに`Storage Blob Data Contributor`を付与(`az role assignment list
  --assignee <principalId>`で割り当てを確認済み)した上で、`az containerapp auth update
  --token-store true --blob-container-uri
  https://easyauthemulatortestwcus.blob.core.windows.net/token-store`がSAS URL無しで成功した。
- **発見2(2026-08-27確認済み)**: 認証済みリクエストのヘッダー注入内容はApp Service/Functionsと完全に
  一致した。実際の`AppServiceAuthSession`Cookieを使った`GET /api/session`で、
  `X-MS-CLIENT-PRINCIPAL-NAME`・`X-MS-CLIENT-PRINCIPAL`(マスク済み)・
  `X-MS-CLIENT-PRINCIPAL-ID`(マスク済み)・`X-MS-CLIENT-PRINCIPAL-IDP`・
  `X-MS-TOKEN-AAD-ACCESS-TOKEN`(マスク済み)・`X-MS-TOKEN-AAD-ID-TOKEN`(マスク済み)が返り、
  `../azure-grpc-poc`でgRPCポート経由で確認済みのものと同じ集合だった。`X-Forwarded-User`・
  `X-Forwarded-Email`は未設定で、これも既存の確認内容と一致。通常のブラウザ/Cookieフローでは
  (`../azure-crossorigin-zumo-poc`のC4で判明したクライアント主導`X-ZUMO-AUTH`フローとは異なり)
  実際のid_tokenのクレーム(`email`・`preferred_username`・`groups`・`name`)がサインイン中の
  アカウントの内容通りにすべて反映されており、エミュレータ自身のクレーム形状との差異は無かった。

## デプロイ手順

```powershell
$rg = "easyauth-emulator-test-rg"
$location = "japaneast"
$acrName = "easyauthemulatortestacr"
$envName = "easyauth-emulator-test-env"
$contAppName = "easyauth-emulator-test-ca"
```

### 1. イメージのビルド・push(ACR Tasksによるリモートビルド。ローカルDockerは不要)

```powershell
az acr create --resource-group $rg --name $acrName --sku Basic --admin-enabled true
az acr build --registry $acrName --image easyauth-sample-app:latest tools/azure-poc/azure-containerapps-poc
```

### 2. Container Apps環境とアプリ本体

```powershell
az containerapp env create --resource-group $rg --name $envName --location $location

$acrPassword = az acr credential show -n $acrName --query "passwords[0].value" -o tsv
az containerapp create --resource-group $rg --name $contAppName `
  --environment $envName `
  --image "$acrName.azurecr.io/easyauth-sample-app:latest" `
  --target-port 8081 --ingress external `
  --registry-server "$acrName.azurecr.io" --registry-username $acrName --registry-password $acrPassword `
  --min-replicas 1 --max-replicas 1
```

このPoCのアプリは
`https://easyauth-emulator-test-ca.ambitiousglacier-64e6fc2c.japaneast.azurecontainerapps.io/`
でアクセスできる。

### 3. 認証(Easy Auth)

Container AppsにはApp Serviceのような「簡易設定」がポータルに無いため、プロバイダーの紐付けには
いくつか明示的な手順が必要。新規のapp registrationを作らず、`../azure-zumo-auth-poc`・
`../azure-functions-zumo-auth-poc`・`../azure-crossorigin-zumo-poc`で既に共有している
AAD app registration(`asami-easyauth-test`)を再利用した。

```powershell
# このContainer AppのコールバックURLを既存app registrationのリダイレクトURI一覧に追加
# (このコマンドはリスト全体を置き換えるため、既存の全URI+新規URIをまとめて渡す。
# 事前に `az ad app show --id <client-id>` で現在の一覧を確認しておくこと)
az ad app update --id <client-id> --web-redirect-uris `
  <...既存のURI...> `
  "https://easyauth-emulator-test-ca.ambitiousglacier-64e6fc2c.japaneast.azurecontainerapps.io/.auth/login/aad/callback"
```

プロバイダー本体とクライアントシークレットの追加は**ポータルで実施**した(Container App →
認証 → ID プロバイダーの追加 → Microsoft → 「既存のアプリの登録を選択する」→
`asami-easyauth-test`を選択 → 「新しいクライアント シークレットを作成する」→
未認証リクエストへの操作: 「HTTP 302 リダイレクト」)。理由は、シークレット発行に必要な
`az ad app credential reset`が、このセッションの安全性分類器によって「機密性の高い資格情報発行操作」
としてブロックされたため。ブロックされなければ相当するCLIは以下:

```powershell
az containerapp auth microsoft update -g $rg -n $contAppName `
  --client-id <client-id> --client-secret <secret> `
  --issuer "https://sts.windows.net/<tenant-id>/"
az containerapp auth update -g $rg -n $contAppName --enabled true --action RedirectToLoginPage
```

### 4. トークンストア(動作確認済み。以下は実際に使った値)

Container Appsの認証トークンストアは、SAS URLではなくContainer App自身のマネージドIDで
Blobコンテナに認証してトークンを永続化できる。

```powershell
az containerapp identity assign --resource-group $rg --name $contAppName --system-assigned

# 新規ストレージアカウントは作らず、../azure-functions-zumo-auth-pocで既に使っていた
# ものを再利用(どのストレージアカウントでもよく、新規コンテナがあれば十分)
$storageAccount = "easyauthemulatortestwcus"
$storageContainer = "token-store"
az storage container create --account-name $storageAccount --name $storageContainer --auth-mode login

# トークンストアを有効化する前に、Container Appのマネージドidにコンテナへのアクセス権を
# 付与しておくこと(付与前だと最初の書き込みが権限エラーになる)
$principalId = az containerapp show -g $rg -n $contAppName --query identity.principalId -o tsv
$storageId = az storage account show -g $rg -n $storageAccount --query id -o tsv
az role assignment create --assignee $principalId --role "Storage Blob Data Contributor" --scope $storageId

az containerapp auth update `
  --resource-group $rg `
  --name $contAppName `
  --token-store true `
  --blob-container-uri "https://$storageAccount.blob.core.windows.net/$storageContainer"
```

`--blob-container-uri`はpreview引数。`--blob-container-identity`を省略すると、上で割り当てた
システム割り当てマネージドIDが既定で使われる。

## クリーンアップ

```powershell
az containerapp delete --resource-group $rg --name $contAppName --yes
az containerapp env delete --resource-group $rg --name $envName --yes
az acr delete --resource-group $rg --name $acrName --yes
```

`easyauthemulatortestwcus`内の`token-store`コンテナと、Container Appの IDに付与した
ロール割り当ても削除すること(ストレージアカウント自体は`../azure-functions-zumo-auth-poc`と
共有しているため削除しない)。ポータルで`asami-easyauth-test`に追加したクライアントシークレットは
他のPoCとも共有している資産なので、他で使われていないことを確認できない限り残しておく。
