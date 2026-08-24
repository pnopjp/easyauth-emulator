# App Service→Azure Functions クロスオリジン呼び出しデモ(実機PoC)

`../azure-zumo-auth-poc`(App Service)と`../azure-functions-zumo-auth-poc`(Functions)
はそれぞれ独立に`X-ZUMO-AUTH`/クライアント主導ログインを確認済み。このPoCはその2つを
実際に繋ぎ、「Easy Auth保護下のApp Serviceで配信されたページのJSが、別オリジンの
Functions APIを呼ぶ」という実際のアーキテクチャパターンをブラウザで動かして確認する。

`app.py`は1ページだけのHTML+JSを返す最小サーバー。いずれもまず`GET /.auth/me`
(同一オリジン、Cookieは自動送信)でこのApp Service自身のトークンストアから
`access_token`を取得したうえで、2つのボタンで別々の経路を試せる。

- **Run (X-ZUMO-AUTH)**: その`access_token`を**別オリジンの**Functions Appの
  `POST /.auth/login/aad`に渡してクライアント主導ログインし、Functions側の
  `authenticationToken`を取得してから、`GET /api/session`を
  `X-ZUMO-AUTH: <authenticationToken>`付きで呼ぶ(Cookieはオリジンをまたがない
  ため使えない)
- **Run (Authorization: Bearer)**: `/.auth/login/aad`のラウンドトリップを経由せず、
  `access_token`をそのまま`Authorization: Bearer`ヘッダーで`GET /api/session`に
  渡す(Microsoft公式ドキュメントの「daemon client application(service-to-
  service呼び出し)」パターン)

## 状態: 検証完了(C1〜C8確定。C4は新規発見、C5でその回避策と本人一致を確認、C6・C7でローカルEmulatorをFunctions役・App Service役それぞれにしても同じ流れが成功することを確認、C8で`Authorization: Bearer`直接呼び出しも成功することを確認)

- **C1・C2(確定、2026-08-24)**: `az functionapp cors add`でApp Serviceのオリジンを
  許可するだけで、`/.auth/login/aad`(POST)・`/api/session`(GET)ともにクロスオリジン
  アクセスが成功した。Easy Auth自身の`/.auth/*`ルートも含めて、Functions標準のCORS設定
  だけで十分だった
- **C3(確定、2026-08-24)**: `/.auth/me`→Functions側`/.auth/login/aad`→`/api/session`の
  一連の流れが実機ブラウザで最後まで成功した(全ステップ`200`)
- **C4(新規発見・確定、2026-08-24)**: クライアント主導ログイン(`X-ZUMO-AUTH`)経由で
  Easy Authが principal を組み立てる際、**access_tokenに実際に含まれているクレーム
  (`email`・`preferred_username`・`upn`など)の内容は一切反映されない**。access_token
  にこれらのクレームを含めた状態(アプリ登録の「トークン構成」でaccess token向けに
  オプションクレームを追加し確認済み)でも、`X-MS-CLIENT-PRINCIPAL`(および
  `client_principal.claims`)には`stable_sid`・`nameidentifier`(合成された`sid:...`)・
  `identityprovider`・`ver`・`nbf`/`exp`/`iat`・`iss`/`aud`という固定の最小限クレーム
  集合しか入らない。通常のブラウザ主導ログイン(Cookie)ではid_tokenの`preferred_username`
  等がちゃんと反映されるのとは対照的で、client-directed flow特有の挙動と見られる
  (最初は「access_tokenにプロフィールクレームが無いから」という仮説を立てたが、
  これは誤りだったと実機で確認できた)
- **C5(確定、2026-08-24)**: C4の回避策として、`function_app.py`側で
  `X-MS-TOKEN-AAD-ACCESS-TOKEN`ヘッダー(転送された生のaccess_token)を自前デコードし、
  `oid`/`email`/`preferred_username`を`access_token_identity`として`/api/session`の
  レスポンスに追加した(`_sample_app_shared.py`側は変更せず、このPoC専用の
  `function_app.py`にのみ実装)。デモページ経由(`X-ZUMO-AUTH`)でこれを取得したところ、
  App Service側でサインインした本人のメールアドレスと一致することを確認済み。
  **すなわち、App Service側とFunctions側で同じアカウントでログインできていることを
  直接確認できた**(Easy Auth自身のprincipalではなくaccess_token自体を見る、という
  回避策が機能した)
