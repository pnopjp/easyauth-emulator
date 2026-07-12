# プロトコル欠落 検証アプリ

WebSocket、gRPC、SSE/ストリーミング、chunkedリクエストボディまわりのプロトコル挙動を確認するための手動検証用アプリ。`src/sample_app.py`が持つprincipal/claims/storageの確認ページも同梱しています(どちらも「デモ用APP_UPSTREAM」という同じ役割で、こちらはgRPCも含む「フル機能版」、`src/sample_app.py`は追加の`grpcio`依存を持たずエミュレーター本体と一緒に配布される「軽量版」という位置づけ)。共有ロジックは`src/_sample_app_shared.py`にあります。`start.py`には統合していません(`src/sample_app.py`は`start.py`が自動起動しますが、こちらは単独動作です)。

`config.toml`はエミュレーター本体と同じ仕組みで読み込みますが、`--config PATH`で上書きできます(`tests/python/test_protocol_gaps.py`が、開発者の実際のシークレットを含む`config.toml`を自動テスト中に読まないようにするため使用):

```bash
python -m tests.protocol.app --config path/to/other-config.toml
```

## セットアップ

`requirements-test.txt`の`grpcio`/`grpcio-reflection`が必要です。システムのPython(グローバル環境)にはこれらは入っていません。プロジェクトの`.venv`を使うか、実行するインタープリタに直接インストールしてください。

```powershell
# プロジェクトの.venvを使う場合 (Windows PowerShell)
.venv\Scripts\Activate.ps1
pip install -r requirements-test.txt
```

```bash
# または今使っているインタープリタに直接インストール
pip install -r requirements-test.txt
```

## 起動

```bash
python -m tests.protocol.app
```

2つのサーバーが起動します（Ctrl+Cで両方停止します）。

- HTTP検証ページ: `http://localhost:8082/`（`SAMPLE_APP_PORT`。`src/sample_app.py`と同じ設定）
- gRPCサービス（reflection有効）: `localhost:8083`（`PROTOCOL_APP_GRPC_PORT`）

## 直接アクセスでの確認（正常動作するはず）

`http://localhost:8082/`を開き、WebSocketとSSEのセクションを試してください。どちらも正常に動作するはずです。`http://localhost:8082/session`には`src/sample_app.py`と同じprincipal/claims/Storage確認ページがあります(別アプリを起動しなくてもEasy Authヘッダー注入を確認できます)。

chunkedボディのセクションにはブラウザ用ボタンがありません。Chrome/Edgeは`fetch`のストリーミングリクエストボディにHTTP/2以上を要求し、そうでなければ`net::ERR_H2_OR_QUIC_REQUIRED`で拒否します。ブラウザはHTTP/2をTLS経由でしかネゴシエートしないため(このアプリは平文のHTTP/2(h2c)も受け付けますが、それは対象外)、ページに表示されているcurlコマンドを使ってください。

```bash
curl -X POST --no-buffer -H "Transfer-Encoding: chunked" --data-binary "chunked request body test" http://localhost:8082/chunked/echo
```

curlは本文の全長を最初から知っているため、実際には1チャンクにまとめて送信します（正規のchunked形式ではあるが、複数チャンクへの分割はしていない）。複数チャンクに分けて時間差で送りたい(実際のストリーミングクライアントに近い)場合は`send_chunked.py`を使います。

```bash
python -m tests.protocol.send_chunked localhost 8082 /chunked/echo
python -m tests.protocol.send_chunked localhost 8080 /chunked/echo   # ゲートウェイ経由。ポートはSITE_PORTに合わせる
```

オプション: `--text`(送信する本文、既定`"chunked request body test"`)、`--chunk-size`(チャンクあたりのバイト数、既定`8`)、`--chunk-delay`(チャンク間の待機秒数、既定`0.2`)。

gRPCは以下で確認します。

```bash
grpcurl -plaintext -d '{"name":"world"}' localhost:8083 echo.Echo/SayHello
```

## ゲートウェイ経由での確認

1. エミュレーターの`config.toml`に以下を設定します。

   ```toml
   APP_UPSTREAM = http://localhost:8082
   ```

2. `SKIP_AUTH_ROUTES`はグロブではなく正規表現で、値はTOMLの引用符付き文字列である必要があります。ログインなしでテストできるよう以下を追加します。

   ```toml
   SKIP_AUTH_ROUTES = "/ws/,/sse/,/chunked/"
   ```

