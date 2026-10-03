# beads-s3-skill

[English](README.md)

ClaudeやChatGPTのサンドボックス環境で [beads](https://github.com/gastownhall/beads)（`bd`）を使うためのAgent Skillです。サンドボックスはセッションごとに破棄されるため、課題データベースをS3互換バケットに保存して永続化します。

beads本体のスキルにセットアップスクリプトを追加した構成になっており、セットアップが完了すればエージェントは通常通り `bd` を利用できます。

## 仕組み

- 各プロジェクトのデータベースは、単一のgitバンドル（`s3://<bucket>/<prefix><project>.bundle`）として保存されます。
- `git-remote-s3bundle` がこのオブジェクト用のgitリモートヘルパーとして機能します。bdのDoltバックエンドは標準でgitリモートに対応しているため、`bd dolt push`、`bd dolt pull`、`bd bootstrap` といった操作がそのままバケットに対して動作します。
- セットアップ時にbdの `dolt.auto-push` を有効化するため、書き込みのたびに自動でバケットへpushされます。
- アップロード時は条件付き書き込みを行い、読み込み時から対象オブジェクトが変更されていないか検証します。途中で別の場所からpushされていた場合は "fetch first" で失敗するため、`bd dolt pull` で差分を取得してマージできます。条件付きリクエストヘッダーを受け付けないストレージでは、アップロード直前にETagを比較する方式にフォールバックします。
- バケットで使用するbdのバージョンは `<prefix>bd/VERSION` に記録され、リリースアーカイブもバケット内にキャッシュされます。これにより、全セッションで同じバージョンのbdが使われ、GitHubから毎回ダウンロードし直す手間を省きます。

バケットのオブジェクトバージョニングを有効にしておくと、pushごとに履歴が残り、過去のバージョンへいつでもロールバックできます。

## 必要なもの

- `bash`、`git`、`python3` が利用可能なサンドボックス（`boto3` が未導入の場合はセットアップ時に自動インストールされます）。
- バケットのエンドポイントへのネットワーク接続。初回セッション時のみ、bdをダウンロードするためにGitHub（またはnpm）へのアクセスも必要です。
- S3互換バケットと、読み込み・書き込み・一覧取得（List）権限を持つアクセスキー。

## セットアップ

1. バケットを作成し、そのバケットのみに権限を絞ったアクセスキーを用意します。
2. `skills/beads-s3-skill/auth/credentials.example.json` を同じディレクトリに `credentials.json` としてコピーし、接続設定を入力します。
3. `skills/beads-s3-skill` フォルダをzip圧縮するか、Releasesの `.skill` ファイルに `credentials.json` を追加して、AIアプリにアップロードします。

| キー | 必須 | 内容 |
| --- | --- | --- |
| `endpoint_url` | ○ | S3 APIのエンドポイント（例: `https://s3.example.com`） |
| `region` | ○ | リクエスト署名に使用するリージョン名 |
| `bucket` | ○ | バケット名 |
| `access_key_id`, `secret_access_key` | ○ | アクセスキー |
| `prefix` | | スキルが保存する全キーのプレフィックス。デフォルトは `beads/` |
| `default_project` | | プロジェクト名を指定しない場合に使用する名前。デフォルトは `beads` |
| `addressing_style` | | `path`（デフォルト）または `virtual` |
| `conditional_writes` | | `auto`（デフォルト）、`always`、`never` |
| `git_user_name`, `git_user_email` | | サンドボックスにGit設定がない場合に使用する識別情報。デフォルトは `beads` / `beads@localhost` |

## 使い方

エージェントにbeadsで作業管理を行うよう指示すると、セッション開始時に以下のスクリプトが実行されます。

```bash
bash scripts/setup.sh [project]
```

bdのインストール、バケットからのプロジェクト復元（未作成の場合は初期化）、および `bd prime` の出力が行われます。指定したプロジェクト名は課題IDのプレフィックス（`<project>-xxx`）としても使われます。

バケット内の全プロジェクトを新しいbdリリースへ移行する場合:

```bash
bash scripts/setup.sh --upgrade [version]   # 省略時は最新リリース
```

各プロジェクトを新リリースで復元し、`bd migrate --force` と `bd dolt push` を実行した上で `<prefix>bd/VERSION` を更新します。これ以降に起動したセッションは自動で新しいリリースを使用します。

## バケットの構成

```
<prefix>
├── bd/
│   ├── VERSION                       # このバケットで使うbdのリリース
│   └── <version>/<os>-<arch>.tar.gz  # キャッシュしたリリースのアーカイブ
└── <project>.bundle                  # プロジェクトのデータベース（gitバンドル）
```

## 手元のマシンから同じバケットを使う

ローカル環境のbd（Claude Codeなど）から同じバケットのプロジェクトを共有して利用できます。

1. `skills/beads-s3-skill/scripts/git-remote-s3bundle` を `PATH` の通った場所に配置し、`boto3` をインストールします。
2. 設定を `~/.config/beads-s3/config.json`（`credentials.json` と同じ形式）に保存するか、環境変数 `BEADS_S3_CONFIG` でファイルパスを指定します。
3. `git config --global url.s3bundle://.insteadOf https://s3bundle.invalid/` を実行します。
4. プロジェクト内で `bd dolt remote add origin git+https://s3bundle.invalid/<bucket>/<prefix><project>.bundle` を実行します。

ローカルのbdは `<prefix>bd/VERSION` に指定されたバージョンを使用してください。bdは、自身より新しいスキーマのデータベースを開くことができません。

## ライセンス

MIT。beadsから取り込んだスキル関連のファイルには、beads本来のMITライセンスが適用されます。[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照してください。
