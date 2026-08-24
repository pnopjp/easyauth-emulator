# Azure X-ZUMO-AUTHヘッダ実機検証用PoC

`X-ZUMO-AUTH`はこのエミュレータには一切実装されていません。実装前に、本物のAzure App
Service Easy Authが実際どう振る舞うのかを確認する必要があります。公式ドキュメントには
「非ブラウザクライアント向けに`AppServiceAuthSession`Cookieの代わりに使える」という程度の
言及しかなく、具体的な仕組みは書かれていません。

このバックエンドは受け取ったヘッダーをそのままJSONで返すだけのものです。認証方法の違い
（Cookieのみ、`X-ZUMO-AUTH`のみ、両方、どちらもなし）によってApp Service側のゲートキーパー
が何を注入するか（またはしないか）を、そのまま観察できます。

## 状態: 検証完了（Q1〜Q6確定、Q7はスキップ）

- **Q1(確定、2026-08-21)**: `POST /.auth/login/aad`のボディは`id_token`ではなく
  `access_token`フィールドが必須（`400 'access_token' field is required.`で発覚）。
  さらに、Easy Auth側の`defaultAuthorizationPolicy.allowedApplications`が既定でこの
  アプリ自身のclientIdに絞られているため、`access_token`の`azp`/`appid`クレームが
  そのclientIdと一致しないトークン（az cliやMSAL等、別クライアントが取得したもの）は
  `403`で拒否される。**このアプリ自身のclientId/secretによるクライアントクレデンシャル
  フロー**（`azp`=自分自身）で取得したaccess_tokenを送ったところ`200 OK`で
  `{"authenticationToken": "<472文字の不透明トークン>", "user": {"userId": "..."}}`
  が返った
- **Q2(確定、2026-08-21)**: 保護ルートへ`X-ZUMO-AUTH: <authenticationToken>`のみ
  （Cookie無し）で`200 OK`になり、バックエンドに`X-MS-CLIENT-PRINCIPAL-ID`・
  `X-MS-CLIENT-PRINCIPAL-IDP`(`aad`)・`X-MS-CLIENT-PRINCIPAL`・
  `X-MS-TOKEN-AAD-ACCESS-TOKEN`が注入されて届いた（今回はapp-onlyトークンのため
  `X-Forwarded-User`/`X-MS-CLIENT-PRINCIPAL-NAME`は無し。ユーザーのdelegatedトークン
  でも同様か、それぞれのクレームの由来までは未検証）
- **Q3(確定、2026-08-21)**: `GET /.auth/me`も`X-ZUMO-AUTH`単独で認識され、`200 OK`で
  通常の`/.auth/me`と同形式（配列に`access_token`・`provider_name`・`user_claims`・
  `user_id`を含む）のレスポンスが返った
- **Q5(確定、2026-08-21)**: 不正な値(`X-ZUMO-AUTH: garbage`)を送ると`401 Unauthorized`
  になった。未認証時の動作設定(`unauthenticatedClientAction: RedirectToLoginPage`)が
  効いている通常のCookie未認証時とは異なり、`X-ZUMO-AUTH`が不正な場合はリダイレクトせず
  常に`401`を直接返すと見られる
- **Q4(確定、2026-08-21)**: ブラウザログイン後の`AppServiceAuthSession`Cookieの値を
  そのまま`X-ZUMO-AUTH`ヘッダに転記して送ると`401 Unauthorized`(`WWW-Authenticate:
  Bearer ...`付き)になった。**`authenticationToken`とセッションCookieの値は相互運用
  不可**（別形式のトークンとして扱われる）
- **Q6(確定、2026-08-21)**: ブラウザでログイン済みの実ユーザーの`Cookie`と、
  app-onlyな`X-ZUMO-AUTH`(別アイデンティティ)を同じリクエストに両方付けたところ、
  届いた`X-MS-CLIENT-PRINCIPAL-ID`はQ2の`X-ZUMO-AUTH`単独テストと同じ値だった。
  **両方存在する場合は`X-ZUMO-AUTH`側のアイデンティティが優先される**
- **Q7: スキップ（2026-08-21）**。Q5で不正な`X-ZUMO-AUTH`が既定の302リダイレクト設定
  を無視して401を返すことが確認済みのため、設定を401に変えても結果は変わらないと
  推測されるが、実際には未確認

## 検証すべき項目

