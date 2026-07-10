# Azure WebSocket + HTTP/2(RFC 8441)実機検証用PoC

`ToDo.md`のRFC 8441(HTTP/2上のWebSocketブートストラップ、`:protocol`疑似ヘッダーを使う拡張
`CONNECT`)対応の判断を止めていた未確定点を実機で確認するためのものです。**本物のAzure App
Serviceは「HTTP version: 2.0」と「Web sockets」を両方有効にしたとき実際にRFC 8441を使うのか。
使うとして、バックエンドアプリ自身がHTTP/1.1を話すかHTTP/2を話すか(`http20ProxyFlag`)で挙動は
変わるのか。**公式ドキュメントは「HTTP version」と「Web sockets」を無関係な別設定として説明して
おり、相互作用については一切触れていません。

ブラウザではなく`h2`ライブラリで直接HTTP/2を話すクライアントを使うため、特定ブラウザのRFC 8441
対応状況や実装差異に結果が左右されません。

## 結果(2026-07-10確定)

| # | 質問 | 答え |
| --- | --- | --- |
| 1 | App Serviceのフロントエンドは`SETTINGS_ENABLE_CONNECT_PROTOCOL`を広告するか | する(`1`) |
| 2 | WebSocketルートへのRFC 8441拡張CONNECTは成功するか | 成功する(`:status 200`、エコーの往復まで確認済み) |
| 3 | `http20ProxyFlag`(バックエンドがHTTP/1.1かHTTP/2か)でこれは変わるか | 変わらない |
| 4 | バックエンドへはどう中継されるか | `http20ProxyFlag`の値に関わらず、**常に**従来のHTTP/1.1 Upgradeハンドシェイクとして中継される |
| 5 | バックエンドがネイティブHTTP/2でしかWebSocketを話せない(HTTP/1.1 Upgrade未対応)場合はどうなるか | `502 Bad Gateway`になる(実際にそのバックエンドを先にデプロイして確認済み) |

**結論**: Azure App ServiceのHTTP/2フロントエンドは、クライアント向けには本当にRFC 8441を実装
している。しかし内部では、バックエンドアプリへ中継する前に**常に**WebSocketのハンドシェイクを
従来のHTTP/1.1 Upgradeリクエストへダウングレードしている。`http20ProxyFlag`でバックエンドが他の
すべての通信をHTTP/2で話す設定にしていても、バックエンド自身はRFC 8441を一切知らなくてよい。
これは、拡張CONNECTに対して現状`501`を返すこのエミュレーターの`_Http2StreamHandler`との、実際の
忠実性ギャップである(エミュレーターの設計への具体的な影響は`ToDo.md`とこのプロジェクトのmemory
`project_proxy_streaming_and_websocket.md`を参照)。

これはAzure Container Appsとは別の話で、Container Apps側には既に確認済みの無関係な実機の制約が
ある(`ingress.transport: http2`/`auto`だとWebSocketが完全に動かない、`microsoft/azure-container-
apps`のissue #280・#562)。Container Appsの忠実性は今回の結果に影響されない。

## デプロイ

1. Linux Web App(Python 3.12ランタイム)を作成する。カスタム起動コマンドとWebSocketsに対応した
   プラン(B1以上。Free/Sharedプランは非対応)を使う
2. `h2`とその依存パッケージをこのフォルダにvendorする(デプロイ時のビルドは不要、というより
   このzipデプロイ構成ではOryxのビルドパイプラインが確実には`pip install`を実行してくれない)。

   ```bash
   pip install --target=./vendor h2==4.3.0 hpack hyperframe
   ```

3. このフォルダ(`app.py`、`vendor/`)をzipデプロイする。例:

   ```bash
   az webapp deploy --resource-group <rg> --name <app-name> --src-path app.zip --type zip
   ```

   zipは**フォワードスラッシュ区切りのパス**で作ること(WindowsのPowerShellの
   `Compress-Archive`はバックスラッシュ区切りのパスでzipを作ってしまい、デプロイ時にLinux側の
   `rsync`が`Invalid argument (22)`で失敗する)。代わりにPythonの`zipfile`を使う:

   ```python
   import zipfile, os
   with zipfile.ZipFile("app.zip", "w", zipfile.ZIP_DEFLATED) as zf:
       zf.write("app.py", "app.py")
       for root, dirs, files in os.walk("vendor"):
           for f in files:
               full = os.path.join(root, f)
               zf.write(full, os.path.relpath(full, ".").replace(os.sep, "/"))
   ```

4. **構成 → 全般設定**:
   - HTTPバージョン: `2.0`
   - Webソケット: `オン`
   - 起動コマンド: `python app.py`
5. **構成 → アプリケーション設定**: `WEBSITES_PORT` = `8000`、
   `SCM_DO_BUILD_DURING_DEPLOYMENT` = `false`(Oryxのビルドを完全にスキップする。依存パッケージは
   vendor済みなのでビルド自体が不要)

**重要**: 最初のデプロイでは`http20ProxyFlag`を既定値(`0`、バックエンドはHTTP/1.1)のままに
しておくこと。Azure自身のコンテナのウォームアップ/ヘルスチェックは、**`http20ProxyFlag`の値に
関わらず常に素のHTTP/1.1でバックエンドに話しかける**。`app.py`が同じポートでHTTP/1.1とh2cの両方を
扱えるようにしてあるのはこのためで、h2cしか理解できないバックエンドはこのチェックを一切通過でき
ず、サイト自体が起動しない(h2c専用版を先にデプロイして確認済み。すべてのプローブ試行で
`h2.exceptions.ProtocolError: Invalid HTTP/2 preamble`が発生し、230秒後に`ContainerTimeout`)。

## テスト1 — App Serviceはクライアント向けにそもそもRFC 8441を使うか

```bash
python check_rfc8441.py <app-name>.azurewebsites.net
```

期待される出力: ALPNが`h2`にネゴシエートされ、サーバーが`SETTINGS_ENABLE_CONNECT_PROTOCOL = 1`を
広告し、拡張CONNECTが`:status 200`を返し、エコーされたWebSocketフレーム(`echo: hello`)が表示
される。

## テスト2 — `http20ProxyFlag`(バックエンド側のプロトコル)で答えは変わるか

1. **構成 → 全般設定 → HTTP 2.0 プロキシ**を`All`に設定する(またはCLIで
   `az webapp config set --generic-configurations '{"http20ProxyFlag": 1}'`)
2. `check_rfc8441.py`を再実行する。バックエンドが(ここの`app.py`のように)HTTP/1.1 Upgrade
   *のみ*に対応している場合、テスト1と同じ成功結果になるはず。これは、バックエンドを他の全通信で
   HTTP/2にする設定にしていても、Azureがwebsocketのハンドシェイクだけは中継前にHTTP/1.1へ
   ダウングレードしていることの証明になる
3. バックエンドがHTTP/1.1 Upgradeへのフォールバックに対応**できない**場合どうなるかを見るには、
   `app.py`の`_handle_http11`から`Upgrade: websocket`の処理を一時的に取り除いて再デプロイする。
   拡張CONNECTは`:status 502`で失敗するはず(このPoCの開発中に実際に確認済み)
4. 検証後は`http20ProxyFlag`を`0`に戻す(`az webapp config set
   --generic-configurations '{"http20ProxyFlag": 0}'`)

## 結果の記録

結果はこのリポジトリのmemory/`ToDo.md`に記録し、検証後はAzureリソースグループを削除して課金を
止めてください(リソースグループを他の用途と共有していて残したい場合は、このWebアプリだけ削除
すれば十分です)。
