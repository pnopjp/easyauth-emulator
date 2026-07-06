# EasyAuth Emulator VS Code 拡張機能 設計仕様書

## 1. 概要

### 目的

EasyAuth Emulator をローカル開発環境の IDE と統合し、デバッグ実行の開始・終了に連動して自動起動・自動停止・ポート追従できるようにする。

### スコープ

- EasyAuth Emulator コアのリファクタリング（config.json 対応、CLI オプション追加）
- VS Code 拡張機能の新規開発
- 他 IDE 対応は将来フェーズ

### パッケージング方針

- **(b) をインストールすると (c) も同梱される。** 別途 (c) を用意する必要はない。
- **(c) は単体でも導入・利用できる。** IDE を使わない環境では (c) だけをインストールして設定ファイルを編集して使う。

---

## 2. コンポーネント構成

```text
(a) Visual Studio Code
     ├── デバッグセッション管理
     └── ワークスペースファイル群
          （launch.json / .env / launchSettings.json / application.properties 等）

          ↑ イベント受信・ファイル読み取り・デバッグ出力イベント受信

(b) EasyAuth Emulator VS Code 拡張機能          ← (c) を同梱
     ├── 言語・フレームワーク検知（ワークスペースごとにキャッシュ）
     ├── ポート検知ロジック
     ├── (c) の状態管理
     ├── VS Code UI（通知 / 設定 / ステータスバー）
     └── プロセス管理
          （Node.js child_process で easyauth-emulator バイナリを子プロセスとして起動・kill する）

          ↑ CLI コマンド呼び出し（easyauth-emulator ...）

(c) EasyAuth Emulator（コア）
     ├── easyauth-emulator（バイナリ。PyInstaller でビルド済み）
     ├── src/app.py（HTTP ゲートウェイ、バイナリに同梱）
     └── oauth2-proxy（IDP 認証プロキシ、起動時に自動ダウンロード）
```

### 設計方針

- **(b) は (a) に強く依存する。** ポート検知・UI はすべて (b) の責務。
- **(c) はポート検知の知識を持たない。** 渡された値でプロキシを起動するだけ。
- **(b) → (c) の制御は CLI のみ。** 制御 API は不要（常時起動を前提としないため）。
- **(c) は単体でも動作する。** IDE なし環境では設定ファイルを直接編集して使う。
- **(b) は `extensionKind: ["workspace"]` として動作する。** Remote - SSH / Remote - Tunnel 環境でも、VS Code がリモートホスト上に (b) を自動配置するため、ポートスキャン・(c) 起動・ファイル読み取りはすべてリモートホスト上で正しく動作する。

### Remote 環境での動作

Remote - SSH / Remote - Tunnel で開発している場合、デバッガ・アプリ・(b)・(c) はすべてリモートホスト上で動作する。VS Code の UI（ステータスバー・通知）のみローカル PC に表示される。

```text
ローカル PC                        リモートホスト
────────────────────               ──────────────────────────────────
VS Code UI（表示のみ）  ←通信→   VS Code 拡張機能ホスト
                                    ├─ (b) 拡張機能（自動配置）
                                    ├─ (c) EasyAuth Emulator
                                    ├─ デバッガ
                                    └─ 開発中のアプリ
```

**追加要件：** なし。easyauth-emulator バイナリは自己完結しており、リモートホストに Python は不要。

---

## 3. コンポーネント責務

### (b) VS Code 拡張機能

| 責務 | 詳細 |
| --- | --- |
| デバッグ検知 | `vscode.debug.onDidStartDebugSession` / `onDidTerminateDebugSession` |
| 言語・フレームワーク検知 | ワークスペースごとに一度検知してキャッシュ。ポート検知失敗時に再検知を促す（§6 参照） |
| ポート取得 | 設定ファイル読み取り → stdout 解析 → ポートスキャン（§6 参照） |
| プロセス管理 | (c) バイナリを CLI で起動・停止・再起動（child_process による子プロセス制御） |
| 状態管理 | (c) の状態を監視・保持（§4 参照） |
| UI | ステータスバー表示・通知・ポート確認ダイアログ |
| ログ転送 | (c) の stdout/stderr を VS Code Output Channel に表示 |

### (c) EasyAuth Emulator コア

| 責務 | 詳細 |
| --- | --- |
| プロキシ起動 | 指定された APP_UPSTREAM でゲートウェイ・oauth2-proxy を起動 |
| 設定読み込み | config.toml（CLI オプションで上書き可） |
| 単体動作 | IDE なしでも設定ファイルだけで動作 |

---

## 4. (c) の状態管理

(b) は (c) の子プロセスを監視し、以下の状態を保持する。

