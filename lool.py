from constructs import Construct
import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_logs as logs,
    aws_ecr as ecr,
    aws_lambda as _lambda,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_s3 as s3,
)


class AutodocStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ============================================================
        # 1) VPC
        # ============================================================
        vpc = ec2.Vpc(
            self,
            "AutodocVpc",
            max_azs=2,
            nat_gateways=1,
        )

        vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        # ============================================================
        # 2) Security Group for ECS Tasks
        # ============================================================
        tasks_sg = ec2.SecurityGroup(
            self,
            "TasksSG",
            vpc=vpc,
            description="Security group for Fargate tasks (indexer + cloner)",
            allow_all_outbound=True,
        )

        # ============================================================
        # 3) ECS Cluster
        # ============================================================
        cluster = ecs.Cluster(
            self,
            "AutodocCluster",
            vpc=vpc,
        )

        # ============================================================
        # 4) S3 Buckets (data + access logs)
        # ============================================================
        s3_logs_bucket = s3.Bucket(
            self,
            "AutodocS3AccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        data_bucket = s3.Bucket(
            self,
            "AutodocDataBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            server_access_logs_bucket=s3_logs_bucket,
            server_access_logs_prefix="s3-access-logs/",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ============================================================
        # 5) ECR Indexer Image
        # ============================================================
        INDEXER_REPO_NAME = "autodoc-indexer"
        INDEXER_TAG = "latest"

        indexer_ecr_repo = ecr.Repository.from_repository_name(
            self, "IndexerEcrRepo", INDEXER_REPO_NAME
        )

        # ============================================================
        # 6) CloudWatch Log Group for Fargate Indexer
        # ============================================================
        indexer_log_group = logs.LogGroup(
            self,
            "IndexerLogGroup",
            log_group_name="/ecs/autodoc-indexer",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ============================================================
        # 7) Fargate Task Definition (Indexer, CPU-only)
        # ============================================================
        indexer_task_def = ecs.FargateTaskDefinition(
            self,
            "IndexerTaskDef",
            cpu=4096,
            memory_limit_mib=8192,
        )

        indexer_task_def.add_to_execution_role_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=["*"],
            )
        )

        indexer_task_def.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    data_bucket.bucket_arn,
                    f"{data_bucket.bucket_arn}/cloned-repos/*",
                ],
            )
        )

        indexer_task_def.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                resources=[
                    data_bucket.bucket_arn,
                    f"{data_bucket.bucket_arn}/vector-datasets/*",
                ],
            )
        )

        indexer_container = indexer_task_def.add_container(
            "autodoc-indexer",
            image=ecs.ContainerImage.from_ecr_repository(
                indexer_ecr_repo, tag=INDEXER_TAG
            ),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="autodoc-indexer",
                log_group=indexer_log_group,
            ),
        )
        indexer_container.add_environment("PYTHONUNBUFFERED", "1")

        # ============================================================
        # 8) Lambda → RunTask (Indexer)
        # ============================================================
        subnet_ids = [sn.subnet_id for sn in vpc.private_subnets]

        indexer_lambda = _lambda.Function(
            self,
            "IndexerTriggerFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.main",
            code=_lambda.Code.from_asset("lambda/indexer_trigger"),
            timeout=Duration.seconds(60),
            environment={
                "CLUSTER_ARN": cluster.cluster_arn,
                "TASK_DEF_ARN": indexer_task_def.task_definition_arn,
                "SUBNETS": ",".join(subnet_ids),
                "SECURITY_GROUP_ID": tasks_sg.security_group_id,
                "DATA_BUCKET": data_bucket.bucket_name,
            },
        )

        indexer_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask"],
                resources=[indexer_task_def.task_definition_arn],
            )
        )
        indexer_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[
                    indexer_task_def.task_role.role_arn,
                    indexer_task_def.execution_role.role_arn,
                ],
            )
        )
        indexer_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeTasks"],
                resources=["*"],
            )
        )

        # ============================================================
        # 9) HTTP API
        # ============================================================
        http_api = apigwv2.HttpApi(
            self,
            "IndexerHttpApi",
            api_name="autodoc-indexer-api",
        )

        index_integration = apigwv2_integrations.HttpLambdaIntegration(
            "IndexerLambdaIntegration",
            handler=indexer_lambda,
        )
        http_api.add_routes(
            path="/index",
            methods=[apigwv2.HttpMethod.POST],
            integration=index_integration,
        )

        # ============================================================
        # 10) ECS Cloner Task Definition (autodoc-cloner, CPU-only)
        # ============================================================
        CLONER_REPO_NAME = "autodoc-cloner"
        CLONER_TAG = "latest"

        cloner_ecr_repo = ecr.Repository.from_repository_name(
            self, "ClonerEcrRepo", CLONER_REPO_NAME
        )

        cloner_log_group = logs.LogGroup(
            self,
            "ClonerLogGroup",
            log_group_name="/ecs/autodoc-cloner",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        cloner_task_def = ecs.FargateTaskDefinition(
            self,
            "ClonerTaskDef",
            cpu=1024,
            memory_limit_mib=2048,
        )

        cloner_task_def.add_to_execution_role_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=["*"],
            )
        )

        cloner_task_def.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                resources=[
                    data_bucket.bucket_arn,
                    f"{data_bucket.bucket_arn}/cloned-repos/*",
                ],
            )
        )

        cloner_container = cloner_task_def.add_container(
            "autodoc-cloner",
            image=ecs.ContainerImage.from_ecr_repository(
                cloner_ecr_repo, tag=CLONER_TAG
            ),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="autodoc-cloner",
                log_group=cloner_log_group,
            ),
        )
        cloner_container.add_environment("DATA_BUCKET", data_bucket.bucket_name)
        cloner_container.add_environment("CLONED_REPOS_PREFIX", "cloned-repos")

        # ============================================================
        # 11) CloneTriggerFn Lambda → RunTask (Cloner)
        # ============================================================
        clone_lambda = _lambda.Function(
            self,
            "CloneTriggerFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.main",
            code=_lambda.Code.from_asset("lambda/clone_trigger"),
            timeout=Duration.seconds(30),
            environment={
                "CLUSTER_ARN": cluster.cluster_arn,
                "CLONE_TASK_DEF_ARN": cloner_task_def.task_definition_arn,
                "SUBNETS": ",".join(subnet_ids),
                "SECURITY_GROUP_ID": tasks_sg.security_group_id,
                "DATA_BUCKET": data_bucket.bucket_name,
                "CLONED_REPOS_PREFIX": "cloned-repos",
            },
        )

        clone_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask"],
                resources=[cloner_task_def.task_definition_arn],
            )
        )
        clone_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[
                    cloner_task_def.task_role.role_arn,
                    cloner_task_def.execution_role.role_arn,
                ],
            )
        )
        clone_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeTasks"],
                resources=["*"],
            )
        )

        clone_integration = apigwv2_integrations.HttpLambdaIntegration(
            "CloneLambdaIntegration",
            handler=clone_lambda,
        )
        http_api.add_routes(
            path="/clone",
            methods=[apigwv2.HttpMethod.POST],
            integration=clone_integration,
        )

        # ============================================================
        # 11a) DetectChangesFn Lambda → RunTask (Cloner w/ ACTION=detect_changes)
        # ============================================================
        detect_changes_lambda = _lambda.Function(
            self,
            "DetectChangesFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.main",
            code=_lambda.Code.from_asset("lambda/detect_changes"),
            timeout=Duration.seconds(60),
            environment={
                "CLUSTER_ARN": cluster.cluster_arn,
                "CLONE_TASK_DEF_ARN": cloner_task_def.task_definition_arn,  # reuse cloner task
                "SUBNETS": ",".join(subnet_ids),
                "SECURITY_GROUP_ID": tasks_sg.security_group_id,
                "DATA_BUCKET": data_bucket.bucket_name,
                "CLONED_REPOS_PREFIX": "cloned-repos",
            },
        )

        detect_changes_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask"],
                resources=[cloner_task_def.task_definition_arn],
            )
        )
        detect_changes_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[
                    cloner_task_def.task_role.role_arn,
                    cloner_task_def.execution_role.role_arn,
                ],
            )
        )
        detect_changes_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeTasks"],
                resources=["*"],
            )
        )

        detect_changes_integration = apigwv2_integrations.HttpLambdaIntegration(
            "DetectChangesLambdaIntegration",
            handler=detect_changes_lambda,
        )
        http_api.add_routes(
            path="/detect-changes",
            methods=[apigwv2.HttpMethod.POST],
            integration=detect_changes_integration,
        )

        # ============================================================
        # 11b) IndexStatusFn Lambda → ECS status + S3 job summary
        # ============================================================
        status_lambda = _lambda.Function(
            self,
            "IndexStatusFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.main",
            code=_lambda.Code.from_asset("lambda/index_status"),
            timeout=Duration.seconds(30),
            environment={
                "CLUSTER_ARN": cluster.cluster_arn,
                "DATA_BUCKET": data_bucket.bucket_name,
            },
        )

        status_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeTasks"],
                resources=["*"],
            )
        )

        status_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    data_bucket.bucket_arn,
                    f"{data_bucket.bucket_arn}/*",
                ],
            )
        )

        status_integration = apigwv2_integrations.HttpLambdaIntegration(
            "IndexStatusLambdaIntegration",
            handler=status_lambda,
        )
        http_api.add_routes(
            path="/index-status",
            methods=[apigwv2.HttpMethod.POST],
            integration=status_integration,
        )

        # ============================================================
        # 11c) CloneStatusFn Lambda → ECS status + clone manifest
        # ============================================================
        clone_status_lambda = _lambda.Function(
            self,
            "CloneStatusFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.main",
            code=_lambda.Code.from_asset("lambda/clone_status"),
            timeout=Duration.seconds(30),
            environment={
                "CLUSTER_ARN": cluster.cluster_arn,
                "DATA_BUCKET": data_bucket.bucket_name,
                "CLONED_REPOS_PREFIX": "cloned-repos",
            },
        )

        clone_status_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeTasks"],
                resources=["*"],
            )
        )

        clone_status_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    data_bucket.bucket_arn,
                    f"{data_bucket.bucket_arn}/*",
                ],
            )
        )

        clone_status_integration = apigwv2_integrations.HttpLambdaIntegration(
            "CloneStatusLambdaIntegration",
            handler=clone_status_lambda,
        )
        http_api.add_routes(
            path="/clone-status",
            methods=[apigwv2.HttpMethod.POST],
            integration=clone_status_integration,
        )

        # ============================================================
        # 12) Outputs
        # ============================================================
        cdk.CfnOutput(self, "VpcId", value=vpc.vpc_id)
        cdk.CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "DataBucketName", value=data_bucket.bucket_name)
        cdk.CfnOutput(self, "IndexerTaskDefArn", value=indexer_task_def.task_definition_arn)
        cdk.CfnOutput(self, "IndexerApiEndpoint", value=http_api.api_endpoint)
        cdk.CfnOutput(self, "IndexerLogGroupName", value=indexer_log_group.log_group_name)
        cdk.CfnOutput(self, "ClonerTaskDefArn", value=cloner_task_def.task_definition_arn)
        cdk.CfnOutput(self, "ClonerLogGroupName", value=cloner_log_group.log_group_name)
        cdk.CfnOutput(self, "TasksSecurityGroupId", value=tasks_sg.security_group_id)
l
