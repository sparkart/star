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

# Upload via boto3. Star Automation has its own bucket; never inherit the
# generic R2 bucket because that credential set may point at another project.
bucket = os.environ.get('STAR_R2_BUCKET', 'star').strip()
if bucket != 'star':
    raise RuntimeError("STAR_R2_BUCKET must be 'star'")

s3 = boto3.client(
    's3',
    endpoint_url=os.environ['R2_ENDPOINT'],
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    region_name='auto',
)

s3.upload_file(
    '/tmp/manifest_v2.json',
    bucket,
    'star/manifest.json',
    ExtraArgs={'ContentType': 'application/json'}
)
print(f"Upload to bucket {bucket} successful!")