| 状態 | 意味 | 遷移条件 |
| --- | --- | --- |
| `stopped` | 未起動 | 初期状態、または正常停止後 |
| `unconfigured` | 未設定 | VS Code 設定に IDP の clientId が設定されていない場合 |
| `missing_secret` | シークレット未登録 | clientId は設定済みだが、クライアントシークレットが SecretStorage に登録されていない場合 |
| `missing_entra_issuer` | Entra Issuer URL 未設定 | Entra の clientId とシークレットは設定済みだが、oidcIssuerUrl が空の場合（Entra のみ対象） |
| `starting` | 起動処理中 | `easyauth-emulator` を実行した直後 |
| `running` | 正常稼働中 | stdout に `All processes started` を検出 |
| `error` | 異常終了 | プロセスが非ゼロ終了コードで終了、またはタイムアウト |

**タイムアウト：** `starting` 状態のまま 30 秒以内に `All processes started` を検出できない場合は `error` に遷移する。

状態はステータスバーに反映される（§10 参照）。

---

## 5. ライフサイクル

### セッション紐付けルール

(b) は (c) を起動したデバッグセッションの ID を保持する。`onDidTerminateDebugSession` 受信時、**保持しているセッション ID と一致する場合のみ** (c) を停止する。

複数のデバッグセッションが同時に起動した場合、最初に (c) を起動させたセッションに紐付ける。以降の新規セッションは無視する（(c) は1つのアップストリームのみをプロキシする設計であるため）。

### 通常フロー

```text
[ユーザー] VS Code でデバッグ開始
    ↓
(b) onDidStartDebugSession 受信
    ↓
(b) セッション ID を保持
    ↓
(b) ポート検知（§6 参照）
    ↓
(b) easyauth-emulator --upstream-port <PORT> を起動  →  状態: starting
    ↓
(b) stdout で "All processes started" を検出        →  状態: running
    ↓                                 （30秒以内に検出できない場合 → 状態: error）
[ユーザー] 開発・テスト
    ↓
[ユーザー] VS Code でデバッグ停止
    ↓
(b) onDidTerminateDebugSession 受信（保持セッション ID と照合）
    ↓
(b) (c) のプロセスを終了                            →  状態: stopped
```

### ポート変更フロー（競合等で起動ポートが変わった場合）

```text
(b) 新しいポートを検知（前回と異なる）
    ↓
(b) (c) の旧プロセスを終了                          →  状態: stopped
    ↓
(b) easyauth-emulator --upstream-port <NEW_PORT> を起動  →  状態: starting
```

### 異常終了フロー

```text
(c) プロセスが非ゼロ終了コードで終了                →  状態: error
    ↓
(b) VS Code に通知を表示（"Output を開く" ボタン付き）
```

### 子プロセスのクリーンアップ

(b) がエミュレータープロセスを停止する際、`oauth2-proxy` の子プロセスも含めてプロセスツリー全体を終了させる。

- **Windows：** `taskkill /F /T /PID <PID>` でプロセスツリーを強制終了
- **Linux：** `kill -TERM <PID>` でシグナルを送信

(b) はエミュレーターの親プロセスに対してのみ終了操作を行えばよく、`oauth2-proxy` を個別に管理する必要はない。

---

## 6. ポート検知仕様

### Step 0: 言語・フレームワーク検知（キャッシュ）

ポート検知の前提として、ワークスペース内のファイルから開発言語・フレームワークを判定する。
**ワークスペースごとに一度だけ実行し、結果をキャッシュする。**
ポート検知が失敗してユーザー確認 UI に至った場合のみ、再検知を促す。

| 検出ファイル | 判定結果 |
| --- | --- |
| `*.csproj` / `launchSettings.json` | .NET |
| `pom.xml` / `build.gradle` | Java (Spring Boot) |
| `package.json` | Node.js |
| `requirements.txt` / `pyproject.toml` / `*.py` | Python |
| 上記なし | 不明（汎用フォールバック） |

複数該当する場合は `launch.json` の `type` フィールドで補完する。

### Steps 1〜6: ポート取得の優先順位

以下の順で試み、取得できた時点で確定する。

| 優先度 | 取得元 | 詳細 |
| --- | --- | --- |
| 1 | 拡張機能設定（手動指定） | `easyauth.upstreamPort`（null = 自動） |
| 2 | `launch.json` | `env.PORT` / `env.ASPNETCORE_URLS` / `env.ASPNETCORE_HTTP_PORTS` / `applicationUrl` |
| 3 | フレームワーク固有設定ファイル | 下表参照 |
| 4 | stdout 解析 | デバッグ出力イベントをフレームワーク別パターンで解析（下表参照） |
| 5 | ポートスキャン | 上記すべてで取得できない場合のフォールバック |
| 6 | ユーザー確認 UI | スキャンで曖昧、またはスキャン起点が不明な場合 |

