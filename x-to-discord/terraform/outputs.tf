output "lambda_function_name" {
  value = aws_lambda_function.this.function_name
}

output "eventbridge_rule_name" {
  value = aws_cloudwatch_event_rule.schedule.name
}

output "ssm_param_bearer_token_name" {
  value       = var.ssm_param_bearer_token
  description = "Create this SecureString parameter manually before the first deploy"
}

output "ssm_param_webhook_url_name" {
  value       = var.ssm_param_webhook_url
  description = "Create this SecureString parameter manually before the first deploy"
}
