# AWS Environment Scanning - Authentication Guide

`env_inventory.py --aws` uses boto3 (`pip install boto3`) and tries credentials in this order:

1. **Explicit keys** - `--aws-access-key`, `--aws-secret-key` and optionally `--aws-session-token`.
2. **AWS CLI / environment credentials** - `aws configure`, `~/.aws/credentials`, `AWS_PROFILE`,
   or the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` environment variables.
3. **IAM role** - an EC2 instance profile (or other role credentials supplied by the environment).

Prefer 2 or 3. Command-line keys can leak through shell history and process listings.

```bash
aws configure
python env_inventory.py --aws --aws-region eu-west-1
```

## Required IAM permissions

The scanner only reads. Grant just the actions for the services you scan:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "lambda:ListFunctions",
        "ecs:ListClusters",
        "ecs:ListTasks",
        "ecs:DescribeTasks",
        "ecs:DescribeTaskDefinition",
        "sagemaker:ListNotebookInstances",
        "sagemaker:ListTrainingJobs",
        "sagemaker:ListEndpoints"
      ],
      "Resource": "*"
    }
  ]
}
```

Skip a service you have no permission for with `--no-aws-ec2`, `--no-aws-lambda`,
`--no-aws-ecs` or `--no-aws-sagemaker`.

## Regions

`us-east-1` is scanned by default. Repeat `--aws-region` to scan several regions.

## Troubleshooting

- **"boto3 is not installed"** - `pip install boto3`.
- **"No AWS credentials found"** - run `aws configure`, set the environment variables, or pass keys.
- **"<service> discovery failed ... AccessDenied"** - add the missing action above, or skip the service.
- **No environments found** - check the region; EC2 only lists running instances, and ECS only lists running tasks.
- **Expired temporary credentials** - refresh them and pass a current `--aws-session-token`.

## Security

Use least-privilege read-only credentials, never commit keys, and prefer roles or short-lived
session tokens in CI/CD.
