# カスタム絵文字 使用率ボット

Discordで `/emoji-stats` と打つと、指定期間に投稿されたメッセージでのカスタム絵文字の使用回数と構成比（本文での使用＋現在のリアクション数）を返すボット。

- 常時起動サーバーは使わず、Discordのスラッシュコマンド → Lambda Function URL のサーバーレス構成。
- 指定した1サーバーでのみ動く。既定では管理者だけが実行でき、結果は実行者だけに見える。

構成:
```
emoji-stats/
  src/handler.py                 Lambdaハンドラ本体
  scripts/register_commands.py   スラッシュコマンド登録
  build.sh                       Lambdaパッケージのビルド
  terraform/                     インフラ定義（Lambda / Function URL / IAM）
```

このボットは他のボットとディレクトリ・デプロイ単位を共有しない。単独でデプロイ・運用できる。

---

## 前提条件

- Python 3（`pip` が使えること）
- Terraform 1.5以上
- AWS CLI（デプロイ先アカウントの認証済み）

---

## 導入方法

### Step 1. Discordアプリケーションを作る

1. Discord Developer Portal で「New Application」を作成する。
2. 「General Information」の **Application ID** と **Public Key** を控える。
3. 「Bot」で **Reset Token** を押し、Botトークンを控える。
4. 同じく「Bot」の Privileged Gateway Intents で **Message Content Intent** をONにする。
   OFFのままだと本文中の絵文字が数えられない（リアクションだけになる）。

### Step 2. サーバーに招待する

1. 「OAuth2」→「URL Generator」で scopes に `bot` と `applications.commands` を選ぶ。
2. Bot Permissions で **View Channels** と **Read Message History** だけを選ぶ。
3. 生成されたURLを開き、対象サーバーに招待する。

閲覧権限のないチャンネルは集計からスキップされる。

### Step 3. BotトークンをSSM Parameter Storeに配置する

**BotトークンはTerraformでは作成しない。** tfstateに平文で残さないため、手動でSecureStringとして登録する。

```bash
aws ssm put-parameter --name "/emoji-stats/discord-bot-token" --type "SecureString" --value "<DISCORD_BOT_TOKEN>"
```

### Step 4. デプロイ

```bash
cd emoji-stats
./build.sh

cd terraform
cp terraform.tfvars.example terraform.tfvars
# terraform.tfvars に guild_id（サーバーID）と discord_public_key（Step 1 の Public Key）を設定する

terraform init
terraform apply
```

`apply` の出力に `interactions_endpoint_url` が表示される。

### Step 5. Interactions Endpoint URL を設定する

1. Developer Portal の「General Information」→ **Interactions Endpoint URL** に、Step 4 の `interactions_endpoint_url` を貼る。
2. 「Save Changes」を押す。Discordがこの時点で疎通と署名検証を確認し、失敗すると保存できない。

### Step 6. スラッシュコマンドを登録する

```bash
cd emoji-stats
export DISCORD_APPLICATION_ID="<APPLICATION_ID>"
export DISCORD_GUILD_ID="<SERVER_ID>"
export DISCORD_BOT_TOKEN="$(aws ssm get-parameter --name /emoji-stats/discord-bot-token \
  --with-decryption --query 'Parameter.Value' --output text)"
python3 scripts/register_commands.py
unset DISCORD_BOT_TOKEN
```

`Registered /emoji-stats` と表示されれば完了。

### Step 7. 動作確認

対象サーバーの任意のチャンネルで `/emoji-stats` を実行する。「考え中…」の表示が、数十秒〜数分で集計結果に置き換われば完了。

---

## 使い方

```
/emoji-stats                 直近30日を集計し、自分だけに表示
/emoji-stats days:7          直近7日（1〜90）
/emoji-stats public:True     結果をチャンネルの全員に表示
```

実行できる人・チャンネルは、Discordの「サーバー設定」→「連携サービス」→ このアプリ から変更できる（再デプロイ不要）。

## 集計ルール

- 期間はメッセージの投稿日で判定する。リアクションは、期間内の投稿に**現在付いている数**を数える（Discord APIはリアクションの付与日時を返さない）。
- 「使用率」は集計した絵文字使用数の総和に対する構成比。
- 対象はこのサーバーの現在登録されているカスタム絵文字のみ。削除済み絵文字、Unicode絵文字、他サーバーの絵文字は数えない。
- 本文での使用は、Bot・Webhookの投稿を除いて数える。
- リアクションは、Botの投稿についたものも数える。
- 対象はテキスト・アナウンス・ボイス/ステージのチャット・アクティブなスレッド。アーカイブ済みスレッドは集計しない。
- 権限不足や通信エラーで読めなかったチャンネルはスキップし、その数を結果に表示する。
- 実行から約14分で打ち切り、そこまでの部分結果を返す（フッターに「時間切れ」と表示される）。

## コードを変更したとき

```bash
cd emoji-stats && ./build.sh && cd terraform && terraform apply
```

コマンドの定義（`scripts/register_commands.py` の `COMMANDS`）を変えたときは Step 6 を再実行する。

## トラブルシューティング

実行ログは CloudWatch Logs の `/aws/lambda/emoji-stats` にある。

| 症状 | 原因候補 |
|---|---|
| Step 5 で URL が保存できない | `discord_public_key` の値違い、または `build.sh` を実行せずに `apply` した |
| 「このアプリケーションは応答しませんでした」 | Lambdaの起動失敗。ログの例外を確認する |
| 結果が「集計に失敗しました」 | BotトークンのSSM名・値の誤り、Botがサーバーにいない |
| 本文の使用回数が常に0 | Message Content Intent がOFF |
| コマンドが一覧に出ない | Step 6 未実行、または実行者が管理者でない |

## テスト

```bash
pip install -r emoji-stats/requirements.txt
python3 -m unittest discover -s emoji-stats/tests -v
```

AWS・Discordには接続せず、モックで検証する。署名検証のテストだけは実際のPyNaClを使う。
