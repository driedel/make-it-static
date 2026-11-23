#!/bin/bash
# Dev-only: create the S3 bucket in LocalStack and allow anonymous reads so the
# nginx preview can serve uploaded sites without signing requests. Mounted into
# LocalStack at /etc/localstack/init/ready.d/ — it runs once S3 is available.
awslocal s3 mb "s3://${S3_BUCKET}" || true
awslocal s3api put-bucket-policy --bucket "${S3_BUCKET}" --policy "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":\"*\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::${S3_BUCKET}/*\"}]}"
echo "Bucket ready."
