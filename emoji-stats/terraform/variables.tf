variable "aws_region" {
  type    = string
  default = "ap-northeast-1"
}

variable "function_name" {
  type    = string
  default = "emoji-stats"
}

variable "guild_id" {
  type        = string
  description = "ID of the only Discord server allowed to run the command"
}

variable "discord_public_key" {
  type        = string
  description = "Application public key (hex) from the Developer Portal, used to verify request signatures. Not a secret."
}

variable "ssm_param_bot_token" {
  type        = string
  description = "SSM Parameter Store name holding the Discord bot token (SecureString, created manually before deploy)"
  default     = "/emoji-stats/discord-bot-token"
}
