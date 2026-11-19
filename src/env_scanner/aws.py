"""AWS environment discovery for EC2, Lambda, ECS and SageMaker.

Discovery is inventory-only: AWS workloads cannot be probed with a local
subprocess, so each environment is reported with its AWS metadata (and the
Python version where AWS exposes it, e.g. the Lambda runtime).
"""

import sys
from enum import Enum
from typing import Dict, List, Optional

try:
    import boto3

    BOTO3_AVAILABLE = True
except ImportError:
    boto3 = None
    BOTO3_AVAILABLE = False

DEFAULT_REGION = "us-east-1"


class AWSAuthMethod(Enum):
    """AWS authentication methods."""

    AWS_CLI_CREDENTIALS = "aws_cli"
    IAM_ROLE = "iam_role"
    EXPLICIT_KEYS = "explicit_keys"


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def resolve_aws_credentials(
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    session_token: Optional[str] = None,
) -> Optional[Dict]:
    """Resolve AWS credentials, trying explicit keys, then the default chain.

    Returns:
        Dict with 'method', 'session' and 'region', or None if no auth is available.
    """
    if not BOTO3_AVAILABLE:
        return None

    if access_key and secret_key:
        try:
            session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                aws_session_token=session_token,
            )
            return {
                "method": AWSAuthMethod.EXPLICIT_KEYS.value,
                "session": session,
                "region": session.region_name or DEFAULT_REGION,
            }
        except Exception:
            return None

    try:
        session = boto3.Session()
        if session.get_credentials():
            return {
                "method": AWSAuthMethod.AWS_CLI_CREDENTIALS.value,
                "session": session,
                "region": session.region_name or DEFAULT_REGION,
            }
    except Exception:
        pass

    # Fall back to an instance profile, which only shows up when STS accepts the call.
    try:
        session = boto3.Session()
        session.client("sts").get_caller_identity()
        return {
            "method": AWSAuthMethod.IAM_ROLE.value,
            "session": session,
            "region": session.region_name or DEFAULT_REGION,
        }
    except Exception:
        return None


def find_aws_ec2_environments(
    region: str = DEFAULT_REGION, session: Optional[object] = None
) -> List[Dict[str, str]]:
    """Discover running EC2 instances in a region."""
    if not BOTO3_AVAILABLE or not session:
        return []

    environments: List[Dict[str, str]] = []
    try:
        ec2 = session.client("ec2", region_name=region)
        response = ec2.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
                environments.append(
                    {
                        "source": "aws-ec2",
                        "name": tags.get("Name", instance_id),
                        "env_path": f"aws://{region}/{instance_id}",
                        "python_executable": f"aws-ec2:{region}:{instance_id}:python3",
                        "aws_instance_id": instance_id,
                        "aws_region": region,
                        "aws_instance_type": instance.get("InstanceType", "unknown"),
                    }
                )
    except Exception as exc:
        _warn(f"EC2 discovery failed in {region}: {exc}")
        return []
    return environments


def find_aws_lambda_environments(
    region: str = DEFAULT_REGION, session: Optional[object] = None
) -> List[Dict[str, str]]:
    """Discover Lambda functions and their runtimes in a region."""
    if not BOTO3_AVAILABLE or not session:
        return []

    environments: List[Dict[str, str]] = []
    try:
        lambda_client = session.client("lambda", region_name=region)
        paginator = lambda_client.get_paginator("list_functions")
        for page in paginator.paginate():
            for func in page.get("Functions", []):
                func_name = func["FunctionName"]
                runtime = func.get("Runtime", "unknown")
                python_version = (
                    runtime[len("python"):] if runtime.startswith("python") else "unknown"
                )
                environments.append(
                    {
                        "source": "aws-lambda",
                        "name": func_name,
                        "env_path": f"aws://{region}/lambda/{func_name}",
                        "python_executable": f"aws-lambda:{region}:{func_name}:python",
                        "python_version": python_version,
                        "implementation": "CPython" if python_version != "unknown" else None,
                        "aws_function_name": func_name,
                        "aws_region": region,
                        "aws_runtime": runtime,
                    }
                )
    except Exception as exc:
        _warn(f"Lambda discovery failed in {region}: {exc}")
        return []
    return environments


