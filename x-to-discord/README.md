# X → Discord ポスト転送ボット

固定のXアカウントが新しくポストするたびに、そのポストへのリンクをDiscordの特定チャンネルへ自動送信するボット。

- 監視対象は1アカウント固定。リプライ・リポストは送らない。引用ポストは送る。
- 常時起動サーバーは使わず、EventBridge（5分間隔） → Lambda → Discord Webhook のサーバーレス構成。
- 状態（最後に送信済みのtweet_id）はSSM Parameter Storeに保存し、二重送信を防ぐ。

構成:
```
x-to-discord/
  src/handler.py       Lambdaハンドラ本体
  terraform/            インフラ定義（Lambda / EventBridge / IAM）
```

このボットは他のボットとディレクトリ・デプロイ単位を共有しない。単独でデプロイ・運用できる。

---

## 導入方法

### Step 1. Discord Webhookを用意する

1. 送信先にしたいDiscordサーバー・チャンネルを開く。
2. チャンネルの「設定（歯車）」→「連携サービス」→「ウェブフック」。
3. 「新しいウェブフック」を作成。名前・アイコンは任意。
4. 「ウェブフックURLをコピー」でURLを取得しておく。

### Step 2. X APIのクレデンシャルを用意する

1. X Developer Portal（developer.x.com）でプロジェクト／アプリを作成する。
2. **Bearer Token** を取得する。
3. 料金体系（pay-per-use、無料枠は廃止済み）を確認し、必要なら支払い設定を済ませる。
   - 参考（2026年時点、要最新確認）: 読み取り約 $0.005/件。本ボットは1アカウント・5分間隔の差分取得のみのため
     読み取り量は少ないが、正確な見積もりは実装・デプロイ時点の公式料金ページで必ず確認すること。

### Step 3. 監視対象の user_id を確定する

1. 監視したいアカウントの screen_name（@なしのユーザー名）を用意する。
2. X APIのユーザー参照エンドポイント（例: `GET /2/users/by/username/:username`）で screen_name → 数値 user_id を引く。
3. この数値IDを `terraform.tfvars` の `x_user_id` に設定する。

### Step 4. シークレットをSSM Parameter Storeに配置する

**Bearer TokenとDiscord Webhook URLはTerraformでは作成しない。** Terraformにplaintextの機密値を
渡すとtfstateに平文で残ってしまうため、AWS CLIやコンソールで直接、手動でSecureStringとして登録する。

```bash
aws ssm put-parameter --name "/x-to-discord/x-bearer-token" --type "SecureString" --value "<X_BEARER_TOKEN>"

aws ssm put-parameter --name "/x-to-discord/discord-webhook-url"  --type "SecureString" --value "<DISCORD_WEBHOOK_URL>"
```

パラメータ名を変えたい場合は `terraform/variables.tf` の `ssm_param_bearer_token` /
`ssm_param_webhook_url` と一致させること。

**この手順は必ずStep 5より前に実行すること。** SecureStringを一度も作成していないAWSアカウント／
リージョンでは `alias/aws/ssm`（SSMのデフォルトKMSキー）がまだ存在せず、Terraformの参照に失敗する。

state用パラメータ（`/x-to-discord/last-tweet-id`）は作成不要。Lambdaが初回実行時に自動作成する。

user_id は非機密のため、Lambda環境変数（`terraform.tfvars` の `x_user_id`）に平文で設定してよい。

これらの値・`terraform.tfvars` はGitHubにコミットしない（`.gitignore` 済み）。

### Step 5. デプロイ

```bash
cd x-to-discord/terraform
cp terraform.tfvars.example terraform.tfvars
# terraform.tfvars を編集して x_user_id を設定する

terraform init
terraform apply
```

Lambda実行ロールには、Step 4で作成したSSMパラメータの取得権限とKMS復号権限のみが最小権限で付与される。

デプロイ後、5分以内に初回のスケジュール実行が走る。**初回は送信されず、最新ポストのidが記録されるだけ**
（仕様通り。稼働開始前の過去ポストを大量送信しないため）。

### Step 6. 稼働確認

1. 対象アカウントが新しくポスト（通常ポストまたは引用ポスト）する。
2. 次回のスケジュール実行（最大5分後）で、該当チャンネルにリンクが届く。
3. 届いたリンクがそのポストに正しく飛ぶことを確認する。
4. リプライ・リポストが送られてこないことも合わせて確認する。
5. CloudWatch Logs（`/aws/lambda/x-to-discord-forwarder`）で実行結果を確認できる。

以上で、Xの固定アカウントのポストがDiscordチャンネルに自動転送される状態になる。

---

## 動作の仕組み

- 状態が未設定（初回）の場合、最新ポストのidだけを記録して終了する。何も送信しない。
- 2回目以降は `since_id` に保存済みidを渡し、差分ポストのみを取得する。
- X APIの `exclude=replies,retweets` でリプライ・リポストを除外する。このパラメータには
  引用ポストを除外する値は存在しないため、引用ポストは自然に取得対象へ残る。
- 取得したポストは古い順に並べ替え、1件送信するごとに状態（tweet_id）を更新する。
  送信途中でエラーが起きても、成功済み分は次回再送されない。