3. エミュレーターを起動し、ポート8082ではなく`http://localhost:<SITE_PORT>/`（ゲートウェイ）を開いて同じ確認を行います。現状の挙動は以下の通りです。

   - **WebSocket** — クライアントがHTTP/1.1（このアプリのページも既定でこちらでテストします）とHTTP/2（RFC 8441の拡張`CONNECT`）のどちらで来ても正しく動作します。どちらの場合も、ゲートウェイは`APP_UPSTREAM`へは従来のUpgradeハンドシェイクとして中継し、その後クライアントとupstreamの間で生のバイト列を双方向にそのまま中継します。WebSocketのフレーム内容自体は一切解釈しないため、このアプリに限らずどのWebSocketアプリケーションでも動作します。本物のAzure App Serviceも`HTTP20_PROXY_MODE`の値に関わらず全く同じ変換をします(詳細は`tools/azure-poc/azure-websocket-poc`参照)。このアプリ自身はRFC 8441を実装していません——本物のAzure App Serviceのバックエンドも同様にRFC 8441を実装する必要がないためです。
   - **SSE** — 正しく動作します。イベントは届いた分から順にストリーミングで中継され、upstreamの応答が完了するまでバッファリングされることはありません（`_proxy_to`のHTTP/1.1中継・`HTTP20_PROXY_MODE=all`時の`_http2_relay_request`によるHTTP/2中継の両方で対応済み）。
   - **chunkedボディ** — 正しく動作します。`Transfer-Encoding: chunked`のボディはデコードされ、全体をバッファリングせず各チャンクが届いた時点で`APP_UPSTREAM`へ転送されます。`received_bytes`は送信した本文の長さと一致します。
   - **HTTP/2(`HTTP20_PROXY_MODE=all`)** — 正しく動作します。このアプリはHTTP/1.1に加えて平文のHTTP/2(h2c)も受け付けるため、`all`(gRPC以外のコンテンツなら`grpc-only`も同様)によるゲートウェイの本物のHTTP/2中継が502にならなくなりました。
   - **gRPC** — 同じ`grpcurl`/gRPCクライアントをゲートウェイの`SITE_PORT`に対して実行すると失敗する。根本原因（ゲートウェイがHTTP/1.1専用でgRPCが要求するHTTP/2接続をネゴシエートできない）は同じでも、クライアント実装によって症状が異なります。
     - Pythonの`grpc`パッケージは即座に失敗: `grpc.RpcError: UNAVAILABLE — Failed parsing HTTP/2 (Expected SETTINGS frame as the first frame, ...)`
     - Go実装の`grpcurl`は自身のダイヤルタイムアウトまで待って失敗: `Failed to dial target host "localhost:<port>": context deadline exceeded`。これが出た場合、先に`curl http://localhost:<SITE_PORT>/healthz`が`ok`を返すか確認し、「何も起動していない」だけの状態ではないことを確かめてください。

     これは無条件の欠落ではなく、現在は**既定の挙動**です。gRPC対応は`HTTP20_ENABLED`/`HTTP20_PROXY_MODE`
     でオプトインできます（設定リファレンスの[「HTTP/2とgRPC」](../../docs/configuration-reference_ja.md#http2とgrpc)を参照）。
     これらを設定すれば同じ呼び出しが成功します — `tests/python/test_protocol_gaps.py`の
     `test_grpc_call_through_gateway`に動作例があります（別のゲートウェイインスタンスで
     `HTTP20_ENABLED=true`/`HTTP20_PROXY_MODE=grpc-only`を設定）。

     `grpcurl`でゲートウェイ経由（`SITE_PORT`または`APPSERVICE_HTTP20_ONLY_PORT`。直接8083に
     繋ぐ場合は対象外）を試す際に知っておくべき点が2つあります:

     - **未認証呼び出しはハングせず即座に`401`が返ります。** 保護対象ルートへの未認証リクエストは
       通常`/.auth/login`へのリダイレクトになりますが、gRPCクライアントはリダイレクトに追従できません
       ——`Content-Type`が`application/grpc*`のリクエストには、実機のApp ServiceのgRPC専用ポートと
       同様に素の`401`（+`WWW-Authenticate: Bearer`）を返すため、呼び出し側のデッドラインまで
       ハングせず`Unauthenticated`として即座に失敗します。
     - **サーバーリフレクションが使えるので`-proto`は必須ではありません。** `grpcurl -plaintext
       localhost:<port> list`（`-proto`なし）でゲートウェイ経由でもサービス一覧が取得でき、
       `echo.Echo/SayHello`もプロトファイルなしで呼び出せます。クライアントストリーミング・
       双方向ストリーミングRPC（リフレクション含む）も、ストリームが終わるのを待たずリクエスト
       開始時点で即座にディスパッチするため、正しく中継されます。本物のAzure App Serviceでは
       認証済みの状態だとサーバーリフレクション自体がプラットフォーム側の制約で失敗する
       (`tools/azure-poc/azure-grpc-poc/README_ja.md`の「結果」節参照)ため、この点は意図的に
       再現していません。実際のgRPCクライアントは通常`.proto`から生成したスタブを直接使い
       実行時にリフレクションへ依存しないため、実機との差異が問題になるのは`grpcurl`等での
       手動デバッグ時に限られます。

## ファイル構成

- `app.py` — HTTP + gRPCの検証用サーバー(principal/claims/storageページとWebSocket/SSE/chunkedボディのハンドラーは`src/_sample_app_shared.py`から読み込み、`src/sample_app.py`と共有)
- `send_chunked.py` — `Transfer-Encoding: chunked`のPOSTを複数の実チャンクに分けて送信する(使い方は上記「直接アクセスでの確認」参照)
- `send_http2.py` — 平文HTTP/2（h2c）で1リクエストを送信しレスポンスを表示する。`HTTP20_ENABLED`を
  通常の（gRPCでない）ルートに対してテストするためのもの。Windows版curlは通常HTTP/2に非対応
  （`curl --version`のFeaturesに"HTTP2"が出ない）ため、代わりに使う。gRPC自体は`grpcurl`か本物の
  gRPCクライアントを使うこと。

  ```bash
  python -m tests.protocol.send_http2 localhost 8080 /healthz
  python -m tests.protocol.send_http2 localhost 8080 /.auth/login
  ```

- `echo.proto` — gRPCの最小サービス定義
- `echo_pb2.py`、`echo_pb2_grpc.py` — `echo.proto`から生成済み。再生成する場合:

  ```bash
  python -m grpc_tools.protoc -I tests/protocol --python_out=tests/protocol --grpc_python_out=tests/protocol tests/protocol/echo.proto
  ```
