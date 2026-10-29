output "interactions_endpoint_url" {
  value       = aws_lambda_function_url.this.function_url
  description = "Paste into Developer Portal > General Information > Interactions Endpoint URL"
}

output "lambda_function_name" {
  value = aws_lambda_function.this.function_name
}

output "ssm_param_bot_token_name" {
  value       = var.ssm_param_bot_token
  description = "Create this SecureString parameter manually before the first deploy"
}
