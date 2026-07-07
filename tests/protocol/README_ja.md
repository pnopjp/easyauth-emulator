# プロトコル欠落 検証アプリ

`ToDo.md`に記載のプロトコル欠落（WebSocket、gRPC、SSE/ストリーミング、chunkedリクエストボディ）を確認するための手動検証用アプリ。単独動作で、`start.py`や`config.toml`には統合していません（`src/sample_app.py`とは異なる位置づけ）。

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

## ゲートウェイ経由での確認（壊れるはず）

1. エミュレーターの`config.toml`に以下を設定します。

   ```toml
   APP_UPSTREAM = http://localhost:8082
   ```

2. `SKIP_AUTH_ROUTES`はグロブではなく正規表現で、値はTOMLの引用符付き文字列である必要があります。ログインなしでテストできるよう以下を追加します。

   ```toml
   SKIP_AUTH_ROUTES = "/ws/,/sse/,/chunked/"
   ```

3. エミュレーターを起動し、ポート8082ではなく`http://localhost:<SITE_PORT>/`（ゲートウェイ）を開いて同じ確認を行います。実機確認済みの挙動（2026-07-07時点、本アプリ vs `main`ブランチの`src/app.py`）は以下の通りです。

   - **WebSocket** — ゲートウェイは`400 Bad Request`を返す。ゲートウェイ自身のヘッダーとupstreamの`101`応答のヘッダーが混在した壊れた応答になっており、Upgradeハンドシェイクを中継できていない。
   - **SSE** — イベントが1つずつ届かず、upstreamの応答が完了する約10秒後（`PROTOCOL_APP_SSE_COUNT` × `PROTOCOL_APP_SSE_INTERVAL_SECONDS`、既定は10×1秒）に**全部まとめて**届く。`_proxy_to`が応答を全部読み切ってから返しているため（[app.py:606](../../src/app.py#L606)）。ストリームがプロキシ側のupstream読み取りタイムアウト（30秒、[app.py:603](../../src/app.py#L603)）より長く続く場合は、`502`で失敗する。
   - **chunkedボディ** — `received_bytes`が`0`で返る（`Transfer-Encoding: chunked`かつ`Content-Length`なしの本文が黙って欠落する）。
   - **gRPC** — 同じ`grpcurl`/gRPCクライアントをゲートウェイの`SITE_PORT`に対して実行すると失敗する。根本原因（ゲートウェイがHTTP/1.1専用でgRPCが要求するHTTP/2接続をネゴシエートできない）は同じでも、クライアント実装によって症状が異なります。
     - Pythonの`grpc`パッケージは即座に失敗: `grpc.RpcError: UNAVAILABLE — Failed parsing HTTP/2 (Expected SETTINGS frame as the first frame, ...)`
     - Go実装の`grpcurl`は自身のダイヤルタイムアウトまで待って失敗: `Failed to dial target host "localhost:<port>": context deadline exceeded`。これが出た場合、先に`curl http://localhost:<SITE_PORT>/healthz`が`ok`を返すか確認し、「何も起動していない」だけの状態ではないことを確かめてください。

## ファイル構成

- `app.py` — HTTP + gRPCの検証用サーバー
- `send_chunked.py` — `Transfer-Encoding: chunked`のPOSTを複数の実チャンクに分けて送信する(使い方は上記「直接アクセスでの確認」参照)
- `echo.proto` — gRPCの最小サービス定義
- `echo_pb2.py`、`echo_pb2_grpc.py` — `echo.proto`から生成済み。再生成する場合:

  ```bash
  python -m grpc_tools.protoc -I tests/protocol --python_out=tests/protocol --grpc_python_out=tests/protocol tests/protocol/echo.proto
  ```