#### 複数 URL・複数ポートがある場合の選択規則

`ASPNETCORE_URLS` 等で複数の URL が列挙されている場合、以下の優先順位で1つを選択する。

1. `http://` を `https://` より優先する（ローカル開発では http で十分なため）
2. 同スキームが複数ある場合は先頭のものを使用する

例: `https://localhost:7000;http://localhost:5000` → `5000` を採用

#### フレームワーク固有設定ファイル（優先度 3）

| フレームワーク | ファイル | キー |
| --- | --- | --- |
| .NET | `launchSettings.json` | `applicationUrl` |
| Spring Boot | `application.properties` | `server.port` |
| Spring Boot | `application.yml` | `server.port` |
| Node.js / Python | `.env` | `PORT` |

#### stdout 解析パターン（優先度 4）

デバッグアダプターが OutputEvent を公開している場合に利用。公開していない場合はこのステップをスキップする。

| フレームワーク | 検出パターン（正規表現） |
| --- | --- |
| .NET | `Now listening on: https?://[^:]+:(\d+)` |
| Spring Boot | `Tomcat started on port.? (\d+)` |
| Node.js / Express | `listening on.*port (\d+)` |
| Flask | `Running on http://[^:]+:(\d+)` |
| FastAPI / Uvicorn | `Uvicorn running on https?://[^:]+:(\d+)` |

#### ポートスキャン仕様（優先度 5）

- **スキャン起点：** `easyauth.portScanBase`（`null` の場合はスキャンをスキップして優先度 6 へ）
- **スキャン範囲：** 起点から `easyauth.portScanMax`（デフォルト: 5）ポート分
- **方法：** 起点ポートから連続して TCP 接続を試み、応答があったポートを候補とする
- **誤検知対策：** スキャン範囲を最大5ポートに絞ることで誤検知リスクを低減する。複数候補が残る場合はユーザー確認 UI（優先度 6）で解消する。

#### ユーザー確認 UI（優先度 6）

| 状況 | UI |
| --- | --- |
| 候補が 1 つ | 自動適用 |
| 候補が複数 | `showQuickPick` でユーザーに選択させる |
| 候補が 0、またはスキャン起点が不明 | `showInputBox` でポートの手動入力を促す |

---

## 7. 設定仕様

### (b) 拡張機能設定（VS Code settings.json）

| キー | 型 | デフォルト | 説明 |
| --- | --- | --- | --- |
| `easyauth.autoStart` | boolean | `true` | デバッグ開始時に自動起動 |
| `easyauth.autoStop` | boolean | `true` | デバッグ終了時に自動停止 |
| `easyauth.upstreamPort` | number \| null | `null` | ポート手動指定（null = 自動検知） |
| `easyauth.portScanMax` | number | `5` | ポートスキャンの最大試行数 |
| `easyauth.portScanBase` | number \| null | `null` | スキャン起点（ヒントが取れない場合） |
| `easyauth.verbose` | boolean | `false` | 起動時に全設定値を出力（シークレットはマスク） |

上記のほか、IDP 設定（`easyauth.entra.*` / `easyauth.google.*` 等）、サイト設定（`easyauth.site.*`）、TLS 設定（`easyauth.tls.*`）、oauth2-proxy 設定（`easyauth.oauth2proxy.*`）など多数の設定が存在する。詳細は拡張機能の Configuration Reference を参照。

> **バイナリパスについて:** 拡張機能は VSIX に同梱したバイナリを使用する。カスタムパス指定は不要。
>
> **設定ファイルについて:** 拡張機能は常に `.vscode/easyauth.toml` を `--config` に渡す。ファイルが存在する場合はベース設定として読み込み、存在しない場合はプロジェクトルートの `config.toml` を誤検知しないよう自動探索を抑制する。

### (c) 設定ファイル

設定ファイルは `config.toml` をサポートする。

| 条件 | 動作 |
| --- | --- |
| `config.toml` が存在する | `config.toml` をベース設定として読み込む |
| 環境変数に設定がある | `config.toml` の値を上書きする（優先） |
| 両方なし | 警告を出力して続行（IDP 設定がないためすぐ失敗） |

> (b) は常に `--config .vscode/easyauth.toml` を渡す。ファイルが存在する場合はベース設定として読み込み、存在しない場合はプロジェクトルートの `config.toml` を誤検知しないよう自動探索を抑制する。IDP 設定・サイト設定は環境変数として渡され、設定ファイルの値を上書きする。

---

## 8. シークレット管理

