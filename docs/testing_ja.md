# テスト戦略

## 概要

本ドキュメントは EasyAuth Emulator のテスト戦略を定義します。
リリース公開前に 3 層のテストが品質ゲートとして機能します。

```
push v*.*.*
     │
     ▼
  build（マトリクス: windows / macos-x64 / macos-arm64 / linux）
     │
     ▼
  test
  ├─ Python 単体テスト     （pytest、任意の 1 プラットフォーム）
  ├─ TypeScript テスト      （@vscode/test-cli + Mocha、任意の 1 プラットフォーム）
  └─ バイナリ スモークテスト（各ビルド成果物、全プラットフォーム）
     │
     ▼
  publish
  ├─ GitHub Release 作成
  └─ VS Code Marketplace 公開
```

---

## Layer 1 — Python 単体テスト

**フレームワーク:** pytest  
**格納場所:** `tests/python/`  
**実行タイミング:** CI `test` ジョブ、任意の 1 プラットフォーム（例: `ubuntu-latest`）

`src/app.py` の純粋なロジック関数を対象とします。
サブプロセス・HTTP 通信・外部サービスは一切不要です。

### テスト対象

#### グループ A — 完全な純粋関数（環境依存なし）

| 関数 | ファイル:行 | テストシナリオ |
| --- | --- | --- |
| `_decode_jwt_claims(token)` | `src/app.py:290` | 正常な JWT → クレーム dict；不正な base64 → `{}`；セグメント数不正 → `{}`；空文字列 → `{}` |
| `_decode_principal(header_value)` | `src/app.py:280` | 正常な base64 JSON → dict；不正な base64 → `None`；空文字列 → `None` |
| `_compute_client_principal(user, email, provider, claim, id_token)` | `src/app.py:305` | JWT id\_token あり → JWT からクレームを取得；id\_token なし → フォールバッククレーム；user・email ともに空 → `""`；出力は必須キー（`auth_typ`・`name_typ`・`role_typ`・`claims`）を持つ正常な base64 JSON |
| `_safe_redirect(url)` | `src/app.py:274` | `/foo` → `/foo`；`//evil.com` → `/`；`https://evil.com` → `/` |
| `_parse_skip_routes(raw)` | `src/app.py:179` | `"GET=/api/.*"` → `[("GET", pattern)]`；`"/health"` → `[("*", pattern)]`；カンマ区切り複数エントリ；空文字列 → `[]`；前後の空白は除去される |
| `_idp_cfg_prefix(idp)` | `src/app.py:196` | `"entra"` → `"IDP_ENTRA"`；`"my-idp"` → `"IDP_MY_IDP"`；`"openid-connect"` → `"IDP_OPENID_CONNECT"` |
| `_provider_logout_bridge_url(idp, post_logout_redirect_uri)` | `src/app.py:270` | `/.auth/provider_logout/<idp>?post_logout_redirect_uri=<エンコード済み>` が返ること |
| `_load_config(config_path)` | `src/app.py:21` | 正常な TOML → フラット dict；`bool` 値 → `"true"`/`"false"`；リスト値 → カンマ結合文字列；存在しないファイル → `{}` |

#### グループ B — 環境依存関数（`monkeypatch.setenv` でテスト可能）

| 関数 | ファイル:行 | テストシナリオ |
| --- | --- | --- |
| `_parse_bool_cfg(name, default)` | `src/app.py:171` | `"1"`・`"true"`・`"yes"`・`"on"` → `True`；`"false"`・`"0"`・`""` → `False`；環境変数未設定時はデフォルト値を使用 |
| `_idp_auth_provider(idp)` | `src/app.py:210` | `entra` でオーバーライドなし → `"aad"`；`IDP_ENTRA_KIND=oidc` → `"oidc"`；`IDP_ENTRA_AUTH_PROVIDER` が最優先 |
| `_idp_user_id_claim(idp)` | `src/app.py:217` | `entra` → `"preferred_username"`；`google` → `"email"`；`IDP_ENTRA_AUTH_USER_ID_CLAIM` でオーバーライド可能 |
| `_idp_logout_endpoint(idp)` | `src/app.py:237` | 環境変数で明示指定された場合はそれを使用；`microsoft` kind かつ issuer が `/v2.0` 終端 → URL 自動導出；その他の kind → `""` |
| `_build_provider_logout_url(idp, post_logout_redirect_uri)` | `src/app.py:249` | エンドポイント未設定 → `""`；相対リダイレクト URI は SITE\_URL + SITE\_PORT で絶対 URL 化；エンドポイントの既存クエリパラメータは保持される |