| # | 質問 |
| --- | --- |
| Q1 | クライアント主導フロー（`POST /.auth/login/aad`に`{"access_token": "<AADのaccess_token>"}`）は、`authenticationToken`を含むJSONを返すか |
| Q2 | 保護ルートへ`X-ZUMO-AUTH: <authenticationToken>`のみ（Cookie無し）で送ったリクエストは通過するか。通過した場合、バックエンドに届く`X-MS-CLIENT-PRINCIPAL*`系ヘッダーは通常のCookie認証と同じ内容になるか |
| Q3 | `GET /.auth/me`も`X-ZUMO-AUTH`単独で認識するか |
| Q4 | `authenticationToken`と`AppServiceAuthSession`Cookieの値は相互運用可能か（Cookieの値をそのまま`X-ZUMO-AUTH`に転記しても通るか） |
| Q5 | 不正・期限切れの`X-ZUMO-AUTH`を送った場合、不正なCookieと同じ`302`リダイレクトになるか、`401`/`403`のJSONが返るか |
| Q6 | 有効な`Cookie`と、（別のアイデンティティの）有効な`X-ZUMO-AUTH`を同じリクエストに両方付けた場合、どちらが優先されるか |
| Q7 | 「未認証時の動作」設定が`HTTP 302リダイレクト`か`HTTP 401`かで、上記の結果は変わるか |

## 前提条件

- Easy Auth有効・IDPがMicrosoft Entra IDのApp Service（Linux、Python）。既存のテスト用
  アプリ登録があればそれで良い
- このPoCの外で、そのアプリ登録に対する有効なAADの**access_token**（id_tokenではない）を
  取得できる手段。例えば、そのアプリ登録がApplication ID URIを公開している場合:

  ```powershell
  az login
  az account get-access-token --resource api://<client-id>
  ```

  公開していない場合はMSAL/`az`デバイスコードフローでそのクライアントID自体を対象に
  access_tokenを要求する

## デプロイ

Easy Auth（Entra ID）を設定済みの既存Linux Web Appをそのまま使う。依存パッケージが無いので
単一ファイルのzipデプロイで済む。

```powershell
# app.pyだけをzip化(単一ファイルなのでパス区切り文字の問題は起きない)
Compress-Archive -Path tools\azure-poc\azure-zumo-auth-poc\app.py -DestinationPath app.zip -Force

az webapp deploy --resource-group <rg> --name <app-name> --src-path app.zip --type zip
az webapp config set --resource-group <rg> --name <app-name> --startup-file "python app.py"
az webapp config appsettings set --resource-group <rg> --name <app-name> --settings WEBSITES_PORT=8000
az webapp restart --resource-group <rg> --name <app-name>
```

デプロイ後、`az webapp config appsettings list`等でEasy Auth(Entra ID)の設定が維持されている
ことを確認しておくとよい(アプリコードの入れ替えだけなので通常は影響しない)。

## テスト0 — 実際のAADのid_tokenを取得する（Q1・Q4・Q6の前提）

ブラウザで通常のログインを行う。

```text
https://<app-name>.azurewebsites.net/.auth/login/aad
```

ログイン完了後、ブラウザの開発者ツールから`AppServiceAuthSession`Cookieの値を控える
（テスト3で使用）。別途、同じアプリ登録に対する`id_token`をMSALまたは`az`デバイスコード
フローで取得する（テスト1のリクエストボディで使用）。

## テスト1 — クライアント主導ログイン（Q1）

```powershell
curl -i --show-error -X POST https://$appName.azurewebsites.net/.auth/login/aad `
  -H "Content-Type: application/json" `
  -d "{`"access_token`": `"$accessToken`"}"
```

レスポンスが`authenticationToken`フィールドを持つJSONかどうか、その値の形式
（不透明なblobかJWT風か）を記録する。

## テスト2 — X-ZUMO-AUTHのみで保護ルートへアクセス（Q2・Q3）

```bash
curl -s -i https://<app-name>.azurewebsites.net/ \
  -H "X-ZUMO-AUTH: <テスト1のauthenticationToken>"

curl -s -i https://<app-name>.azurewebsites.net/.auth/me \
  -H "X-ZUMO-AUTH: <テスト1のauthenticationToken>"
```

エコーされたヘッダー（`X-MS-CLIENT-PRINCIPAL*`、`X-MS-TOKEN-AAD-*`）を、通常のCookie
認証と比較する。

```bash
curl -s -i https://<app-name>.azurewebsites.net/ \
  -H "Cookie: AppServiceAuthSession=<テスト0のCookie値>"
```

## テスト3 — トークンの相互運用性（Q4）

```bash
curl -s -i https://<app-name>.azurewebsites.net/ \
  -H "X-ZUMO-AUTH: <テスト0のAppServiceAuthSession Cookie値>"
```

## テスト4 — 不正なトークンの挙動（Q5）

```bash
curl -s -i https://<app-name>.azurewebsites.net/ -H "X-ZUMO-AUTH: garbage"
```

## テスト5 — 両方付けた場合の優先順位（Q6）

2つの異なるアイデンティティでログインし（または同一アイデンティティの古いトークンと新しい
トークンでも良い）、同一リクエストに両方のヘッダーを付けて、エコーされた
`X-MS-CLIENT-PRINCIPAL*`にどちらのクレームが出るか比較する。

```bash
curl -s -i https://<app-name>.azurewebsites.net/ \
  -H "Cookie: AppServiceAuthSession=<アイデンティティAのCookie>" \
  -H "X-ZUMO-AUTH: <アイデンティティBのauthenticationToken>"
