# discord-bot

複数のDiscord向けボットを収容するリポジトリ。各ボットは独立したディレクトリを持ち、
単独でデプロイ・運用できる（インフラ・依存・デプロイ単位を共有しない）。

## ボット一覧

- [x-to-discord](./x-to-discord/) — 固定のXアカウントの新規ポストをDiscordチャンネルへ転送するボット。
- [emoji-stats](./emoji-stats/) — `/emoji-stats` でサーバーのカスタム絵文字の使用率を返すボット。