- **C6(確定、2026-08-26)**: Functions役を実機App Service(https)の代わりに**ローカルの
  easyauth-emulator**(HTTPS化、`site.localhost`+mkcert証明書)にして同じ一連の流れを
  試したところ、こちらも最後まで成功した(全ステップ`200`)。実機App Service(https)
  →ローカルEmulator(https、`site.localhost`)というhttps同士の構成であれば、mixed
  contentの問題は起きない。当初`http://localhost`で試して`TypeError: Failed to
  fetch`(mixed content)になったため、`site.localhost`をmkcertでHTTPS化して解決した。
  なお、このテストを通じてエミュレータ側の実装バグ(`Access-Control-Allow-Origin`が
  preflight応答で二重送信されてしまう不具合)を発見・修正した(`src/app.py`の
  `_dispatch()`のpreflight応答から、`end_headers()`側で既に付与される
  `_cors_response_headers()`の重複呼び出しを削除)
- **C7(確定、2026-08-26)**: C6の逆方向、**App Service役をローカルのeasyauth-emulator
  (https、`site.localhost`)にして、実機のFunctions App(既存のものをそのまま利用)へ
  クロスオリジンでアクセスする構成**も試し、成功した(全ステップ`200`)。この際、
  ローカルEmulator側の`IDP_ENTRA_SCOPES`が既定値(`openid profile email`のみ)だと
  Microsoft Graph向けのトークン(`aud`がGraphの固定ID)になってしまい`401`になった。
  さらに、`IDP_ENTRA_SCOPES`に別の検証(Storage連携機能)で使っていたStorage向け
  スコープ(`aud`が`https://storage.azure.com`になった)が残っていたケースも遭遇した。
  App Service側で`loginParameters`にscopeを追加したのと同じ理由で、ローカル側も
  `IDP_ENTRA_SCOPES = "openid profile email api://<client-id>/user_impersonation"`
  と、このアプリ自身向けのスコープを明示的に設定する必要がある(既定値へのコメント
  アウトでは戻ってしまうので不可)

## 前提条件

- `../azure-zumo-auth-poc`のApp Service(Easy Auth/Entra ID設定済み)
- `../azure-functions-zumo-auth-poc`のFunction App(同じAADアプリ登録で認証設定済み、
  `/api/session`稼働中)

## デプロイ

既存のApp Serviceに、この`app.py`をデプロイする(既存のコードを上書きする)。
使い捨てのzipはこのPoCフォルダの中に作る(`.gitignore`で除外済み)。

```powershell
$rg = "<resource-group-name>"
$appName = "<app-name>"  # azure-zumo-auth-poc / sample_app.py を動かしていたApp Service
$pocDir = "tools\azure-poc\azure-crossorigin-zumo-poc"

Compress-Archive -Path "$pocDir\app.py" -DestinationPath "$pocDir\crossorigin-app.zip" -Force
az webapp deploy --resource-group $rg --name $appName --src-path "$pocDir\crossorigin-app.zip" --type zip
az webapp config set --resource-group $rg --name $appName --startup-file "python app.py"
az webapp config appsettings set --resource-group $rg --name $appName --settings WEBSITES_PORT=8000
az webapp restart --resource-group $rg --name $appName
```

## Functions側にCORSを設定する

```powershell
$funcAppName = "<func-app-name>"

az functionapp cors add --resource-group $rg --name $funcAppName `
  --allowed-origins "https://$appName.azurewebsites.net"
```

## テスト

1. ブラウザで`https://<app-name>.azurewebsites.net/`を開き、Easy Authでサインインする
2. 表示された入力欄に`https://<func-app-name>.azurewebsites.net`を入力する
3. **Run (X-ZUMO-AUTH)**を押す(C1〜C7で確認済みの経路)。ページ上に各ステップの
   HTTPステータスと結果が表示される。ブラウザの開発者ツール(Network/Consoleタブ)で
   CORSエラーが出ていないかも確認する
4. **Run (Authorization: Bearer)**も押してみる(C8、`/.auth/login/aad`を経由せず
   `Authorization: Bearer <access_token>`を直接`/api/session`に渡す経路)

C1・C2でCORSエラーになった場合、`az functionapp cors add`だけでは`/.auth/*`ルートに
効かない可能性がある。その場合はエラー内容(devtoolsのConsoleに出るCORSエラーメッセージ)
を確認し、対処法を検討する。

### C8(確定、2026-08-26): Authorization: Bearer での直接呼び出し

公式ドキュメントの「daemon client application」の記載通り、`/.auth/login/<idp>`を
経由せず、`Authorization: Bearer <access_token>`を保護ルートに直接付けるだけで
認証されることを実機で確認した。クライアント主導ログイン(`X-ZUMO-AUTH`)は必須では
なく、標準的なOAuth2の`Authorization`ヘッダーでも同じ結果が得られる、別の(より
単純な)経路として実機で成立している。

## 結果の記録

実施後は上記「状態」欄を実際の回答で更新し、`ToDo.md`に反映すること。

## 後片付け

検証後はApp Service・Function App(このPoC専用のリソースグループなら
リソースグループ全体)を削除して課金を止めること。