def find_aws_ecs_environments(
    region: str = DEFAULT_REGION, session: Optional[object] = None
) -> List[Dict[str, str]]:
    """Discover containers of running ECS tasks in a region."""
    if not BOTO3_AVAILABLE or not session:
        return []

    environments: List[Dict[str, str]] = []
    try:
        ecs = session.client("ecs", region_name=region)
        task_definitions: Dict[str, Dict] = {}

        for cluster_arn in ecs.list_clusters().get("clusterArns", []):
            cluster_name = cluster_arn.split("/")[-1]
            task_arns = ecs.list_tasks(cluster=cluster_arn).get("taskArns", [])
            if not task_arns:
                continue

            for task in ecs.describe_tasks(cluster=cluster_arn, tasks=task_arns).get("tasks", []):
                task_id = task["taskArn"].split("/")[-1]
                task_def_arn = task["taskDefinitionArn"]
                if task_def_arn not in task_definitions:
                    task_definitions[task_def_arn] = ecs.describe_task_definition(
                        taskDefinition=task_def_arn
                    )["taskDefinition"]
                task_def = task_definitions[task_def_arn]

                for container in task_def.get("containerDefinitions", []):
                    container_name = container.get("name", "")
                    environments.append(
                        {
                            "source": "aws-ecs",
                            "name": f"{cluster_name}/{task_def['family']}/{container_name}",
                            "env_path": f"aws://{region}/ecs/{cluster_name}",
                            "python_executable": (
                                f"aws-ecs:{region}:{task_id}:{container_name}:python"
                            ),
                            "aws_cluster": cluster_name,
                            "aws_region": region,
                            "aws_container_name": container_name,
                            "aws_container_image": container.get("image", ""),
                        }
                    )
    except Exception as exc:
        _warn(f"ECS discovery failed in {region}: {exc}")
        return []
    return environments


def find_aws_sagemaker_environments(
    region: str = DEFAULT_REGION, session: Optional[object] = None
) -> List[Dict[str, str]]:
    """Discover SageMaker notebook instances, training jobs and endpoints."""
    if not BOTO3_AVAILABLE or not session:
        return []

    environments: List[Dict[str, str]] = []
    try:
        sagemaker = session.client("sagemaker", region_name=region)

        for notebook in sagemaker.list_notebook_instances().get("NotebookInstances", []):
            name = notebook["NotebookInstanceName"]
            environments.append(
                {
                    "source": "aws-sagemaker",
                    "name": f"notebook:{name}",
                    "env_path": f"aws://{region}/sagemaker/notebook/{name}",
                    "python_executable": f"aws-sagemaker:{region}:notebook:{name}:python",
                    "aws_region": region,
                    "aws_notebook_name": name,
                    "aws_instance_type": notebook.get("InstanceType", "unknown"),
                }
            )

        for job in sagemaker.list_training_jobs().get("TrainingJobSummaries", []):
            name = job["TrainingJobName"]
            environments.append(
                {
                    "source": "aws-sagemaker",
                    "name": f"training:{name}",
                    "env_path": f"aws://{region}/sagemaker/training/{name}",
                    "python_executable": f"aws-sagemaker:{region}:training:{name}:python",
                    "aws_region": region,
                }
            )

        for endpoint in sagemaker.list_endpoints().get("Endpoints", []):
            name = endpoint["EndpointName"]
            environments.append(
                {
                    "source": "aws-sagemaker",
                    "name": f"endpoint:{name}",
                    "env_path": f"aws://{region}/sagemaker/endpoint/{name}",
                    "python_executable": f"aws-sagemaker:{region}:endpoint:{name}:python",
                    "aws_region": region,
                }
            )
    except Exception as exc:
        _warn(f"SageMaker discovery failed in {region}: {exc}")
        return []
    return environments


def discover_aws_environments(
    regions: Optional[List[str]] = None,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    session_token: Optional[str] = None,
    enable_ec2: bool = True,
    enable_lambda: bool = True,
    enable_ecs: bool = True,
    enable_sagemaker: bool = True,
) -> List[Dict[str, str]]:
    """Discover AWS environments across the given regions (default us-east-1)."""
    if not BOTO3_AVAILABLE:
        _warn("boto3 is not installed (pip install boto3). Skipping AWS discovery.")
        return []

    auth = resolve_aws_credentials(access_key, secret_key, session_token)
    if not auth:
        _warn("No AWS credentials found. Skipping AWS environment discovery.")
        return []

    session = auth["session"]
    environments: List[Dict[str, str]] = []
    for region in regions or [DEFAULT_REGION]:
        print(f"Scanning AWS region {region}...")
        if enable_ec2:
            environments.extend(find_aws_ec2_environments(region, session))
        if enable_lambda:
            environments.extend(find_aws_lambda_environments(region, session))
        if enable_ecs:
            environments.extend(find_aws_ecs_environments(region, session))
        if enable_sagemaker:
            environments.extend(find_aws_sagemaker_environments(region, session))

    print(f"Found {len(environments)} AWS environment(s).")
    return environments
