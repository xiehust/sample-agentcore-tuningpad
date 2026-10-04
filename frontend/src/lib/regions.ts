/** Regions where SageMaker HyperPod (EKS) and AgentCore Runtime are both offered. */
export const REGIONS = ["us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1", "ap-northeast-1", "ap-southeast-2"];

export const regionOptions = REGIONS.map((r) => ({ value: r, label: r }));
