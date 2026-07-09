# プロトコル欠落 検証アプリ

WebSocket、gRPC、SSE/ストリーミング、chunkedリクエストボディまわりのプロトコル挙動を確認するための手動検証用アプリ（WebSocketのみ`ToDo.md`記載の未対応項目として残っている）。単独動作で、`start.py`や`config.toml`には統合していません（`src/sample_app.py`とは異なる位置づけ）。

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

- HTTP検証ページ: `http://localhost:8082/`（`PROTOCOL_APP_PORT`）
- gRPCサービス（reflection有効）: `localhost:8083`（`PROTOCOL_APP_GRPC_PORT`）

## 直接アクセスでの確認（正常動作するはず）

`http://localhost:8082/`を開き、WebSocketとSSEのセクションを試してください。どちらも正常に動作するはずです。

chunkedボディのセクションにはブラウザ用ボタンがありません。Chrome/Edgeは`fetch`のストリーミングリクエストボディにHTTP/2以上を要求し、HTTP/1.1サーバーに対しては`net::ERR_H2_OR_QUIC_REQUIRED`で拒否します。このアプリもゲートウェイもHTTP/1.1専用なので、ページに表示されているcurlコマンドを使ってください。

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

   - **WebSocket** — ゲートウェイは`400 Bad Request`を返す。ゲートウェイ自身のヘッダーとupstreamの`101`応答のヘッダーが混在した壊れた応答になっており、Upgradeハンドシェイクを中継できていない（実機確認済み、`ToDo.md`記載の未対応項目）。
   - **SSE** — 正しく動作します。イベントは届いた分から順にストリーミングで中継され、upstreamの応答が完了するまでバッファリングされることはありません（`_proxy_to`のHTTP/1.1中継・`HTTP20_PROXY_MODE=all`時の`_http2_relay_request`によるHTTP/2中継の両方で対応済み）。
   - **chunkedボディ** — 正しく動作します。`Transfer-Encoding: chunked`のボディはデコードされてから中継され、`received_bytes`は送信した本文の長さと一致します。
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
     - **必ず`-proto`（または`-import-path`）を指定してください** ——指定しない場合`grpcurl`は
       サーバーリフレクションでメソッドを調べますが、リフレクションは双方向ストリーミングRPCです。
       このゲートウェイのHTTP/2処理（受信側・`APP_UPSTREAM`への中継の両方）は単項リクエスト/
       レスポンスのみに対応しています（設定リファレンスの「HTTP/2とgRPC」節の補足を参照）。
       そのためリフレクション呼び出しはハングします——クライアントが送信側ストリームを
       閉じない限りゲートウェイはリクエストのディスパッチ自体を行わないため、応答が返ってきません。
       `-proto`でリフレクションを回避すればこの問題を避けられます:

       ```bash
       grpcurl -plaintext -proto tests/protocol/echo.proto -d '{"name":"world"}' localhost:<port> echo.Echo/SayHello
       ```

## ファイル構成

- `app.py` — HTTP + gRPCの検証用サーバー
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
