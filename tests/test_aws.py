"""Unit tests for env_scanner.aws with mocked boto3 calls."""

import unittest
from unittest import mock

from env_scanner import aws as aws_scanner
from env_scanner.aws import (
    AWSAuthMethod,
    discover_aws_environments,
    find_aws_ec2_environments,
    find_aws_ecs_environments,
    find_aws_lambda_environments,
    find_aws_sagemaker_environments,
    resolve_aws_credentials,
)


def with_boto3(test):
    """Patch boto3 and mark it available so tests run without the real package."""
    test = mock.patch("env_scanner.aws.BOTO3_AVAILABLE", True)(test)
    return mock.patch("env_scanner.aws.boto3", create=True)(test)


class TestAWSAuthentication(unittest.TestCase):
    @with_boto3
    def test_explicit_keys(self, mock_boto3):
        session = mock.MagicMock(region_name="us-west-2")
        mock_boto3.Session.return_value = session

        result = resolve_aws_credentials(access_key="AKIA", secret_key="secret")

        self.assertEqual(result["method"], AWSAuthMethod.EXPLICIT_KEYS.value)
        self.assertEqual(result["region"], "us-west-2")

    @with_boto3
    def test_cli_credentials(self, mock_boto3):
        session = mock.MagicMock(region_name=None)
        session.get_credentials.return_value = object()
        mock_boto3.Session.return_value = session

        result = resolve_aws_credentials()

        self.assertEqual(result["method"], AWSAuthMethod.AWS_CLI_CREDENTIALS.value)
        self.assertEqual(result["region"], "us-east-1")

    @with_boto3
    def test_iam_role_fallback(self, mock_boto3):
        session = mock.MagicMock(region_name="eu-west-1")
        session.get_credentials.return_value = None
        mock_boto3.Session.return_value = session

        result = resolve_aws_credentials()

        self.assertEqual(result["method"], AWSAuthMethod.IAM_ROLE.value)

    @with_boto3
    def test_no_credentials(self, mock_boto3):
        session = mock.MagicMock()
        session.get_credentials.return_value = None
        session.client.side_effect = Exception("no credentials")
        mock_boto3.Session.return_value = session

        self.assertIsNone(resolve_aws_credentials())

    @mock.patch("env_scanner.aws.BOTO3_AVAILABLE", False)
    def test_boto3_missing(self):
        self.assertIsNone(resolve_aws_credentials("a", "b"))
        self.assertEqual(discover_aws_environments(), [])


class TestEC2Discovery(unittest.TestCase):
    @with_boto3
    def test_find_instances(self, _):
        session = mock.MagicMock()
        session.client.return_value.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-123",
                            "InstanceType": "t3.micro",
                            "Tags": [{"Key": "Name", "Value": "web-1"}],
                        }
                    ]
                }
            ]
        }

        envs = find_aws_ec2_environments("us-east-1", session)

        self.assertEqual(len(envs), 1)
        self.assertEqual(envs[0]["source"], "aws-ec2")
        self.assertEqual(envs[0]["name"], "web-1")
        self.assertEqual(envs[0]["aws_instance_id"], "i-123")

    @with_boto3
    def test_api_error_returns_empty(self, _):
        session = mock.MagicMock()
        session.client.return_value.describe_instances.side_effect = Exception("boom")

        self.assertEqual(find_aws_ec2_environments("us-east-1", session), [])


class TestLambdaDiscovery(unittest.TestCase):
    def _session(self, functions):
        session = mock.MagicMock()
        paginator = session.client.return_value.get_paginator.return_value
        paginator.paginate.return_value = [{"Functions": functions}]
        return session

    @with_boto3
    def test_python_runtime(self, _):
        session = self._session([{"FunctionName": "fn", "Runtime": "python3.11"}])

        envs = find_aws_lambda_environments("us-east-1", session)

        self.assertEqual(envs[0]["python_version"], "3.11")
        self.assertEqual(envs[0]["aws_function_name"], "fn")

    @with_boto3
    def test_non_python_runtime(self, _):
        session = self._session([{"FunctionName": "fn", "Runtime": "nodejs18.x"}])

        envs = find_aws_lambda_environments("us-east-1", session)

        self.assertEqual(envs[0]["python_version"], "unknown")
        self.assertEqual(envs[0]["aws_runtime"], "nodejs18.x")


class TestECSDiscovery(unittest.TestCase):
    @with_boto3
    def test_find_containers(self, _):
        session = mock.MagicMock()
        ecs = session.client.return_value
        ecs.list_clusters.return_value = {"clusterArns": ["arn:aws:ecs:r:1:cluster/my-cluster"]}
        ecs.list_tasks.return_value = {"taskArns": ["arn:aws:ecs:r:1:task/my-cluster/abc"]}
        ecs.describe_tasks.return_value = {
            "tasks": [
                {
                    "taskArn": "arn:aws:ecs:r:1:task/my-cluster/abc",
                    "taskDefinitionArn": "arn:aws:ecs:r:1:task-definition/t:1",
                }
            ]
        }
        ecs.describe_task_definition.return_value = {
            "taskDefinition": {
                "family": "t",
                "containerDefinitions": [{"name": "app", "image": "img:latest"}],
            }
        }

        envs = find_aws_ecs_environments("us-east-1", session)

        self.assertEqual(len(envs), 1)
        self.assertEqual(envs[0]["aws_cluster"], "my-cluster")
        self.assertEqual(envs[0]["aws_container_name"], "app")


class TestSageMakerDiscovery(unittest.TestCase):
    @with_boto3
    def test_find_all_kinds(self, _):
        session = mock.MagicMock()
        sm = session.client.return_value
        sm.list_notebook_instances.return_value = {
            "NotebookInstances": [{"NotebookInstanceName": "nb", "InstanceType": "ml.t3.medium"}]
        }
        sm.list_training_jobs.return_value = {"TrainingJobSummaries": [{"TrainingJobName": "job"}]}
        sm.list_endpoints.return_value = {"Endpoints": [{"EndpointName": "ep"}]}

        envs = find_aws_sagemaker_environments("us-east-1", session)

        self.assertEqual([e["name"] for e in envs], ["notebook:nb", "training:job", "endpoint:ep"])


class TestDiscoverAll(unittest.TestCase):
    @with_boto3
    @mock.patch("env_scanner.aws.resolve_aws_credentials")
    def test_service_flags_and_regions(self, mock_auth, _):
        mock_auth.return_value = {"session": mock.MagicMock()}
        with mock.patch.object(
            aws_scanner, "find_aws_ec2_environments", return_value=[{"source": "aws-ec2"}]
        ) as ec2, mock.patch.object(
            aws_scanner, "find_aws_lambda_environments", return_value=[{"source": "aws-lambda"}]
        ) as lam, mock.patch.object(
            aws_scanner, "find_aws_ecs_environments", return_value=[]
        ) as ecs, mock.patch.object(
            aws_scanner, "find_aws_sagemaker_environments", return_value=[]
        ) as sm:
            envs = discover_aws_environments(
                regions=["us-east-1", "eu-west-1"], enable_ecs=False, enable_sagemaker=False
            )

        self.assertEqual(len(envs), 4)
        self.assertEqual(ec2.call_count, 2)
        self.assertEqual(lam.call_count, 2)
        ecs.assert_not_called()
        sm.assert_not_called()

    @with_boto3
    @mock.patch("env_scanner.aws.resolve_aws_credentials", return_value=None)
    def test_no_credentials(self, *_):
        self.assertEqual(discover_aws_environments(), [])


if __name__ == "__main__":
    unittest.main()
