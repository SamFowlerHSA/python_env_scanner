"""Integration tests for AWS scanning in env_scanner.inventory."""

import sys
import unittest
from unittest import mock

from env_scanner import inventory as env_inventory

AWS_LAMBDA_ENV = {
    "source": "aws-lambda",
    "name": "fn",
    "env_path": "aws://us-east-1/lambda/fn",
    "python_executable": "aws-lambda:us-east-1:fn:python",
    "python_version": "3.11",
    "aws_runtime": "python3.11",
}


def make_args(**overrides):
    base = dict(
        no_conda=True,
        no_uv=True,
        no_venv_scan=True,
        no_system=True,
        no_wsl=True,
        scan_root=None,
        max_depth=1,
        scan_timeout=1,
        max_workers=1,
        timeout=1,
        aws=False,
        aws_region=None,
        aws_access_key=None,
        aws_secret_key=None,
        aws_session_token=None,
        no_aws_ec2=False,
        no_aws_lambda=False,
        no_aws_ecs=False,
        no_aws_sagemaker=False,
    )
    base.update(overrides)
    return mock.MagicMock(**base)


class TestAWSCli(unittest.TestCase):
    def test_flags_parse(self):
        argv = [
            "env_scanner.inventory.py", "--aws", "--aws-region", "us-west-2", "--aws-region", "eu-west-1",
            "--no-aws-ec2", "--aws-access-key", "k", "--aws-secret-key", "s",
        ]
        with mock.patch.object(sys, "argv", argv):
            args = env_inventory.parse_args()

        self.assertTrue(args.aws)
        self.assertEqual(args.aws_region, ["us-west-2", "eu-west-1"])
        self.assertTrue(args.no_aws_ec2)
        self.assertFalse(args.no_aws_lambda)

    def test_aws_off_by_default(self):
        with mock.patch.object(sys, "argv", ["env_scanner.inventory.py"]):
            self.assertFalse(env_inventory.parse_args().aws)


class TestAWSDiscoveryIntegration(unittest.TestCase):
    @mock.patch("env_scanner.inventory.discover_aws_environments")
    def test_not_called_without_flag(self, mock_discover):
        env_inventory.discover_environments(make_args())
        mock_discover.assert_not_called()

    @mock.patch("env_scanner.inventory.discover_aws_environments", return_value=[AWS_LAMBDA_ENV])
    def test_called_with_flags(self, mock_discover):
        envs = env_inventory.discover_environments(
            make_args(aws=True, aws_region=["eu-west-1"], no_aws_ec2=True)
        )

        self.assertEqual(envs, [AWS_LAMBDA_ENV])
        kwargs = mock_discover.call_args.kwargs
        self.assertEqual(kwargs["regions"], ["eu-west-1"])
        self.assertFalse(kwargs["enable_ec2"])
        self.assertTrue(kwargs["enable_lambda"])

    @mock.patch("env_scanner.inventory.get_tool_version", return_value=None)
    @mock.patch("env_scanner.inventory.probe_interpreter")
    @mock.patch("env_scanner.inventory.discover_aws_environments", return_value=[AWS_LAMBDA_ENV])
    def test_aws_rows_are_not_probed(self, _, mock_probe, __):
        minimums = dict(env_inventory.REQUIRED_VERSIONS)

        rows = env_inventory.collect_environments(make_args(aws=True), minimums)

        mock_probe.assert_not_called()
        self.assertEqual(rows[0]["env_verdict"], "not_probed")
        self.assertEqual(rows[0]["python_version"], "3.11")

        summary = env_inventory.build_summary(rows, minimums, None)
        self.assertEqual(summary["action_required"], 0)
        self.assertEqual(summary["probe_errors"], 0)


if __name__ == "__main__":
    unittest.main()
