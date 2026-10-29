terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.28.0+ adds the permissions a public Function URL needs (see aws_lambda_function_url).
      version = "~> 6.28"
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

# Produced by ../build.sh (handler + PyNaCl built for Lambda's platform). Run it before apply.
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/build/package"
  output_path = "${path.module}/build/lambda.zip"
}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  region        = data.aws_region.current.region
  function_arn  = "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.function_name}"
  bot_token_arn = "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.ssm_param_bot_token}"
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

# The bot token parameter is created manually (see README) so its plaintext never
# passes through Terraform state; this policy only grants read access by name.
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
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.function_name}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = local.bot_token_arn
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = data.aws_kms_alias.ssm.target_key_arn
      },
      {
        # The interaction handler answers Discord within 3 seconds, then re-invokes
        # this same function asynchronously to do the slow crawl.
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = local.function_arn
      },
    ]
  })
}

resource "aws_lambda_function" "this" {
  function_name    = var.function_name
  role             = aws_iam_role.lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["x86_64"]
  # Lambda's ceiling, matching the 15-minute life of the interaction token. The handler
  # stops crawling a minute early so a partial result is still delivered.
  timeout          = 900
  # Lambda CPU scales with memory; 256 MB shortens the cold start (boto3 + PyNaCl import)
  # that must fit Discord's 3-second reply window. The cost difference is negligible here.
  memory_size      = 256
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      GUILD_ID            = var.guild_id
      DISCORD_PUBLIC_KEY  = var.discord_public_key
      SSM_PARAM_BOT_TOKEN = var.ssm_param_bot_token
    }
  }
}

# A retried crawl would only post a duplicate (or fail on an expired token).
resource "aws_lambda_function_event_invoke_config" "this" {
  function_name                = aws_lambda_function.this.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 600
}

# Discord's Interactions Endpoint. Authentication is the Ed25519 signature check in the handler.
#
# Function URLs created since October 2025 answer 403 unless the function policy grants both
# lambda:InvokeFunctionUrl and lambda:InvokeFunction (InvokedViaFunctionUrl=true). With
# authorization_type NONE, provider >= 6.28 adds both on creation, so no aws_lambda_permission
# is declared here. Those statements are NOT removed on destroy; delete them by hand if needed.
resource "aws_lambda_function_url" "this" {
  function_name      = aws_lambda_function.this.function_name
  authorization_type = "NONE"
}