```

## 補足 — `sample_app.py`を使った実機とローカル実装の比較(2026-08-21〜22)

上記のQ1〜Q6はこのフォルダの最小エコーバックエンド(`app.py`)で検証したが、その後
`src/sample_app.py`(エミュレータの動作確認用アプリ)を同じApp Serviceにデプロイし、
実装した`X-ZUMO-AUTH`対応(`_handle_client_directed_login`・`_check_auth_via_bearer_token`)を
ローカルのエミュレータで同じAADテストアプリの資格情報を使って動かし、`/api/session`の
出力を比較した。結果は一致(`X-MS-CLIENT-PRINCIPAL-ID`・`X-MS-CLIENT-PRINCIPAL-IDP: aad`
等が実機・ローカルどちらも同じ形で注入された)。

`sample_app.py`は`src/_sample_app_shared.py`経由で`h2`パッケージに依存するため、この
フォルダの`app.py`と違い単純な依存パッケージ無しzipデプロイでは動かない。実機比較する
場合は`src/`から直接パッケージすること(このフォルダにコピーは置かない — `src/`が正本)。

使い捨てのビルド物(ステージング用フォルダとzip)はリポジトリ直下ではなく、この
PoCフォルダの中に作る。`.gitignore`側で`tools/azure-poc/*/deploy-*/`と
`tools/azure-poc/*/*.zip`を既に除外済み。

```powershell
$rg = "<rg>"
$appName = "<app-name>"
$pocDir = "tools\azure-poc\azure-zumo-auth-poc"

New-Item -ItemType Directory -Force -Path "$pocDir\deploy-sample-app" | Out-Null
Copy-Item src\sample_app.py "$pocDir\deploy-sample-app\"
Copy-Item src\_sample_app_shared.py "$pocDir\deploy-sample-app\"
"h2==4.3.0" | Out-File -Encoding utf8 "$pocDir\deploy-sample-app\requirements.txt"

# ビルドを走らせる設定は、最初のzipデプロイより前に設定しておくこと(下記の
# ハマりポイント参照)
az webapp config appsettings set --resource-group $rg --name $appName `
  --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true SAMPLE_APP_PORT=8000

Compress-Archive -Path "$pocDir\deploy-sample-app\*" -DestinationPath "$pocDir\sample-app.zip" -Force
az webapp deploy --resource-group $rg --name $appName --src-path "$pocDir\sample-app.zip" --type zip
az webapp config set --resource-group $rg --name $appName --startup-file "python sample_app.py"
az webapp restart --resource-group $rg --name $appName
```

デプロイ後、`deploy-sample-app/`と`sample-app.zip`はローカルの使い捨てビルド物なので
削除してよい(`.gitignore`で除外済みなので、消さなくても`git status`は汚れない)。

### ハマったポイント

- **`SCM_DO_BUILD_DURING_DEPLOYMENT=true`は`az webapp deploy`より前に設定する**。後から
  設定してもその時点までのデプロイはビルド無しのまま(Oryxが`requirements.txt`の
  `h2`をインストールせず`ModuleNotFoundError: No module named 'h2'`で起動失敗する)。
  設定後、同じzipを**もう一度**`az webapp deploy`し直す必要がある
- **`WEBSITES_PORT`アプリ設定は、Linuxの組み込みPythonランタイム(カスタムコンテナ
  ではない)では期待通りに反映されないことがあった**(CLI・Portalどちらで`8081`に
  設定してもプラットフォームは`App port: 8000, Port selected by: Default for
  this image`のままだった。原因は特定できていない)。回避策として、
  `WEBSITES_PORT`はデフォルト値(`8000`)のままにしておき、**アプリ側のリスニング
  ポートをそれに合わせる**方が確実だった(`sample_app.py`は`SAMPLE_APP_PORT`環境変数で
  ポートを変更できるので、`SAMPLE_APP_PORT=8000`を設定した)
- `az webapp config appsettings set`/`list`の出力は(比較的新しいaz cliで)全ての値が
  `null`と表示される。これは値が実際に空という意味ではなく、CLI側の表示マスキング
  なので、実際の値を確認したい場合はAzure Portalの「構成」ブレードを見る方が確実

## 結果の記録

実施後は上記「状態」欄を実際の回答で更新し、`ToDo.md`とこのプロジェクトのmemoryに反映
してから、エミュレータ側の実装作業を始めること。

## 後片付け

検証後はWeb App（このPoC専用のリソースグループならリソースグループ全体）を削除して
課金を止めること。
