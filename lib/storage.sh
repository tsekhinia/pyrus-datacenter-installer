#!/usr/bin/env bash
configure_storage() {
    if [[ -f "$STATE_DIR/storage.done" ]]; then return; fi
    local endpoint access secret provider
    if [[ "$STORAGE_TYPE" == minio ]]; then
        quiet_run dc up -d minio
        wait_for MinIO 300 service_ready minio
        endpoint=http://pyrus-minio:9000
        access=$MINIO_USER; secret=$MINIO_PASSWORD; provider=MinIo
    else
        endpoint=$S3_ENDPOINT; access=$S3_ACCESS_KEY; secret=$S3_SECRET_KEY; provider=$S3_STORAGE_TYPE
    fi
    guarded_step storage setup --configure-s3 --bucket "$S3_BUCKET" --accessKey "$access" \
        --secretKey "$secret" --endpointUrl "$endpoint" --storageType "$provider" \
        --forcePathStyle "$S3_FORCE_PATH_STYLE"
}