- X APIが429（レート超過）を返した場合、その回はスキップし状態を更新しない。
- Discord送信が失敗した場合も、その回はそこで打ち切り状態を更新しない（次回リトライで拾う）。
- Discordが429（レート超過）を返した場合は `Retry-After` に従って最大3回まで再送する。
  待ち時間が5秒を超える場合はLambdaのタイムアウトを避けるため再送せず、次回の実行に委ねる。
- 送信リクエストには明示的な `User-Agent` を付与する。urllibの既定値（`Python-urllib/x.y`）のままだと
  Discordのエッジ（Cloudflare）に403で拒否され、リクエストがWebhookまで到達しない。

## トラブルシューティング

### X APIは動いているがDiscordに何も届かず、ログにもエラーがない

まず「エラーがない」ことと「Discord送信処理まで到達した」ことを分けて確認する。Lambdaは各実行で
`Poll started`、X API取得後に `X API poll completed`、正常終了時に `Poll completed` を出力する。
`result_count=0` / `fetched=0` ならDiscordにはリクエストしておらず、WebhookではなくXの差分取得または
保存済みstateを調べる。`fetched` が1以上で `sent=0` なら `Discord send failed` の行を調べる。

以下の例では、リージョンやパラメータ名を実際の設定に合わせる。

```bash
# 1. EventBridgeから定期実行されているか（直近30分の開始・終了・診断ログ）
aws logs tail /aws/lambda/x-to-discord-forwarder --since 30m --format short

# 2. Lambdaを手動実行し、返り値のstatus/fetched/sentと新しいログを確認
aws lambda invoke --function-name x-to-discord-forwarder \
  --cli-binary-format raw-in-base64-out --payload '{}' /tmp/x-to-discord-result.json
cat /tmp/x-to-discord-result.json

# 3. 差分取得の基準になっているtweet_idを確認
aws ssm get-parameter --name /x-to-discord/last-tweet-id --query 'Parameter.Value' --output text

# 4. Lambdaが参照している環境変数（別リージョン・別パラメータ名の取り違えを確認）
aws lambda get-function-configuration --function-name x-to-discord-forwarder \
  --query '{State:State,LastUpdateStatus:LastUpdateStatus,Environment:Environment.Variables}'

# 5. EventBridgeルールとターゲットが有効か確認
aws events describe-rule --name x-to-discord-forwarder-schedule
aws events list-targets-by-rule --rule x-to-discord-forwarder-schedule
```

主な見分け方:

| 観測結果 | 原因候補 | 次に確認すること |
|---|---|---|
| `Poll started` 自体がない | EventBridge未実行、別リージョン/別ロググループを見ている | ルールの`State`、ターゲット、LambdaのMetrics `Invocations` |
| `First run: recorded ... without sending` | 初回起動の仕様 | そのログより後に作成した新規ポストで再確認 |
| `result_count=0` | 新規対象なし、ポストがreply/retweet、stateが最新より先、`x_user_id`違い | X APIレスポンスの`meta`、SSMのstate、対象投稿種別 |
| `fetched>0 sent>0` | Lambda上はDiscordのHTTP成功（通常204） | Webhookの送信先チャンネル、チャンネル表示/権限、別Webhook URLの取り違え |
| `status=discord_failed` | Discord HTTP/ネットワーク失敗 | 同じrequest ID付近の `Discord send failed` |

Webhook URLそのものを単独確認する場合は、SSMから取得した値を画面や履歴へ表示せずにテストする。

```bash
WEBHOOK_URL="$(aws ssm get-parameter --name /x-to-discord/discord-webhook-url \
  --with-decryption --query 'Parameter.Value' --output text)"
curl --fail-with-body -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  -H 'Content-Type: application/json' \
  -d '{"content":"x-to-discord webhook connectivity test"}' "$WEBHOOK_URL"
unset WEBHOOK_URL
```

成功時は `HTTP 204` になりテストメッセージが届く。これが成功しLambdaが `result_count=0` なら、原因は
Discord送信ではなく差分判定側にある。stateを削除すると次回実行は**初回扱いとなり、その時点の最新IDを
記録するだけで過去投稿は送信しない**ため、調査目的で安易に削除しない。

### Discord送信が403 Forbiddenで失敗する

CloudWatch Logsの `Discord send failed for tweet_id=...` の行に `status` / `server` / `body` が出るので、
本文で原因を切り分ける。

| ログの `body` | 原因 | 対処 |
|---|---|---|
| 空、またはHTMLに `error code: 1010` / `1020` 等 | エッジ（Cloudflare）でのブロック | `User-Agent` が付いているか確認する。付いていて再発する場合は送信元IP側の問題 |
| `{"message": "Missing Permissions", "code": 50013}` 等のJSON | Discord側の権限エラー | 対象チャンネルの権限・Webhookの設定を確認する |

なお、tokenが誤っている場合は401、Webhookが削除済み・ID誤りの場合は404（`10015 Unknown Webhook`）が返る。
403はこれらとは別の原因を示す。

## 設定変更

- ポーリング間隔: `terraform/variables.tf` の `polling_schedule`（EventBridge schedule expression）。
- SSMパラメータ名: `terraform/variables.tf` の `ssm_param_*` 系変数。
