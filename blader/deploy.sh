#!/usr/bin/env bash
set -euo pipefail

# Deploy blader to AWS Lambda.

LAMBDA_FUNCTION="${LAMBDA_FUNCTION:-blader-agent}"
AWS_REGION="${AWS_REGION:-us-west-2}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/.build"
ZIP_FILE="$BUILD_DIR/lambda.zip"

echo "Packaging $LAMBDA_FUNCTION..."

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/package"

# Copy mailroom framework
cp -r "$SCRIPT_DIR/../mailroom" "$BUILD_DIR/package/mailroom"

# Copy blader package
cp -r "$SCRIPT_DIR" "$BUILD_DIR/package/blader"
rm -rf "$BUILD_DIR/package/blader/.build"

# Copy Lambda entry point to root
cp "$SCRIPT_DIR/lambda_function.py" "$BUILD_DIR/package/lambda_function.py"

# Build zip
cd "$BUILD_DIR/package"
zip -r "$ZIP_FILE" . -q -x '*.pyc' '*__pycache__*' '*.build*'

echo "Package: $ZIP_FILE ($(du -h "$ZIP_FILE" | cut -f1))"

# Deploy
echo "Deploying to $LAMBDA_FUNCTION in $AWS_REGION..."
aws lambda update-function-code \
    --function-name "$LAMBDA_FUNCTION" \
    --zip-file "fileb://$ZIP_FILE" \
    --region "$AWS_REGION" \
    --output text \
    --query 'FunctionArn'

echo "Done."
