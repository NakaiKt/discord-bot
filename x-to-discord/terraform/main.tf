terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/build/lambda.zip"
}

locals {
  ssm_arn_prefix   = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter"
  bearer_token_arn = "${local.ssm_arn_prefix}${var.ssm_param_bearer_token}"
  webhook_url_arn  = "${local.ssm_arn_prefix}${var.ssm_param_webhook_url}"
  state_param_arn  = "${local.ssm_arn_prefix}${var.ssm_param_state}"
}

resource "aws_iam_role" "lambda" {
  name = "${var.function_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Bearer token and webhook URL parameters are created manually (see README Step 4) so their
# plaintext values never pass through Terraform state; this policy only grants read access by name.
resource "aws_iam_role_policy" "lambda" {
  name = "${var.function_name}-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.function_name}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [local.bearer_token_arn, local.webhook_url_arn, local.state_param_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:PutParameter"]
        Resource = [local.state_param_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = data.aws_kms_alias.ssm.target_key_arn
      },
    ]
  })
}

resource "aws_lambda_function" "this" {
  function_name    = var.function_name
  role             = aws_iam_role.lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  # Headroom for the Discord 429 retry/backoff path; billed on actual duration, so the
  # larger ceiling costs nothing on normal runs.
  timeout          = 90
  memory_size      = 128
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      X_USER_ID                     = var.x_user_id
      SSM_PARAM_X_BEARER_TOKEN      = var.ssm_param_bearer_token
      SSM_PARAM_DISCORD_WEBHOOK_URL = var.ssm_param_webhook_url
      SSM_PARAM_STATE               = var.ssm_param_state
    }
  }
}

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.function_name}-schedule"
  schedule_expression = var.polling_schedule
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = aws_lambda_function.this.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}