クライアントシークレットおよびクッキー署名鍵は、VS Code SecretStorage API を通じて**クライアント側**に保存する — デスクトップ版 VS Code ではプラットフォーム標準のセキュアストア（OS キーチェーン）、Web クライアント（vscode.dev）ではブラウザのストレージ。`settings.json` やリモートホストには保存されない。ストアはクライアントごとに独立しているため、クライアントを切り替えた場合（デスクトップ版 ⇔ vscode.dev など）はシークレットの再入力が必要になる。

### 保存キー

| キー | 内容 | 生成タイミング |
| --- | --- | --- |
| `easyauth\|{workspaceUri}\|{idpKey}` | IdP のクライアントシークレット | **Set Client Secret** コマンドによるユーザー操作 |
| `easyauth\|{workspaceUri}\|__cookieSecret__` | oauth2-proxy 共通クッキー署名鍵 | 初回起動時に自動生成（16 バイトランダム、Base64） |

`workspaceUri` はワークスペースフォルダの URI（例: `file:///c:/Users/user/myproject`）。`idpKey` は組み込み IdP のキー（`entra` / `google` / `facebook` / `apple` / `github`）またはカスタム IdP の `custom:{name}`。

### 既知の制限

SecretStorage のキーはワークスペースフォルダの URI を含む。**プロジェクトディレクトリを削除・移動・リネームすると、保存済みのシークレットが孤立する。** 孤立したシークレットは **Clear Client Secret** コマンドでは削除できず、プラットフォームのキーチェーン管理ツールから手動で削除する必要がある。

---

## 9. CLI インターフェース仕様（(b) → (c) 制御）

```text
easyauth-emulator [オプション]

オプション:
  --app-upstream URL     APP_UPSTREAM を上書き（例: http://localhost:3000）
  --config PATH          設定ファイルのパスを指定（デフォルト: ./config.toml）
  --verbose / -v         起動時に全設定値を出力（シークレットはマスク）
```

> **(b) からの制御は環境変数経由:** (b) は `APP_UPSTREAM` を含む IDP 設定・サイト設定・アップストリーム設定をすべて環境変数として渡す。(c) は `IDP_*`・`SITE_*`・`APP_*`・`OAUTH2_PROXY_*` プレフィックスの環境変数を設定ファイルより優先して読み込む。`--app-upstream` は (c) を単体で使う場合のオプションである。

### 動作例

config.toml に `APP_UPSTREAM = "http://localhost:3000"` が設定されている状態で:

```sh
easyauth-emulator --app-upstream http://localhost:8081
```

→ `http://localhost:8081` を APP_UPSTREAM として動作する。

---

## 10. VS Code ステータスバー

| 状態 | 表示例 | クリック動作 |
| --- | --- | --- |
| `stopped` | `$(shield) EasyAuth: stopped` | ポート検知して起動 |
| `unconfigured` | `$(warning) EasyAuth: no config` | 拡張機能の設定画面を開く |
| `missing_secret` | `$(lock) EasyAuth: secret missing`（黄色背景） | クライアントシークレット入力ポップアップを表示 |
| `missing_entra_issuer` | `$(warning) EasyAuth: Entra issuer missing`（黄色背景） | ワークスペース設定の `easyauth.entra.oidcIssuerUrl` を開く |
| `starting` | `$(sync~spin) EasyAuth: starting...` | Output Channel を開く |
| `running` | `$(shield) EasyAuth: 8080:8081`（リッスンポート:アップストリームポート） | ブラウザでエミュレーターを開く |
| `error` | `$(error) EasyAuth: error` | 1回目: Output Channel を開く / 2回目以降: ポート検知して再起動 |

---

## 11. コマンドパレットコマンド

| コマンド | 説明 |
| --- | --- |
| `EasyAuth Emulator: Start` | ポート検知してエミュレーターを手動起動 |
| `EasyAuth Emulator: Stop` | エミュレーターを停止 |
| `EasyAuth Emulator: Restart` | エミュレーターを再起動 |
| `EasyAuth Emulator: Open Output` | Output Channel を開く |
| `EasyAuth Emulator: Open in Browser` | ブラウザでエミュレーターを開く |
| `EasyAuth Emulator: Set Client Secret` | IDP クライアントシークレットを SecretStorage に保存 |
| `EasyAuth Emulator: Clear Client Secret` | 保存済みクライアントシークレットを削除 |

---

## 12. 将来拡張

| 対象 | 拡張機能の流用 | 方針 |
| --- | --- | --- |
| Cursor / Codex | 可（VS Code 互換 API） | (b) をそのまま流用 |
| Visual Studio | 不可（拡張モデルが異なる） | C#/.NET VSIX として別途実装。(c) への制御は CLI で同様 |
| Eclipse | 不可 | Java プラグインとして別途実装。(c) への制御は CLI で同様 |
| その他 IDE | 不可 | CLI または設定ファイル手動編集での単体利用を基本とする |
