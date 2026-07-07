# Azure gRPC + Easy Auth 実地検証用PoC

gRPC対応の設計を進める上で未確定だった点（`ToDo.md`参照）を実機で確認するためのものです。
**本物のAzure App ServiceのEasy Authは、gRPC用リスナー（`HTTP20_ONLY_PORT`）を保護するのか、
それともgRPCトラフィックは素通りするのか？** 公式ドキュメントはどちらとも明記していません
（Easy AuthのドキュメントはgRPCに触れず、gRPCのドキュメントはEasy Authに触れていません）。

このアプリは受け取ったgRPCメタデータを全部そのまま返します。Easy Authが本当にprincipal相当の
情報を注入しているなら（あるいは自分で送ったcookieそのものが）、レスポンスに直接出てきます。

## デプロイ

1. Linux Web App（Python 3.12ランタイム）を作成。カスタム起動コマンドが使えるプラン（B1以上）
2. このフォルダ（`app.py`、`requirements.txt`、`echo.proto`、`echo_pb2.py`、`echo_pb2_grpc.py`）
   をzipデプロイ、または`az webapp up`をこのディレクトリで実行
3. **構成 → 全般設定**:
   - HTTPバージョン: `2.0`
   - HTTP 2.0 プロキシ: `gRPC のみ`
   - 起動コマンド: `python app.py`
4. **構成 → アプリケーション設定**: `HTTP20_ONLY_PORT` = `8585` を追加
5. **認証**（Easy Auth）: IDプロバイダーを追加（Microsoft Entra IDのExpress設定が最速）。
   「未認証要求のアクション」の設定値（既定は「HTTP 302 Found リダイレクト」）を覚えておく
6. 適用して再起動を待つ

## テスト1 — 未認証のgRPC呼び出しはアプリまで届くか?

```bash
grpcurl -d '{"name":"world"}' <app-name>.azurewebsites.net:443 echo.Echo/SayHello
```

- **成功する**（`message`が返る）→ Easy Authはこのポートを保護していない。gRPCトラフィックは
  素通りしている
- **失敗する**（`PermissionDenied`、`Unauthenticated`、またはEasy Authが横取りしたTLS/
  ハンドシェイクエラーなど）→ Easy AuthはgRPCトラフィックを横取りしている。正確なエラー内容を
  記録する（エミュレーターが未認証gRPCをどう拒否すべきかの参考になる）

## テスト2 — 認証済みの呼び出しではprincipal相当の情報が注入されるか?

1. ブラウザで`https://<app-name>.azurewebsites.net/`を開き、Easy Authでサインインする
2. DevTools → Application/Storage → Cookiesで、`AppServiceAuthSession`の値をコピーする
3. 以下を実行する:

   ```bash
   grpcurl -H "cookie: AppServiceAuthSession=<value>" -d '{"name":"world"}' <app-name>.azurewebsites.net:443 echo.Echo/SayHello
   ```

4. 返ってきた`message`を確認する — 受け取った全メタデータのキー/値が列挙されている。
   `x-ms-client-principal`、`x-ms-client-principal-id`、`x-ms-client-principal-name`等
   （このエミュレーターがHTTP/1.1側で既に注入しているものと同等のもの、`src/app.py`参照）が
   無いか確認する

## 結果の記録

両方のテストを実行した結果が、エミュレーターのgRPCアーキテクチャを決めます。

- **Easy Authを完全に素通りする** → エミュレーターのgRPCリスナーは、認証と無関係な単純な
  パススルーでよい（前述の選択肢A）。principal相当のメタデータ注入は不要で、実際の挙動と一致する
- **保護されていて、メタデータ注入もある** → エミュレーターにはgRPC対応の認証付きプロキシが
  必要（選択肢B）。実装コストは大きく上がるが、本物のギャップとして対処が必要
- **保護されているが、メタデータ注入は無い（ゲートのみ）** → 中間案: 認証チェックはするが
  それ以外は素通しにする

結果はこのリポジトリのmemory/`ToDo.md`に記録し、検証後はリソースグループを削除して
課金を止めてください。
