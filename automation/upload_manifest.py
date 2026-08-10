#!/usr/bin/env python3
"""Upload manifest to R2 using boto3."""
import boto3
import os
import json

# Verify the file
with open('/tmp/manifest_v2.json') as f:
    manifest = json.load(f)
    print(f"Manifest has {len(manifest['days'])} days")
    total = sum(len(d.get('captions', {})) for d in manifest['days'])
    print(f"Total caption sets: {total}")

# Upload via boto3
s3 = boto3.client(
    's3',
    endpoint_url=os.environ['R2_ENDPOINT'],
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    region_name='auto',
)

s3.upload_file(
    '/tmp/manifest_v2.json',
    'qrf',
    'star/manifest.json',
    ExtraArgs={'ContentType': 'application/json'}
)
print("Upload successful!")
