DOCKER_IMAGE=$(echo "$DOCKER_IMAGE" | sed 's/[\/:]/_/g')

tfs --no-colors transform nice raw.log nice.log.txt
tfs --no-colors report results -a "https://s3.amazonaws.com/$S3_BUCKET/$PR_NUMBER/$COMMIT_SHA/grype/$DOCKER_IMAGE" raw.log - --copyright "Altinity LTD" | tfs --no-colors document convert > report.html

S3_PATH="s3://$S3_BUCKET/$PR_NUMBER/$COMMIT_SHA/grype/$DOCKER_IMAGE"
aws s3 cp --no-progress nice.log.txt $S3_PATH/nice.log.txt --content-type "text/plain; charset=utf-8" || echo "nice log file not found".
aws s3 cp --no-progress report.html $S3_PATH/report.html || echo "results file not found".
aws s3 cp --no-progress raw.log $S3_PATH/raw.log || echo "raw.log file not found".
aws s3 cp --no-progress result.json $S3_PATH/result.json --content-type "text/plain; charset=utf-8" || echo "result.json not found".