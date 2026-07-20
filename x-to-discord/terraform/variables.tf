variable "aws_region" {
  type    = string
  default = "ap-northeast-1"
}

variable "function_name" {
  type    = string
  default = "x-to-discord-forwarder"
}

variable "x_user_id" {
  type        = string
  description = "Numeric X (Twitter) user ID to monitor (not the screen name)"
}

variable "ssm_param_bearer_token" {
  type        = string
  description = "SSM Parameter Store name holding the X API Bearer Token (SecureString, created manually before deploy)"
  default     = "/x-to-discord/x-bearer-token"
}

variable "ssm_param_webhook_url" {
  type        = string
  description = "SSM Parameter Store name holding the Discord Webhook URL (SecureString, created manually before deploy)"
  default     = "/x-to-discord/discord-webhook-url"
}

variable "ssm_param_state" {
  type        = string
  description = "SSM Parameter Store name the Lambda uses to persist the last sent tweet ID (managed entirely by the Lambda, not created by Terraform)"
  default     = "/x-to-discord/last-tweet-id"
}

variable "polling_schedule" {
  type        = string
  description = "EventBridge schedule expression controlling poll frequency"
  default     = "rate(5 minutes)"
}