---

## Layer 2 — TypeScript テスト

**フレームワーク:** `@vscode/test-cli` + Mocha（VS Code Extension Host 上で実行）  
**格納場所:** `vscode-extension/src/test/`  
**実行タイミング:** CI `test` ジョブ、任意の 1 プラットフォーム

実テストは 2 種類に分かれます。

### ユニットテスト（`src/test/portDetector.test.ts`）

`vscode-extension/src/portDetector.ts` の VS Code API に依存しない純粋ロジックメソッドを対象とします。
private メソッドは `(instance as any).method(...)` 経由でアクセスします。

| メソッド | ファイル:行 | テストシナリオ |
| --- | --- | --- |
| `extractPortFromText(text)` | `portDetector.ts:264` | .NET: `"Now listening on: http://localhost:5000"` → `5000`；Tomcat: `"Tomcat started on port 8080"` → `8080`；汎用: `"listening on port 3000"` → `3000`；Flask: `"Running on http://127.0.0.1:5000"` → `5000`；Uvicorn: `"Uvicorn running on http://0.0.0.0:8000"` → `8000`；マッチしないテキスト → `null` |
| `portFromUrlList(urlStr)` | `portDetector.ts:160` | 単一 URL → ポート番号；セミコロン区切りリストで HTTP を HTTPS より優先；末尾スラッシュを正しく処理；明示ポートのない URL → `null`；空文字列 → `null` |
| `portFromLaunchConfig(cfg)` | `portDetector.ts:136` | `env.PORT` 設定 → 返す；`ASPNETCORE_URLS` → ポート抽出；`ASPNETCORE_HTTP_PORTS` → 先頭ポート抽出；`applicationUrl` 文字列 → ポート抽出；いずれも未設定 → `null` |

> **現時点で対象外:** `detect()`・`fromLaunchJson()`・`detectFramework()`・`fromConfigFile()` は `vscode.workspace` や `fs` に依存しており、将来のマイルストーンに持ち越します。

### 統合テスト（`src/test/extension.test.ts`）

実際の VS Code Extension Host 上で拡張機能を起動して検証します。

| テスト | 内容 |
| --- | --- |
| extension is present | `pnop.easyauth-emulator` がインストール済みであること |
| extension is active | `activate()` 後に `isActive === true` であること |
| all commands are registered | 全 11 コマンド（`easyauth.start` 等）がコマンドパレットに登録済みであること |

---

## Layer 3 — バイナリ スモークテスト

**実行タイミング:** CI `test` ジョブ、全 4 プラットフォーム（`build` ジョブに依存）

各プラットフォームのビルド成果物（アーティファクト）をダウンロードし、
プロセスがクラッシュせずに起動できることを確認します。

### テストシナリオ

| 確認内容 | コマンド | 期待値 |
| --- | --- | --- |
| バイナリが実行可能で正常終了する | `./easyauth-emulator --help` | 終了コード `0` |

これは意図的に最小限の確認です。以下を保証します：

- PyInstaller が必要な Python モジュールをすべてバンドルできている
- 対象 OS / アーキテクチャでライブラリ欠落なく動作する

---

## 将来のテスト拡充計画

| レイヤー | 内容 | 優先度 |
| --- | --- | --- |
| VS Code 拡張機能 統合テスト（拡充） | `EmulatorManager` 状態遷移・`PortDetector.detect()` フロー全体（基本的な起動・コマンド登録テストは実装済み） | `vscode.workspace` や `fs` に依存するため追加のセットアップが必要 |
| E2E / HTTP 統合テスト | ゲートウェイを起動してリクエストを送り、レスポンスヘッダーをアサート | モック oauth2-proxy または実際の IDP クレデンシャルが必要 |
| 設定バリデーションテスト | 不正な `config.toml` を渡してエラーハンドリングを検証 | 優先度低（既存の TOML パースでほぼカバー済み） |

---

## ローカルでの実行方法

### Python

```bash
# テスト依存パッケージのインストール
pip install pytest

# Python 単体テストをすべて実行
pytest tests/python/ -v
```

### TypeScript

```bash
cd vscode-extension

# 依存パッケージのインストール（初回のみ）
npm install

# テストを実行（esbuild バンドル + tsc コンパイル → VS Code Extension Host 上で実行）
npm test
```

### バイナリ スモークテスト（手動）

```bash
# scripts/package.py でビルド後
dist/easyauth-emulator/easyauth-emulator --help
```
